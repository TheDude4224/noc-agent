"""Config loading. YAML in, pydantic out, nothing clever."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

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


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8088


class Config(BaseModel):
    llm: LLMConfig = LLMConfig()
    policy: PolicyConfig = PolicyConfig()
    runbooks_file: str = "runbooks/runbooks.yaml"
    audit: AuditConfig = AuditConfig()
    notify: NotifyConfig = NotifyConfig()
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
