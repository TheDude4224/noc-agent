"""Config loading. YAML in, pydantic out, nothing clever."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Runbook


class LLMConfig(BaseModel):
    provider: str = "openai"           # openai | fake
    base_url: str = "http://localhost:11434/v1"
    model: str = "llama3.1:8b"
    api_key_env: str = "NOC_LLM_API_KEY"
    timeout_seconds: int = 60
    fake_responses_file: str = "examples/fake_llm.yaml"

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") or "not-needed"


class PolicyConfig(BaseModel):
    dry_run: bool = True
    require_approval_for_irreversible: bool = True
    max_actions_per_alert: int = 2
    max_actions_per_hour: int = 10
    min_confidence_to_act: float = 0.75
    never_automate_labels: list[str] = Field(default_factory=list)


class AuditConfig(BaseModel):
    path: str = "audit.jsonl"


class NotifyConfig(BaseModel):
    webhook_url: str = ""
    announce: bool = True              # post intent + evidence + impact BEFORE executing
    require_announce: bool = False     # refuse to act if that announcement is not delivered (2xx)


class WriterConfig(BaseModel):
    """The diagnosis writer (writer.py). Off unless a provider is set. Unknown keys are an error:
    a silently ignored key here means write-ups quietly stop (or never start)."""
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _yaml_on_is_true(cls, data):
        # YAML 1.1 reads a bare `on:` key as the boolean True. Name the trap instead of ignoring it.
        if isinstance(data, dict) and (True in data or "on" in data):
            raise ValueError("writer: use `decisions:` (a bare `on:` key is the boolean true in YAML)")
        return data

    provider: str = "none"             # none | claude-cli | anthropic-api | openai | fake
    model: str = "claude-haiku-4-5"
    credential_env: str = ""           # default per provider: CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY / NOC_WRITER_API_KEY
    base_url: str = "http://localhost:11434/v1"   # openai provider only
    claude_bin: str = "claude"         # claude-cli provider only
    decisions: list[str] = Field(default_factory=lambda: [
        "escalated", "needs-approval", "blocked-policy", "error", "executed-rolled-back"])
    max_words: int = 120
    timeout_seconds: int = 120
    dedupe_minutes: int = 360          # same alert + decision + runbook: one write-up per window
    max_per_hour: int = 20
    path: str = "diagnoses.jsonl"
    fake_text: str = "fake diagnosis"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8088


class Config(BaseModel):
    llm: LLMConfig = LLMConfig()
    policy: PolicyConfig = PolicyConfig()
    runbooks_file: str = "runbooks/runbooks.yaml"
    audit: AuditConfig = AuditConfig()
    notify: NotifyConfig = NotifyConfig()
    writer: WriterConfig = WriterConfig()
    server: ServerConfig = ServerConfig()
    approvals_dir: str = "approvals"


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        p = Path("config.example.yaml")
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)


def load_runbooks(path: str | Path) -> list[Runbook]:
    with Path(path).open() as f:
        raw = yaml.safe_load(f) or []
    books = [Runbook.model_validate(r) for r in raw]
    ids = [b.id for b in books]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate runbook ids: {sorted(dupes)}")
    return books
