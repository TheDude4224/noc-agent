"""The loop: alert -> triage -> policy -> (execute | park | log) -> audit -> notify."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .audit import AuditLog
from .config import Config, load_runbooks
from .executor import execute, render, render_text
from .metrics import METRICS, engine_version
from .models import Alert, AuditRecord, Runbook
from .notify import Notifier
from .policy import Policy
from .triage import Triager
from .writer import Writer


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.runbooks: list[Runbook] = load_runbooks(cfg.runbooks_file)
        self.by_id = {rb.id: rb for rb in self.runbooks}
        self.triager = Triager(cfg.llm)
        self.policy = Policy(cfg.policy)
        self.audit = AuditLog(cfg.audit.path)
        self.notifier = Notifier(cfg.notify.webhook_url)
        if cfg.notify.require_announce and not cfg.notify.webhook_url:
            raise ValueError("notify.require_announce needs notify.webhook_url: there is nowhere to announce to")
        self.writer = Writer(cfg.writer, on_written=self.notifier.send_diagnosis)
        self.approvals = Path(cfg.approvals_dir)
        self.approvals.mkdir(parents=True, exist_ok=True)
        METRICS.set("noc_build_info", 1, {"version": engine_version()})
        METRICS.set("noc_started_timestamp_seconds", time.time())
        METRICS.set("noc_dry_run", 1 if cfg.policy.dry_run else 0)
        METRICS.set("noc_runbooks", len(self.runbooks))

    def pending_approvals(self) -> int:
        return sum(1 for _ in self.approvals.glob("*.json"))

    def menu_for(self, alert: Alert) -> list[Runbook]:
        """Only show the model runbooks that claim this alert, plus escalate."""
        menu = [rb for rb in self.runbooks if rb.applies_to(alert) and rb.id != "escalate-to-human"]
        esc = self.by_id.get("escalate-to-human")
        if esc:
            menu.append(esc)
        return menu

    def handle(self, alert: Alert) -> AuditRecord:
        t0 = time.time()
        run_id = uuid.uuid4().hex[:12]
        menu = self.menu_for(alert)
        triage = self.triager.triage(alert, menu)
        runbook = self.by_id.get(triage.runbook_id)
        verdict = self.policy.evaluate(alert, triage, runbook)

        rec = AuditRecord(
            run_id=run_id,
            alert=alert,
            triage=triage,
            decision="error",
            runbook_id=runbook.id if runbook else None,
            reason=verdict.reason,
        )

        try:
            if runbook and runbook.id != "escalate-to-human":
                rec.rendered_command = render(runbook.command, alert)
                rec.intent = render_text(runbook.intent or runbook.description, alert)
        except KeyError as e:
            rec.decision = "error"
            rec.reason = f"cannot render runbook: {e}"
            return self._finish(rec, t0)

        if verdict.outcome == "execute":
            assert runbook is not None
            if not self._announce(rec, runbook, why=triage.reasoning or alert.summary or "its alert fired"):
                rec.decision = "blocked-policy"
                rec.reason = "could not announce the action (notify.require_announce); not acting silently"
                return self._finish(rec, t0)
            self.policy.record_execution(alert)
            res = execute(runbook, alert)
            rec.exit_code = res.exit_code
            rec.stdout, rec.stderr = res.stdout, res.stderr
            if res.exit_code == 0:
                rec.decision = "executed"
                rec.reason = "ran and verified"
            elif res.rolled_back:
                rec.decision = "executed-rolled-back"
                rec.reason = "verify failed; rollback ran"
            elif res.verify_failed:
                rec.decision = "error"
                rec.reason = "ran, but verify failed and no rollback is defined; needs a human"
            else:
                rec.decision = "error"
                rec.reason = f"command exited {res.exit_code}"
        elif verdict.outcome == "needs-approval":
            rec.decision = "needs-approval"
            self._park_for_approval(rec)
        elif verdict.outcome == "dry-run":
            rec.decision = "dry-run"
        elif verdict.outcome == "blocked-policy":
            rec.decision = "blocked-policy"
        else:
            rec.decision = "escalated"

        return self._finish(rec, t0)

    def _park_for_approval(self, rec: AuditRecord) -> None:
        """Write a small file a human can review and then run with `noc-agent approve <run_id>`."""
        p = self.approvals / f"{rec.run_id}.json"
        p.write_text(json.dumps({
            "run_id": rec.run_id,
            "alert": rec.alert.model_dump(mode="json"),
            "runbook_id": rec.runbook_id,
            "rendered_command": rec.rendered_command,
            "triage": rec.triage.model_dump() if rec.triage else None,
        }, indent=2))

    def approve(self, run_id: str) -> AuditRecord:
        """A human said yes. Execute the parked action, audit it under a new run id."""
        p = self.approvals / f"{run_id}.json"
        if not p.exists():
            raise FileNotFoundError(f"no parked approval {run_id}")
        data = json.loads(p.read_text())
        alert = Alert.model_validate(data["alert"])
        runbook = self.by_id[data["runbook_id"]]
        t0 = time.time()
        rec = AuditRecord(
            run_id=uuid.uuid4().hex[:12],
            alert=alert,
            decision="error",
            runbook_id=runbook.id,
            rendered_command=data["rendered_command"],
            intent=render_text(runbook.intent or runbook.description, alert),
            reason=f"human-approved parked run {run_id}",
        )
        if not self._announce(rec, runbook, why=f"a human approved parked run {run_id}"):
            rec.decision = "blocked-policy"
            rec.reason = f"approved run {run_id} not executed: could not announce it (notify.require_announce); still parked"
            return self._finish(rec, t0)
        self.policy.record_execution(alert)
        res = execute(runbook, alert)
        rec.decision = "executed" if res.exit_code == 0 else ("executed-rolled-back" if res.rolled_back else "error")
        rec.exit_code, rec.stdout, rec.stderr = res.exit_code, res.stdout, res.stderr
        p.unlink()
        return self._finish(rec, t0)

    def _announce(self, rec: AuditRecord, runbook: Runbook, *, why: str) -> bool:
        """Say what it is going to do. False only if delivery failed AND the config requires it."""
        if not self.cfg.notify.announce:
            return True
        impact = render_text(runbook.impact, rec.alert) if runbook.impact else "not stated in the runbook"
        delivered = self.notifier.announce(rec, impact=impact, why=why)
        return delivered or not self.cfg.notify.require_announce

    def _finish(self, rec: AuditRecord, t0: float) -> AuditRecord:
        rec.duration_ms = int((time.time() - t0) * 1000)
        self.audit.write(rec)
        METRICS.inc("noc_runs_total", {"decision": rec.decision})
        METRICS.observe("noc_run_duration_seconds", rec.duration_ms / 1000)
        METRICS.set("noc_last_run_timestamp_seconds", time.time())
        self.notifier.send(rec)
        self.writer.submit(rec)       # background; a person-bound run gets a written diagnosis
        return rec

    def close(self) -> None:
        """Let queued write-ups finish (CLI commands exit right after one run)."""
        self.writer.close(wait=True)
