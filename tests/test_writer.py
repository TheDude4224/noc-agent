"""The diagnosis writer: off by default, background, deduped, capped, and every provider's call shape."""

import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from noc_agent import notify, writer as writer_mod
from noc_agent.agent import Agent
from noc_agent.config import Config, WriterConfig
from noc_agent.models import Alert, AuditRecord, Triage
from noc_agent.server import create_app
from noc_agent.writer import Writer, build_prompt

ROOT = Path(__file__).resolve().parent.parent


def make_agent(tmp_path, monkeypatch, posts, **writer):
    raw = yaml.safe_load((ROOT / "examples" / "config.live-demo.yaml").read_text())
    raw["policy"]["dry_run"] = False
    raw["audit"]["path"] = str(tmp_path / "audit.jsonl")
    raw["approvals_dir"] = str(tmp_path / "approvals")
    raw["llm"]["fake_responses_file"] = str(ROOT / "examples" / "fake_llm.yaml")
    raw["runbooks_file"] = str(ROOT / "examples" / "runbooks.local.yaml")
    raw["notify"] = {"webhook_url": "http://bridge/notify"}
    raw["writer"] = {"provider": "fake", "fake_text": "the cert expired; renew it", "path": str(tmp_path / "diag.jsonl"), **writer}

    def post(url, json=None, timeout=None):  # noqa: A002
        posts.append(json)

        class R:
            status_code = 204
        return R()
    monkeypatch.setattr(notify.httpx, "post", post)
    return Agent(Config.model_validate(raw))


def rec(decision="escalated", **kw):
    a = Alert(alertname="WeirdCertThing", instance="web-01:443", summary="cert odd", labels={"job": "blackbox"})
    return AuditRecord(run_id=kw.pop("run_id", "r1"), alert=a, decision=decision,
                       triage=Triage(runbook_id="escalate-to-human", confidence=0.4, reasoning="nothing fits"),
                       reason="model punted", **kw)


def test_off_by_default(tmp_path):
    w = Writer(WriterConfig(path=str(tmp_path / "d.jsonl")))
    assert not w.enabled and w.submit(rec()) is None


def test_person_bound_runs_get_a_diagnosis_posted_and_stored(tmp_path, monkeypatch):
    posts = []
    agent = make_agent(tmp_path, monkeypatch, posts)
    alerts = [Alert.model_validate(a) for a in json.loads((ROOT / "examples" / "alerts.json").read_text())]
    recs = [agent.handle(a) for a in alerts]
    agent.close()
    want = {r.run_id for r in recs if r.decision in agent.cfg.writer.on}
    diag = [p for p in posts if p["stage"] == "diagnosis"]
    assert {p["run_id"] for p in diag} == want and want            # every person-bound run, nothing else
    assert all(p["text"].startswith("[DIAGNOSIS] ") and "the cert expired" in p["text"] for p in diag)
    by_run = {r.run_id: r.decision for r in recs}
    assert all(p["decision"] == by_run[p["run_id"]] for p in diag)   # a relay can route by decision
    executed = next(r for r in recs if r.decision == "executed")
    assert agent.writer.get(executed.run_id) is None
    some = next(iter(want))
    assert agent.writer.get(some)["text"] == "the cert expired; renew it"
    # the after-line for a run always goes out before its diagnosis
    order = [(p["run_id"], p["stage"]) for p in posts]
    assert order.index((some, "after")) < order.index((some, "diagnosis"))


def test_dedupe_and_hourly_cap(tmp_path):
    w = Writer(WriterConfig(provider="fake", path=str(tmp_path / "d.jsonl"), max_per_hour=2))
    assert w.submit(rec(run_id="a")) is not None
    assert w.submit(rec(run_id="b")) is None                      # same alert+decision: deduped
    other = rec(run_id="c", decision="error")
    assert w.submit(other) is not None
    third = rec(run_id="d", decision="blocked-policy")
    assert w.submit(third) is None                                # cap of 2 per hour
    w.close()
    assert [json.loads(l)["run_id"] for l in (tmp_path / "d.jsonl").read_text().splitlines()] == ["a", "c"]


def test_prompt_carries_the_evidence():
    r = rec(decision="error", runbook_id="restart-service", intent="restart nginx on web-01",
            rendered_command="ssh web-01 systemctl restart nginx", exit_code=1, stderr="Job failed")
    p = build_prompt(r)
    for s in ("WeirdCertThing", "job=blackbox", "decision: error", "model punted", "nothing fits",
              "restart nginx on web-01", "exit code: 1", "Job failed"):
        assert s in p


