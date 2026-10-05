"""The gates are the product. Every one gets a test."""

from noc_agent.config import PolicyConfig
from noc_agent.models import Alert, Runbook, Triage
from noc_agent.policy import Policy


def mk_alert(name="ServiceDown", labels=None):
    return Alert(alertname=name, instance="h1:9100", labels=labels or {"service": "nginx"})


def mk_rb(rid="restart-service", matches=("ServiceDown",), reversible=True):
    return Runbook(id=rid, description="x", matches=list(matches), command="true", reversible=reversible)


def mk_triage(rid="restart-service", conf=0.9):
    return Triage(runbook_id=rid, confidence=conf, reasoning="because")


def policy(**kw):
    base = dict(dry_run=False, require_approval_for_irreversible=True,
                max_actions_per_alert=2, max_actions_per_hour=10,
                min_confidence_to_act=0.75, never_automate_labels=["tier=payments"])
    base.update(kw)
    return Policy(PolicyConfig(**base))


def test_all_gates_pass():
    v = policy().evaluate(mk_alert(), mk_triage(), mk_rb())
    assert v.allowed and v.outcome == "execute"


def test_dry_run_is_last_gate_and_blocks():
    v = policy(dry_run=True).evaluate(mk_alert(), mk_triage(), mk_rb())
    assert not v.allowed and v.outcome == "dry-run"


def test_escalate_when_model_punts():
    v = policy().evaluate(mk_alert(), mk_triage("escalate-to-human"), mk_rb("escalate-to-human", ("*",)))
    assert v.outcome == "escalated"


def test_escalate_when_runbook_missing():
    v = policy().evaluate(mk_alert(), mk_triage("does-not-exist"), None)
    assert v.outcome == "escalated"


def test_never_automate_label_blocks_even_with_high_confidence():
    a = mk_alert(labels={"service": "pay", "tier": "payments"})
    v = policy().evaluate(a, mk_triage(conf=0.99), mk_rb())
    assert v.outcome == "blocked-policy" and "never-automate" in v.reason


def test_runbook_must_match_alert_not_just_model_choice():
    v = policy().evaluate(mk_alert("DiskSpaceLow"), mk_triage(), mk_rb(matches=("ServiceDown",)))
    assert v.outcome == "blocked-policy" and "does not match" in v.reason


def test_confidence_floor():
    v = policy().evaluate(mk_alert(), mk_triage(conf=0.5), mk_rb())
    assert v.outcome == "blocked-policy" and "confidence" in v.reason


def test_irreversible_always_needs_approval():
    v = policy().evaluate(mk_alert(), mk_triage(), mk_rb(reversible=False))
    assert v.outcome == "needs-approval"


def test_per_alert_cap():
    p = policy(max_actions_per_alert=1)
    a = mk_alert()
    assert p.evaluate(a, mk_triage(), mk_rb()).outcome == "execute"
    p.record_execution(a)
    v = p.evaluate(a, mk_triage(), mk_rb())
    assert v.outcome == "blocked-policy" and "per-alert cap" in v.reason


def test_hourly_cap():
    p = policy(max_actions_per_hour=2)
    for i in range(2):
        p.record_execution(mk_alert(name=f"A{i}"))
    v = p.evaluate(mk_alert(name="A9"), mk_triage(), mk_rb(matches=("A9",)))
    assert v.outcome == "blocked-policy" and "hourly cap" in v.reason


def test_gate_order_label_beats_confidence():
    """A never-automate label should be reported even when confidence would also fail."""
    a = mk_alert(labels={"tier": "payments"})
    v = policy().evaluate(a, mk_triage(conf=0.1), mk_rb())
    assert "never-automate" in v.reason
