"""Data shapes. Small on purpose."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

from .matchers import Selector, label_view, parse_selector


class Alert(BaseModel):
    """One firing alert, normalized from whatever sent it."""

    alertname: str
    instance: str = ""              # host:port as Prometheus reports it
    host: str = ""                  # bare hostname, derived from instance if empty
    severity: str = "warning"
    summary: str = ""
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    fingerprint: str = ""
    starts_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_post_init(self, __context) -> None:  # noqa: D401
        if not self.host and self.instance:
            self.host = self.instance.split(":")[0]
        if not self.fingerprint:
            self.fingerprint = f"{self.alertname}/{self.instance}"


class Runbook(BaseModel):
    id: str
    description: str
    matches: list[str]
    command: str
    verify: str | None = None
    rollback: str | None = None
    reversible: bool = False        # default to the cautious value if someone forgets the field
    timeout: int = 60

    _selectors: list[Selector] = PrivateAttr(default_factory=list)

    def model_post_init(self, __context) -> None:  # noqa: D401
        # Parse once, at load. A bad selector raises here, so a broken runbook file never loads.
        self._selectors = [parse_selector(m) for m in self.matches]

    def applies_to(self, alert: "Alert | str") -> bool:
        """Does this runbook claim the alert? `matches` entries are ORed; see matchers.py."""
        if isinstance(alert, str):
            view = {"alertname": alert}
        else:
            view = label_view(alert.alertname, alert.labels, instance=alert.instance,
                              host=alert.host, severity=alert.severity)
        return any(s.ok(view) for s in self._selectors)


class Triage(BaseModel):
    """What the model concluded. It picks a runbook id; it never writes a command."""

    runbook_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    blast_radius: Literal["single-host", "single-service", "site", "unknown"] = "unknown"


Decision = Literal[
    "executed",             # ran, verified
    "executed-rolled-back", # ran, verify failed, rollback ran
    "dry-run",              # would have run; policy.dry_run is on
    "needs-approval",       # irreversible; parked for a human
    "blocked-policy",       # a hard gate said no (label, cap, confidence)
    "escalated",            # model chose escalate-to-human or no runbook fit
    "error",
]


class AuditRecord(BaseModel):
    """One line per run. This is the thing you grep at 2am."""

    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: str
    alert: Alert
    triage: Triage | None = None
    decision: Decision
    runbook_id: str | None = None
    rendered_command: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str = ""
    duration_ms: int = 0
