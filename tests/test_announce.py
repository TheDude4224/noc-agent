"""Say what it's going to do, do what it said: announce before executing, report after."""

import json
from pathlib import Path

import pytest
import yaml

from noc_agent import notify
from noc_agent.agent import Agent
from noc_agent.config import Config
from noc_agent.executor import render_text
from noc_agent.models import Alert

ROOT = Path(__file__).resolve().parent.parent


class Hook:
    """Stands in for httpx.post; records every webhook body, in order."""

    def __init__(self, status=204, fail_stage=None):
        self.status, self.fail_stage, self.posts = status, fail_stage, []

    def __call__(self, url, json=None, timeout=None):  # noqa: A002 - mirrors httpx.post
        self.posts.append(json)
        if self.fail_stage == json["stage"]:
            raise OSError("bridge down")

        class R:
            status_code = self.status if json["stage"] == "before" else 204
        return R()


def make_agent(tmp_path, monkeypatch, hook, *, dry_run=False, require=False, announce=True, marker=None):
    raw = yaml.safe_load((ROOT / "examples" / "config.live-demo.yaml").read_text())
    raw["policy"]["dry_run"] = dry_run
    raw["audit"]["path"] = str(tmp_path / "audit.jsonl")
    raw["approvals_dir"] = str(tmp_path / "approvals")
    raw["llm"]["fake_responses_file"] = str(ROOT / "examples" / "fake_llm.yaml")
    books = yaml.safe_load((ROOT / "examples" / "runbooks.local.yaml").read_text())
    for b in books:
        if b["id"] == "restart-service":
            b["intent"] = "restart {label.service} on {host}"
            b["impact"] = "a few seconds of downtime for {label.service}"
            if marker:  # the command leaves a mark so ordering can be checked against the webhook log
                b["command"] = f"echo ran >> {marker}"
    rb = tmp_path / "runbooks.yaml"
    rb.write_text(yaml.safe_dump(books))
    raw["runbooks_file"] = str(rb)
    raw["notify"] = {"webhook_url": "http://bridge/notify", "announce": announce, "require_announce": require}
    monkeypatch.setattr(notify.httpx, "post", hook)
    return Agent(Config.model_validate(raw))


def service_down(n=1):
    return Alert(alertname="ServiceDown", instance=f"web-0{n}:9100", labels={"service": "nginx"})


def test_announces_before_executing_then_reports(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    seen_before_run = []

    hook = Hook()
    orig = hook.__call__

    def spy(url, json=None, timeout=None):
        seen_before_run.append((json["stage"], marker.exists()))
        return orig(url, json=json, timeout=timeout)
    agent = make_agent(tmp_path, monkeypatch, spy, marker=str(marker))
    rec = agent.handle(service_down())
    assert rec.decision == "executed"
    assert seen_before_run == [("before", False), ("after", True)]   # said it, then did it
    before, after = hook.posts
    assert before["text"].startswith("[GOING TO] ServiceDown on web-01")
    assert "will: restart nginx on web-01" in before["text"]
    assert "impact: a few seconds of downtime for nginx" in before["text"]
    assert "why: " in before["text"] and f"run={rec.run_id}" in before["text"]
    assert after["stage"] == "after" and after["decision"] == "executed"
    assert "did: restart nginx on web-01" in after["text"]
    assert rec.intent == "restart nginx on web-01"


@pytest.mark.parametrize("dry", [True])
def test_no_announcement_when_nothing_executes(tmp_path, monkeypatch, dry):
    hook = Hook()
    agent = make_agent(tmp_path, monkeypatch, hook, dry_run=dry)
    alerts = [Alert.model_validate(a) for a in json.loads((ROOT / "examples" / "alerts.json").read_text())]
    for a in alerts:
        agent.handle(a)
    assert [p["stage"] for p in hook.posts] == ["after"] * len(alerts)


def test_failed_announcement_still_acts_by_default(tmp_path, monkeypatch):
    hook = Hook(fail_stage="before")
    rec = make_agent(tmp_path, monkeypatch, hook).handle(service_down())
    assert rec.decision == "executed"


@pytest.mark.parametrize("hook", [Hook(fail_stage="before"), Hook(status=502)])
def test_require_announce_blocks_a_silent_action(tmp_path, monkeypatch, hook):
    marker = tmp_path / "ran"
    agent = make_agent(tmp_path, monkeypatch, hook, require=True, marker=str(marker))
    rec = agent.handle(service_down())
    assert rec.decision == "blocked-policy" and "not acting silently" in rec.reason
    assert rec.exit_code is None and not marker.exists()
    # it was not counted against the caps either
    assert agent.policy._executions_last_hour() == 0


def test_announce_off_sends_only_outcomes(tmp_path, monkeypatch):
    hook = Hook()
    make_agent(tmp_path, monkeypatch, hook, announce=False).handle(service_down())
    assert [p["stage"] for p in hook.posts] == ["after"]


def test_require_announce_without_webhook_is_a_config_error(tmp_path, monkeypatch):
    raw = yaml.safe_load((ROOT / "examples" / "config.live-demo.yaml").read_text())
    raw["audit"]["path"] = str(tmp_path / "a.jsonl")
    raw["approvals_dir"] = str(tmp_path / "ap")
    raw["llm"]["fake_responses_file"] = str(ROOT / "examples" / "fake_llm.yaml")
    raw["runbooks_file"] = str(ROOT / "examples" / "runbooks.local.yaml")
    raw["notify"] = {"webhook_url": "", "require_announce": True}
    with pytest.raises(ValueError, match="require_announce"):
        Agent(Config.model_validate(raw))


def test_approved_run_is_announced_and_stays_parked_if_it_cannot_be(tmp_path, monkeypatch):
    hook = Hook()
    agent = make_agent(tmp_path, monkeypatch, hook)
    wan = Alert(alertname="PrimaryWANDown", instance="gw-01:9100")
    parked = agent.handle(wan)
    assert parked.decision == "needs-approval"
    assert [p["stage"] for p in hook.posts] == ["after"]

    agent.notifier.webhook_url = "http://bridge/notify"
    agent.cfg.notify.require_announce = True
    monkeypatch.setattr(notify.httpx, "post", Hook(fail_stage="before"))
    rec = agent.approve(parked.run_id)
    assert rec.decision == "blocked-policy" and "still parked" in rec.reason
    assert (tmp_path / "approvals" / f"{parked.run_id}.json").exists()

    ok = Hook()
    monkeypatch.setattr(notify.httpx, "post", ok)
    rec = agent.approve(parked.run_id)
    assert rec.decision == "executed"
    assert [p["stage"] for p in ok.posts] == ["before", "after"]
    assert "a human approved parked run" in ok.posts[0]["text"]
    assert not (tmp_path / "approvals" / f"{parked.run_id}.json").exists()


def test_render_text_is_unquoted_and_never_fails():
    a = Alert(alertname="X", instance="h:1", labels={"svc": "a b"})
    assert render_text("restart {label.svc} on {host} ({label.missing})", a) == "restart a b on h (?)"
