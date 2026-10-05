"""End-to-end through the loop with the fake model and local runbooks."""

import json
from pathlib import Path

import yaml

from noc_agent.agent import Agent
from noc_agent.config import Config
from noc_agent.models import Alert
from noc_agent.server import normalize_alertmanager
from noc_agent.triage import Triager, _extract_json
from noc_agent.config import LLMConfig

ROOT = Path(__file__).resolve().parent.parent


def make_agent(tmp_path, dry_run=False):
    raw = yaml.safe_load((ROOT / "examples" / "config.live-demo.yaml").read_text())
    raw["policy"]["dry_run"] = dry_run
    raw["audit"]["path"] = str(tmp_path / "audit.jsonl")
    raw["approvals_dir"] = str(tmp_path / "approvals")
    raw["llm"]["fake_responses_file"] = str(ROOT / "examples" / "fake_llm.yaml")
    raw["runbooks_file"] = str(ROOT / "examples" / "runbooks.local.yaml")
    return Agent(Config.model_validate(raw))


def load_alerts():
    return [Alert.model_validate(a) for a in json.loads((ROOT / "examples" / "alerts.json").read_text())]


def test_demo_decisions_match_expectations(tmp_path):
    agent = make_agent(tmp_path)
    decisions = [agent.handle(a).decision for a in load_alerts()]
    assert decisions == [
        "executed",              # ServiceDown -> restart, verified
        "error",                 # DiskSpaceLow -> verify fails, no rollback
        "needs-approval",        # PrimaryWANDown -> irreversible
        "blocked-policy",        # HighErrorRate on tier=payments
        "escalated",             # WeirdCertThing -> model punted
        "executed",              # ServiceDown #2
        "blocked-policy",        # ServiceDown #3 -> per-alert cap
    ]


def test_audit_has_one_line_per_run(tmp_path):
    agent = make_agent(tmp_path)
    alerts = load_alerts()
    for a in alerts:
        agent.handle(a)
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == len(alerts)
    rec = json.loads(lines[0])
    assert rec["decision"] == "executed" and rec["rendered_command"].startswith("echo 'restart")


def test_dry_run_executes_nothing(tmp_path):
    agent = make_agent(tmp_path, dry_run=True)
    recs = [agent.handle(a) for a in load_alerts()]
    assert all(r.exit_code is None for r in recs)
    assert {r.decision for r in recs} <= {"dry-run", "needs-approval", "blocked-policy", "escalated"}


def test_approve_runs_parked_action(tmp_path):
    agent = make_agent(tmp_path)
    wan = [a for a in load_alerts() if a.alertname == "PrimaryWANDown"][0]
    parked = agent.handle(wan)
    assert parked.decision == "needs-approval"
    assert (tmp_path / "approvals" / f"{parked.run_id}.json").exists()
    done = agent.approve(parked.run_id)
    assert done.decision == "executed" and "FAILOVER" in done.stdout
    assert not (tmp_path / "approvals" / f"{parked.run_id}.json").exists()


def test_model_choosing_off_menu_id_is_escalated(tmp_path):
    t = Triager(LLMConfig(provider="fake", fake_responses_file=str(tmp_path / "f.yaml")))
    t._fake = {"X": {"runbook_id": "rm-rf-everything", "confidence": 0.99, "reasoning": "trust me"}}
    from noc_agent.models import Runbook
    menu = [Runbook(id="escalate-to-human", description="", matches=["*"], command="true", reversible=True)]
    out = t.triage(Alert(alertname="X"), menu)
    assert out.runbook_id == "escalate-to-human" and out.confidence == 0.0


def test_bad_json_from_model_is_escalated_not_crash(tmp_path):
    t = Triager(LLMConfig(provider="fake", fake_responses_file=str(tmp_path / "f.yaml")))
    t._fake = {}
    out = t.triage(Alert(alertname="X"), [])
    assert out.runbook_id == "escalate-to-human"


def test_extract_json_tolerates_fences():
    assert _extract_json('```json\n{"a": 1}\n```')["a"] == 1


def test_alertmanager_normalization_skips_resolved():
    payload = {"alerts": [
        {"status": "firing", "labels": {"alertname": "ServiceDown", "instance": "h:9100", "service": "nginx"},
         "annotations": {"summary": "s"}, "fingerprint": "abc"},
        {"status": "resolved", "labels": {"alertname": "ServiceDown", "instance": "h:9100"}},
    ]}
    out = normalize_alertmanager(payload)
    assert len(out) == 1
    assert out[0].alertname == "ServiceDown" and out[0].host == "h" and out[0].labels["service"] == "nginx"
    assert "alertname" not in out[0].labels
