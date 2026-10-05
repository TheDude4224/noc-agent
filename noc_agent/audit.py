"""Append-only JSONL audit log. One line per run, flushed immediately."""

from __future__ import annotations

import json
from pathlib import Path

from .models import AuditRecord


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rec: AuditRecord) -> None:
        line = rec.model_dump_json()
        with self.path.open("a") as f:
            f.write(line + "\n")
            f.flush()

    def tail(self, n: int = 20) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-n:]
        return [AuditRecord.model_validate(json.loads(l)) for l in lines if l.strip()]