def _stub_claude(tmp_path, exit_code=0):
    log = tmp_path / "claude.log"
    exe = tmp_path / "claude"
    exe.write_text(f"""#!/bin/sh
python3 - "$@" <<'PY'
import json, os, sys
json.dump({{"argv": sys.argv[1:], "home": os.environ.get("HOME"), "cwd": os.getcwd(),
           "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), "api": os.environ.get("ANTHROPIC_API_KEY")}},
          open({str(log)!r}, "w"))
PY
cat > {tmp_path}/stdin.txt
echo "stub diagnosis"
exit {exit_code}
""")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, log


def test_claude_cli_runs_headless_with_no_tools_and_a_throwaway_home(tmp_path, monkeypatch):
    exe, log = _stub_claude(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    w = Writer(WriterConfig(provider="claude-cli", claude_bin=str(exe), path=str(tmp_path / "d.jsonl")))
    assert w._run(rec()) == "stub diagnosis"
    seen = json.loads(log.read_text())
    argv = seen["argv"]
    assert argv[0] == "-p" and "--bare" not in argv                 # bare mode would ignore OAuth
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert argv[argv.index("--tools") + 1] == ""                    # every tool disabled
    for f in ("--strict-mcp-config", "--no-session-persistence"):
        assert f in argv
    assert argv[argv.index("--output-format") + 1] == "text"
    assert seen["home"] != os.environ.get("HOME") and "noc-writer-" in seen["home"] and seen["cwd"] == seen["home"]
    assert seen["oauth"] == "tok-123" and seen["api"] is None         # only the configured credential crosses
    assert "WeirdCertThing" in (tmp_path / "stdin.txt").read_text()   # prompt on stdin, not argv
    assert not Path(seen["home"]).exists()                            # cleaned up


def test_claude_cli_failure_is_logged_not_stored(tmp_path, monkeypatch):
    exe, _ = _stub_claude(tmp_path, exit_code=3)
    w = Writer(WriterConfig(provider="claude-cli", claude_bin=str(exe), path=str(tmp_path / "d.jsonl")))
    assert w._run(rec()) is None and not (tmp_path / "d.jsonl").exists()


def test_anthropic_api_uses_the_sdk(tmp_path, monkeypatch):
    calls = {}

    class Messages:
        def create(self, **kw):
            calls["create"] = kw
            block = types.SimpleNamespace(type="text", text="api diagnosis")
            return types.SimpleNamespace(stop_reason="end_turn", content=[block])

    class Anthropic:
        def __init__(self, **kw):
            calls["client"] = kw
            self.messages = Messages()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    w = Writer(WriterConfig(provider="anthropic-api", path=str(tmp_path / "d.jsonl")))
    assert w._run(rec()) == "api diagnosis"
    assert calls["client"]["api_key"] == "sk-test"
    c = calls["create"]
    assert c["model"] == "claude-haiku-4-5" and "on-call engineer" in c["system"]
    assert c["messages"][0]["role"] == "user" and "WeirdCertThing" in c["messages"][0]["content"]


def test_anthropic_api_refusal_is_an_error(tmp_path, monkeypatch):
    class Anthropic:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=lambda **k: types.SimpleNamespace(stop_reason="refusal", content=[]))
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    w = Writer(WriterConfig(provider="anthropic-api", path=str(tmp_path / "d.jsonl")))
    assert w._run(rec()) is None


def test_openai_compatible_local_model(tmp_path, monkeypatch):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):  # noqa: A002
        seen.update(url=url, body=json)

        class R:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "local diagnosis"}}]}
        return R()
    monkeypatch.setattr(writer_mod.httpx, "post", post)
    w = Writer(WriterConfig(provider="openai", model="qwen3:8b", base_url="http://gpu:11434/v1/", path=str(tmp_path / "d.jsonl")))
    assert w._run(rec()) == "local diagnosis"
    assert seen["url"] == "http://gpu:11434/v1/chat/completions" and seen["body"]["model"] == "qwen3:8b"


def test_unknown_provider_fails_at_startup(tmp_path):
    with pytest.raises(ValueError, match="writer.provider"):
        Writer(WriterConfig(provider="gpt-magic", path=str(tmp_path / "d.jsonl")))


def test_diagnosis_endpoint(tmp_path, monkeypatch):
    posts = []
    agent = make_agent(tmp_path, monkeypatch, posts)
    r = agent.handle(Alert(alertname="WeirdCertThing", instance="web-01:443"))
    agent.writer.close()
    c = TestClient(create_app(agent))
    assert c.get(f"/diagnosis/{r.run_id}").json()["text"] == "the cert expired; renew it"
    assert c.get("/diagnosis/nope").status_code == 404
