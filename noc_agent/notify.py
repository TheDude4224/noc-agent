"""Tell a human what is about to happen, then what happened. Stdout always; a webhook if configured.

Say what it's going to do, do what it said: before a runbook executes, `announce` posts the
intent, the evidence (the model's reasoning and the alert) and the impact. After every run,
`send` posts the outcome. Messages stay terse. Nobody wants a paragraph at 2am.

Webhook payload: {"text": <line>, "stage": "before"|"after"|"diagnosis", "run_id", "alertname",
"runbook_id", "decision"}. Receivers that only read "text" keep working.
"""

from __future__ import annotations

import sys

import httpx

from .metrics import METRICS
from .models import AuditRecord

_ICON = {
    "executed": "FIXED",
    "executed-rolled-back": "ROLLED BACK",
    "dry-run": "DRY RUN",
    "needs-approval": "NEEDS APPROVAL",
    "blocked-policy": "BLOCKED",
    "escalated": "ESCALATED",
    "error": "ERROR",
}
_ACTED = ("executed", "executed-rolled-back", "error")


def _where(rec: AuditRecord) -> str:
    a = rec.alert
    return f"{a.alertname} on {a.host or a.instance or '?'}"


def format_line(rec: AuditRecord) -> str:
    parts = [f"[{_ICON.get(rec.decision, rec.decision)}] {_where(rec)}"]
    if rec.runbook_id and rec.runbook_id != "escalate-to-human":
        parts.append(f"runbook={rec.runbook_id}")
    if rec.intent and rec.decision in _ACTED and rec.exit_code is not None:
        parts.append(f"did: {rec.intent}")
    if rec.triage:
        parts.append(f"conf={rec.triage.confidence:.2f}")
    parts.append(rec.reason)
    if rec.decision == "needs-approval":
        parts.append(f"approve with: noc-agent approve {rec.run_id}")
    return " | ".join(parts)


def format_announcement(rec: AuditRecord, impact: str, why: str) -> str:
    parts = [f"[GOING TO] {_where(rec)}", f"runbook={rec.runbook_id}", f"will: {rec.intent}",
             f"why: {why}", f"impact: {impact}"]
    if rec.triage:
        parts.append(f"conf={rec.triage.confidence:.2f}")
    parts.append(f"run={rec.run_id}")
    return " | ".join(parts)


class Notifier:
    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    def _post(self, line: str, stage: str, rec: AuditRecord) -> bool:
        """True only when the webhook confirmed (2xx). Never raises."""
        print(line, file=sys.stdout, flush=True)
        if not self.webhook_url:
            return True
        body = {"text": line, "stage": stage, "run_id": rec.run_id, "alertname": rec.alert.alertname,
                "runbook_id": rec.runbook_id, "decision": None if stage == "before" else rec.decision}
        try:
            r = httpx.post(self.webhook_url, json=body, timeout=30 if stage == "before" else 5)
            ok = 200 <= r.status_code < 300
            if not ok:
                print(f"[notify] webhook answered {r.status_code}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - a notification failure must never crash a run
            print(f"[notify] webhook failed: {e}", file=sys.stderr)
            ok = False
        METRICS.inc("noc_announcements_total", {"stage": stage, "result": "ok" if ok else "failed"})
        return ok

    def announce(self, rec: AuditRecord, *, impact: str, why: str) -> bool:
        """Before acting. Returns whether the announcement was delivered."""
        return self._post(format_announcement(rec, impact, why), "before", rec)

    def send(self, rec: AuditRecord) -> bool:
        """After every run."""
        return self._post(format_line(rec), "after", rec)

    def send_diagnosis(self, rec: AuditRecord, text: str) -> bool:
        """The writer's follow-up for a run that landed on a person."""
        return self._post(f"[DIAGNOSIS] {_where(rec)} | run={rec.run_id}\n{text}", "diagnosis", rec)
