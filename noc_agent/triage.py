"""Triage: hand the model the alert and the runbook menu, get back a choice.

The model's output is a runbook id, a confidence, and reasoning. It cannot invent
a command. If it returns anything that isn't on the menu, we treat that as
"escalate-to-human". Bad JSON is also an escalation, never a crash.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
import yaml

from .config import LLMConfig
from .metrics import METRICS
from .models import Alert, Runbook, Triage

SYSTEM_PROMPT = """You are the triage step of a Network Operations Center agent.
You will be given one firing alert and a numbered menu of runbooks.
Pick exactly one runbook from the menu and return its id string (for example "restart-service"),
never its number. Never invent an id or a command.
If nothing on the menu clearly fits, or the situation is ambiguous, choose "escalate-to-human".
A miss is cheaper than a wrong action. Lower your confidence when the alert is vague.

Reply with ONLY a JSON object, no prose, in this shape:
{"runbook_id": "<id from menu>", "confidence": 0.0-1.0, "reasoning": "<two sentences>", "blast_radius": "single-host|single-service|site|unknown"}
"""


def build_user_prompt(alert: Alert, menu: list[Runbook]) -> str:
    lines = [
        "ALERT",
        f"  name: {alert.alertname}",
        f"  severity: {alert.severity}",
        f"  host: {alert.host or '(none)'}",
        f"  summary: {alert.summary}",
        f"  description: {alert.description}",
        "  labels: " + ", ".join(f"{k}={v}" for k, v in sorted(alert.labels.items())),
        "",
        "RUNBOOK MENU (choose one id)",
    ]
    for i, rb in enumerate(menu, 1):
        tag = "auto-ok" if rb.reversible else "needs-human-approval"
        lines.append(f"  {i}. {rb.id} [{tag}] - {rb.description}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Models wrap JSON in fences or prose more often than you'd like."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))


class Triager:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._fake: dict[str, dict] | None = None
        if cfg.provider == "fake":
            p = Path(cfg.fake_responses_file)
            self._fake = yaml.safe_load(p.read_text()) if p.exists() else {}

    def triage(self, alert: Alert, menu: list[Runbook]) -> Triage:
        allowed = {rb.id for rb in menu}
        try:
            raw = self._call(alert, menu)
            data = _extract_json(raw)
            t = Triage.model_validate(data)
        except Exception as e:  # noqa: BLE001 - any failure here is an escalation, not a crash
            return Triage(
                runbook_id="escalate-to-human",
                confidence=0.0,
                reasoning=f"triage failed: {type(e).__name__}: {e}",
                blast_radius="unknown",
            )
        # Small local models like to answer with the menu number instead of the id.
        # Map "3" (or "#3", "3.") to the third entry rather than throwing the triage away.
        if t.runbook_id not in allowed:
            digits = t.runbook_id.strip().lstrip("#").rstrip(".")
            if digits.isdigit() and 1 <= int(digits) <= len(menu):
                t = t.model_copy(update={"runbook_id": menu[int(digits) - 1].id})
        if t.runbook_id not in allowed:
            return Triage(
                runbook_id="escalate-to-human",
                confidence=0.0,
                reasoning=f"model chose '{t.runbook_id}', which is not on the menu; escalating",
                blast_radius=t.blast_radius,
            )
        return t

    # -- providers ---------------------------------------------------------

    def _call(self, alert: Alert, menu: list[Runbook]) -> str:
        METRICS.inc("noc_llm_requests_total")
        t0 = time.time()
        try:
            if self.cfg.provider == "fake":
                out = self._call_fake(alert)
            else:
                out = self._call_openai(alert, menu)
        except Exception:
            METRICS.inc("noc_llm_errors_total")
            raise
        METRICS.observe("noc_llm_latency_seconds", time.time() - t0)
        METRICS.set("noc_llm_last_success_timestamp_seconds", time.time())
        return out

    def _call_fake(self, alert: Alert) -> str:
        assert self._fake is not None
        resp = self._fake.get(alert.alertname) or self._fake.get("*")
        if resp is None:
            raise ValueError(f"no fake response for {alert.alertname}")
        return json.dumps(resp)

    def _call_openai(self, alert: Alert, menu: list[Runbook]) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.cfg.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(alert, menu)},
            ],
        }
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
            r = client.post(url, json=body, headers=headers)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
