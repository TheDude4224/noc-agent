"""Tell a human what happened. Stdout always; a webhook if configured.

The message is deliberately terse. If the agent did something, say what. If it
didn't, say why. Nobody wants a paragraph at 2am.
"""

from __future__ import annotations

import sys

import httpx

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


def format_line(rec: AuditRecord) -> str:
    a = rec.alert
    head = f"[{_ICON.get(rec.decision, rec.decision)}] {a.alertname} on {a.host or a.instance or '?'}"
    parts = [head]
    if rec.runbook_id and rec.runbook_id != "escalate-to-human":
        parts.append(f"runbook={rec.runbook_id}")
    if rec.triage:
        parts.append(f"conf={rec.triage.confidence:.2f}")
    parts.append(rec.reason)
    if rec.decision == "needs-approval":
        parts.append(f"approve with: noc-agent approve {rec.run_id}")
    return " | ".join(parts)


class Notifier:
    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    def send(self, rec: AuditRecord) -> None:
        line = format_line(rec)
        print(line, file=sys.stdout, flush=True)
        if not self.webhook_url:
            return
        try:
            httpx.post(self.webhook_url, json={"text": line}, timeout=5)
        except Exception as e:  # noqa: BLE001 - notification failure must never block the run
            print(f"[notify] webhook failed: {e}", file=sys.stderr)
