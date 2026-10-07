"""Guardrails. Every gate here is deterministic and runs AFTER the model, so the
model can never talk its way past one. Order matters: cheapest, hardest gates first.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .config import PolicyConfig
from .models import Alert, Runbook, Triage


@dataclass
class Verdict:
    allowed: bool
    outcome: str          # "execute" | "dry-run" | "needs-approval" | "blocked-policy" | "escalated"
    reason: str


class Policy:
    def __init__(self, cfg: PolicyConfig):
        self.cfg = cfg
        self._hour_window: deque[float] = deque()
        self._per_alert: dict[str, int] = {}

    # -- bookkeeping -------------------------------------------------------

    def record_execution(self, alert: Alert) -> None:
        self._hour_window.append(time.time())
        self._per_alert[alert.fingerprint] = self._per_alert.get(alert.fingerprint, 0) + 1

    def _executions_last_hour(self) -> int:
        cutoff = time.time() - 3600
        while self._hour_window and self._hour_window[0] < cutoff:
            self._hour_window.popleft()
        return len(self._hour_window)

    # -- the gates ---------------------------------------------------------

    def evaluate(self, alert: Alert, triage: Triage, runbook: Runbook | None) -> Verdict:
        # 1. The model punted, or picked something that doesn't exist.
        if runbook is None or triage.runbook_id == "escalate-to-human":
            return Verdict(False, "escalated", triage.reasoning or "no runbook selected")

        # 2. Labels that are never automated, full stop.
        for rule in self.cfg.never_automate_labels:
            k, _, v = rule.partition("=")
            if alert.labels.get(k) == v:
                return Verdict(False, "blocked-policy", f"label {rule} is on the never-automate list")

        # 3. The runbook must actually claim this alert (alertname and any label matchers).
        #    The model's choice is not enough.
        if not runbook.applies_to(alert):
            return Verdict(False, "blocked-policy",
                           f"runbook '{runbook.id}' does not match alert '{alert.alertname}'")

        # 4. Confidence floor.
        if triage.confidence < self.cfg.min_confidence_to_act:
            return Verdict(False, "blocked-policy",
                           f"confidence {triage.confidence:.2f} below floor {self.cfg.min_confidence_to_act:.2f}")

        # 5. Rate caps. If something is looping, this is what stops it.
        if self._per_alert.get(alert.fingerprint, 0) >= self.cfg.max_actions_per_alert:
            return Verdict(False, "blocked-policy",
                           f"per-alert cap reached ({self.cfg.max_actions_per_alert}) for {alert.fingerprint}")
        if self._executions_last_hour() >= self.cfg.max_actions_per_hour:
            return Verdict(False, "blocked-policy",
                           f"hourly cap reached ({self.cfg.max_actions_per_hour})")

        # 6. Irreversible actions always wait for a person.
        if not runbook.reversible and self.cfg.require_approval_for_irreversible:
            return Verdict(False, "needs-approval",
                           f"runbook '{runbook.id}' is irreversible; parked for human approval")

        # 7. Dry run is the default and the last gate before anything real.
        if self.cfg.dry_run:
            return Verdict(False, "dry-run", "policy.dry_run is true; nothing executed")

        return Verdict(True, "execute", "all gates passed")
