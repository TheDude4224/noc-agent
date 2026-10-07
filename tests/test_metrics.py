"""GET /metrics: the numbers a monitoring stack needs to watch the watcher."""

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from noc_agent.agent import Agent
from noc_agent.config import Config, LLMConfig
from noc_agent.metrics import METRICS, Metrics
from noc_agent.models import Alert
from noc_agent.server import create_app
from noc_agent.triage import Triager

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


def test_registry_renders_prometheus_text():
    m = Metrics()
    m.describe("x_total", "counter", "things")
    m.describe("lat_seconds", "summary", "latency")
    m.inc("x_total", {"decision": 'a"b'})
    m.inc("x_total", {"decision": "c"}, by=2)
    m.observe("lat_seconds", 0.25)
    m.observe("lat_seconds", 0.75)
    text = m.render()
    assert "# HELP x_total things\n# TYPE x_total counter\n" in text
    assert 'x_total{decision="a\\"b"} 1\n' in text and 'x_total{decision="c"} 2\n' in text
    # _sum and _count sit under one family header
    assert "# TYPE lat_seconds summary\nlat_seconds_count 2\nlat_seconds_sum 1.0\n" in text
    assert text.count("# TYPE") == 2


def test_runs_and_model_calls_are_counted_end_to_end(tmp_path):
    METRICS.reset()
    agent = make_agent(tmp_path)
    alerts = load_alerts()
    for a in alerts:
        agent.handle(a)
    by = {d: METRICS.get("noc_runs_total", {"decision": d})
          for d in ("executed", "error", "needs-approval", "blocked-policy", "escalated", "dry-run")}
    assert by == {"executed": 2, "error": 1, "needs-approval": 1, "blocked-policy": 2, "escalated": 1, "dry-run": 0}
    assert sum(by.values()) == len(alerts)
    assert METRICS.get("noc_run_duration_seconds_count") == len(alerts)
    assert METRICS.get("noc_llm_requests_total") == len(alerts)
    assert METRICS.get("noc_llm_errors_total") == 0
    assert METRICS.get("noc_llm_last_success_timestamp_seconds") > 0
    assert METRICS.get("noc_last_run_timestamp_seconds") > 0
    assert METRICS.get("noc_dry_run") == 0 and METRICS.get("noc_runbooks") == len(agent.runbooks)


def test_model_failure_is_an_llm_error(tmp_path):
    METRICS.reset()
    t = Triager(LLMConfig(provider="fake", fake_responses_file=str(tmp_path / "f.yaml")))
    t._fake = {}  # no response for anything -> the provider raises
    out = t.triage(Alert(alertname="X"), [])
    assert out.runbook_id == "escalate-to-human"
    assert METRICS.get("noc_llm_requests_total") == 1 and METRICS.get("noc_llm_errors_total") == 1
    assert METRICS.get("noc_llm_latency_seconds_count") == 0


def test_metrics_endpoint_counts_received_before_handling(tmp_path):
    METRICS.reset()
    agent = make_agent(tmp_path)
    client = TestClient(create_app(agent))
    payload = {"alerts": [
        {"status": "firing", "labels": {"alertname": "ServiceDown", "instance": "h:9100", "service": "nginx"},
         "annotations": {"summary": "s"}, "fingerprint": "abc"},
        {"status": "resolved", "labels": {"alertname": "ServiceDown", "instance": "h:9100"}},
    ]}
    assert client.post("/alertmanager", json=payload).status_code == 200
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "noc_alerts_received_total 1\n" in text          # resolved alerts are not counted
    assert 'noc_runs_total{decision="executed"} 1\n' in text
    assert 'noc_build_info{version="' in text
    assert "noc_dry_run 0\n" in text and "noc_approvals_pending 0\n" in text
    assert "# TYPE noc_run_duration_seconds summary" in text


def test_pending_approvals_gauge(tmp_path):
    METRICS.reset()
    agent = make_agent(tmp_path)
    wan = [a for a in load_alerts() if a.alertname == "PrimaryWANDown"][0]
    parked = agent.handle(wan)
    client = TestClient(create_app(agent))
    assert "noc_approvals_pending 1\n" in client.get("/metrics").text
    agent.approve(parked.run_id)
    assert "noc_approvals_pending 0\n" in client.get("/metrics").text
