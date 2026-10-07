"""Runbook selectors: alertname, label matchers, and loud failure on bad syntax."""

import pytest

from noc_agent.matchers import parse_selector
from noc_agent.models import Alert, Runbook


def rb(*matches):
    return Runbook(id="x", description="x", matches=list(matches), command="true")


def alert(name="GuestStopped", **labels):
    return Alert(alertname=name, instance=labels.pop("instance", "10.0.0.5:9100"), labels=labels)


def test_bare_alertname_and_star_keep_working():
    assert rb("GuestStopped").applies_to(alert())
    assert not rb("NodeDown").applies_to(alert())
    assert rb("*").applies_to(alert("Anything"))
    assert rb("GuestStopped").applies_to("GuestStopped")      # string form still accepted


def test_alertname_with_labels_is_and():
    r = rb('GuestStopped{node="OryahCloud-01", id=~"lxc/.*"}')
    assert r.applies_to(alert(node="OryahCloud-01", id="lxc/107"))
    assert not r.applies_to(alert(node="oryahcloud-02", id="lxc/107"))
    assert not r.applies_to(alert(node="OryahCloud-01", id="qemu/500"))
    assert not r.applies_to(alert("NodeDown", node="OryahCloud-01", id="lxc/107"))


def test_labels_only_entry_matches_any_alertname():
    r = rb('{origin="legacy-ct400", severity!="info"}')
    assert r.applies_to(alert("A", origin="legacy-ct400", severity="warning"))
    assert r.applies_to(alert("B", origin="legacy-ct400"))           # missing severity is "" != "info"
    assert not r.applies_to(alert("A", origin="legacy-ct400", severity="info"))
    assert not r.applies_to(alert("A", origin="obs"))


def test_entries_are_or():
    r = rb("NodeDown", '{job="pve"}')
    assert r.applies_to(alert("NodeDown"))
    assert r.applies_to(alert("Other", job="pve"))
    assert not r.applies_to(alert("Other", job="node"))


def test_regex_is_fully_anchored_and_negative_regex():
    assert not rb('{job=~"node"}').applies_to(alert(job="node_ct400"))
    assert rb('{job=~"node.*"}').applies_to(alert(job="node_ct400"))
    assert rb('{job!~"node.*"}').applies_to(alert(job="pve"))


def test_normalized_fields_are_matchable_but_labels_win():
    a = Alert(alertname="X", instance="10.0.0.45:9100", severity="critical")
    assert rb('{host="10.0.0.45"}').applies_to(a)
    assert rb('{severity="critical"}').applies_to(a)
    a2 = Alert(alertname="X", instance="10.0.0.45:9100", labels={"host": "ct400"})
    assert rb('{host="ct400"}').applies_to(a2)


def test_escaped_quote_and_comma_in_value():
    r = rb(r'{summary="a, \"b\""}')
    assert r.applies_to(alert(summary='a, "b"'))


@pytest.mark.parametrize("bad", ['{job=node}', '{job="x"', 'Foo{job=="x"}', '{="x"}', '{job=~"("}', '', '   '])
def test_bad_syntax_fails_at_load(bad):
    with pytest.raises(ValueError):
        rb(bad)


def test_policy_gate_uses_labels(tmp_path):
    """A runbook scoped by label must not be accepted for an alert outside that scope,
    even if the model picks it."""
    from noc_agent.config import PolicyConfig
    from noc_agent.models import Triage
    from noc_agent.policy import Policy
    r = Runbook(id="start-guest", description="x", matches=['GuestStopped{node="OryahCloud-01"}'],
                command="true", reversible=True)
    p = Policy(PolicyConfig(dry_run=False))
    t = Triage(runbook_id="start-guest", confidence=0.99, reasoning="r")
    assert p.evaluate(alert(node="OryahCloud-01"), t, r).outcome == "execute"
    v = p.evaluate(alert(node="oryahcloud-02"), t, r)
    assert v.outcome == "blocked-policy" and "does not match" in v.reason


def test_parse_selector_shapes():
    s = parse_selector('Foo{a="1",b!~"x|y"}')
    assert s.alertname == "Foo" and [m.op for m in s.matchers] == ["=", "!~"]
    assert parse_selector("*").alertname == "*"
