"""The writer: a strong model explains, in plain words, what a human is being handed.

The local triage model picks a runbook from a menu; it is fast, cheap and on-prem. The
writer is separate and optional. For runs that land on a person (escalated, needs
approval, blocked, error, rolled back) it writes a short diagnosis from the evidence the
engine already has: the alert, the triage reasoning, the policy reason, the runbook and
its output. It never chooses or runs anything; its text is advice for a human.

It runs on a single background worker, so an alert is never held up by a slow write-up,
and it is deduplicated and rate-capped, so a flapping alert does not turn into a bill.

Providers (writer.provider):
  none           off (default)
  claude-cli     the Claude Code CLI, headless (`claude -p`), no tools; auth from
                 CLAUDE_CODE_OAUTH_TOKEN (a Claude subscription) or ANTHROPIC_API_KEY
  anthropic-api  the Anthropic Messages API via the `anthropic` SDK
                 (pip install 'noc-agent[anthropic]'); key from ANTHROPIC_API_KEY
  openai         any OpenAI-compatible endpoint, e.g. a local model on Ollama/vLLM
  fake           fixed text, for tests and demos
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import WriterConfig
from .metrics import METRICS
from .models import AuditRecord

SYSTEM_PROMPT = """You write the diagnosis an on-call engineer reads when an automated NOC hands them an alert.
Use only the evidence given. Do not invent hosts, values, history or causes; if the evidence is thin, say so.
Plain text, no headings, no markdown, at most {max_words} words, in this order:
what is wrong (cite the evidence), the most likely cause, and the one next step a human should take.
The NOC already decided what it would and would not do; explain that decision, do not overrule it."""

_DEFAULT_CRED_ENV = {"claude-cli": "CLAUDE_CODE_OAUTH_TOKEN", "anthropic-api": "ANTHROPIC_API_KEY",
                     "openai": "NOC_WRITER_API_KEY"}


def build_prompt(rec: AuditRecord, max_chars: int = 1500) -> str:
    a = rec.alert
    lines = [
        "ALERT",
        f"  name: {a.alertname}",
        f"  severity: {a.severity}",
        f"  host: {a.host or a.instance or '(none)'}",
        f"  summary: {a.summary}",
        f"  description: {a.description}",
        "  labels: " + ", ".join(f"{k}={v}" for k, v in sorted(a.labels.items())),
        f"  firing since: {a.starts_at.isoformat()}",
        "",
        "WHAT THE NOC DID",
        f"  decision: {rec.decision}",
        f"  reason: {rec.reason}",
    ]
    if rec.triage:
        lines += [f"  triage choice: {rec.triage.runbook_id} (confidence {rec.triage.confidence:.2f}, "
                  f"blast radius {rec.triage.blast_radius})",
                  f"  triage reasoning: {rec.triage.reasoning}"]
    if rec.runbook_id and rec.runbook_id != "escalate-to-human":
        lines.append(f"  runbook: {rec.runbook_id}" + (f" ({rec.intent})" if rec.intent else ""))
    if rec.rendered_command:
        lines.append(f"  command: {rec.rendered_command}")
    if rec.exit_code is not None:
        lines.append(f"  exit code: {rec.exit_code}")
    if rec.stdout.strip():
        lines.append("  stdout (tail): " + rec.stdout.strip()[-max_chars:])
    if rec.stderr.strip():
        lines.append("  stderr (tail): " + rec.stderr.strip()[-max_chars:])
    return "\n".join(lines)


class Writer:
    def __init__(self, cfg: WriterConfig, on_written=None):
        self.cfg = cfg
        self.on_written = on_written            # callback(rec, text) -> None, e.g. post to the webhook
        self.enabled = cfg.provider != "none"
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="noc-writer") if self.enabled else None
        self._lock = threading.Lock()
        self._recent: dict[str, float] = {}     # dedupe key -> last written
        self._hour: list[float] = []
        self.path = Path(cfg.path)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if cfg.provider not in ("claude-cli", "anthropic-api", "openai", "fake"):
                raise ValueError(f"unknown writer.provider {cfg.provider!r}")

    # -- intake ------------------------------------------------------------

    def wants(self, rec: AuditRecord) -> bool:
        return self.enabled and rec.decision in self.cfg.decisions

    def submit(self, rec: AuditRecord) -> Future | None:
        """Queue a write-up for this run. Returns None when skipped (off, dedupe, cap)."""
        if not self.wants(rec):
            return None
        key = f"{rec.alert.fingerprint}|{rec.decision}|{rec.runbook_id}"
        now = time.time()
        with self._lock:
            if now - self._recent.get(key, 0) < self.cfg.dedupe_minutes * 60:
                METRICS.inc("noc_writer_skipped_total", {"reason": "dedupe"})
                return None
            self._hour = [t for t in self._hour if t > now - 3600]
            if len(self._hour) >= self.cfg.max_per_hour:
                METRICS.inc("noc_writer_skipped_total", {"reason": "hourly-cap"})
                return None
            self._recent[key] = now
            self._hour.append(now)
        assert self._pool is not None
        return self._pool.submit(self._run, rec)

    def close(self, wait: bool = True) -> None:
        if self._pool:
            self._pool.shutdown(wait=wait)

    # -- the work ------------------------------------------------------------

    def _run(self, rec: AuditRecord) -> str | None:
        labels = {"provider": self.cfg.provider}
        METRICS.inc("noc_writer_requests_total", labels)
        t0 = time.time()
        try:
            text = self.write_text(build_prompt(rec)).strip()
            if not text:
                raise ValueError("empty write-up")
        except Exception as e:  # noqa: BLE001 - a failed write-up is logged, never fatal
            METRICS.inc("noc_writer_errors_total", labels)
            print(f"[writer] {self.cfg.provider} failed for {rec.run_id}: {type(e).__name__}: {e}", file=sys.stderr)
            return None
        METRICS.observe("noc_writer_latency_seconds", time.time() - t0)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": rec.run_id,
                 "alertname": rec.alert.alertname, "decision": rec.decision,
                 "provider": self.cfg.provider, "model": self.cfg.model, "text": text}
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        if self.on_written:
            try:
                self.on_written(rec, text)
            except Exception as e:  # noqa: BLE001
                print(f"[writer] delivery failed for {rec.run_id}: {e}", file=sys.stderr)
        return text

    def get(self, run_id: str) -> dict | None:
        if not self.path.exists():
            return None
        for line in reversed(self.path.read_text().splitlines()):
            if line.strip():
                d = json.loads(line)
                if d.get("run_id") == run_id:
                    return d
        return None

    # -- providers -------------------------------------------------------------

    def _system(self) -> str:
        return SYSTEM_PROMPT.format(max_words=self.cfg.max_words)

    def _cred(self) -> str:
        env = self.cfg.credential_env or _DEFAULT_CRED_ENV.get(self.cfg.provider, "")
        return os.environ.get(env, "") if env else ""

    def write_text(self, prompt: str) -> str:
        p = self.cfg.provider
        if p == "fake":
            return self.cfg.fake_text
        if p == "claude-cli":
            return self._claude_cli(prompt)
        if p == "anthropic-api":
            return self._anthropic_api(prompt)
        return self._openai(prompt)

    def _claude_cli(self, prompt: str) -> str:
        """Headless Claude Code with every tool disabled: it can read the prompt and answer, nothing else.
        Not --bare: bare mode ignores OAuth, and tenant zero authenticates with a subscription token."""
        exe = shutil.which(self.cfg.claude_bin) or self.cfg.claude_bin
        cmd = [exe, "-p", "--model", self.cfg.model, "--tools", "", "--strict-mcp-config",
               "--no-session-persistence", "--output-format", "text", "--system-prompt", self._system()]
        with tempfile.TemporaryDirectory(prefix="noc-writer-") as home:
            # A throwaway HOME: no user settings, memory, plugins or CLAUDE.md from the host leak in.
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
            cred_env = self.cfg.credential_env or _DEFAULT_CRED_ENV["claude-cli"]
            if os.environ.get(cred_env):
                env["CLAUDE_CODE_OAUTH_TOKEN" if cred_env != "ANTHROPIC_API_KEY" else "ANTHROPIC_API_KEY"] = os.environ[cred_env]
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=self.cfg.timeout_seconds, env=env, cwd=home)
        if p.returncode != 0:
            raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}")
        return p.stdout

    def _anthropic_api(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on the extra being installed
            raise RuntimeError("writer.provider anthropic-api needs: pip install 'noc-agent[anthropic]'") from e
        client = anthropic.Anthropic(api_key=self._cred() or None, timeout=float(self.cfg.timeout_seconds), max_retries=2)
        resp = client.messages.create(
            model=self.cfg.model,
            max_tokens=1024,
            system=self._system(),
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined to write this diagnosis")
        return "".join(b.text for b in resp.content if b.type == "text")

    def _openai(self, prompt: str) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        body = {"model": self.cfg.model, "temperature": 0,
                "messages": [{"role": "system", "content": self._system()}, {"role": "user", "content": prompt}]}
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {self._cred() or 'not-needed'}"},
                       timeout=self.cfg.timeout_seconds)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
