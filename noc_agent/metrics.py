"""Prometheus text-format metrics for the engine, without prometheus_client.

Exposed at GET /metrics so the monitoring stack can watch the watcher. An agent that is
down, whose model is unreachable, or that receives alerts but never finishes a run
would otherwise fail silently, and silent failure is the one thing a NOC must not do.

Everything goes through the module-level METRICS registry; agent.py, triage.py and
server.py update it. Counters only go up; a restart resets them, which Prometheus
handles with rate() / increase().
"""

from __future__ import annotations

import threading
import time

_LabelKey = tuple[tuple[str, str], ...]


def engine_version() -> str:
    try:
        from importlib.metadata import version

        return version("noc-agent")
    except Exception:  # noqa: BLE001 - not installed as a package (dev checkout)
        return "0.0.0+local"


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meta: dict[str, tuple[str, str]] = {}  # family -> (type, help)
        self._values: dict[tuple[str, _LabelKey], float] = {}

    def describe(self, name: str, typ: str, help_: str) -> None:
        self._meta[name] = (typ, help_)

    def reset(self) -> None:
        """Forget every value (tests). Descriptions stay."""
        with self._lock:
            self._values.clear()

    @staticmethod
    def _key(labels: dict[str, str] | None) -> _LabelKey:
        return tuple(sorted((labels or {}).items()))

    def inc(self, name: str, labels: dict[str, str] | None = None, by: float = 1.0) -> None:
        k = (name, self._key(labels))
        with self._lock:
            self._values[k] = self._values.get(k, 0.0) + by

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._values[(name, self._key(labels))] = float(value)

    def get(self, name: str, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._values.get((name, self._key(labels)), 0.0)

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """A summary without quantiles: <name>_sum and <name>_count."""
        self.inc(name + "_sum", labels, value)
        self.inc(name + "_count", labels, 1.0)

    def _family(self, name: str) -> str:
        for suffix in ("_sum", "_count"):
            if name.endswith(suffix) and name[: -len(suffix)] in self._meta:
                return name[: -len(suffix)]
        return name

    def render(self) -> str:
        with self._lock:
            items = sorted(self._values.items())
        families: dict[str, list[str]] = {}
        for (name, labels), value in items:
            lbl = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
            line = f"{name}{{{lbl}}} {_fmt(value)}" if lbl else f"{name} {_fmt(value)}"
            families.setdefault(self._family(name), []).append(line)
        out: list[str] = []
        for fam, lines in families.items():
            typ, help_ = self._meta.get(fam, ("untyped", ""))
            out.append(f"# HELP {fam} {help_}")
            out.append(f"# TYPE {fam} {typ}")
            out.extend(lines)
        return "\n".join(out) + "\n"


METRICS = Metrics()
for _n, _t, _h in [
    ("noc_build_info", "gauge", "Always 1; the engine version is the label."),
    ("noc_started_timestamp_seconds", "gauge", "When this engine process started."),
    ("noc_dry_run", "gauge", "1 when policy.dry_run is on: the engine plans and audits but executes nothing."),
    ("noc_runbooks", "gauge", "Runbooks loaded."),
    ("noc_alerts_received_total", "counter", "Firing alerts received from Alertmanager, counted before handling starts."),
    ("noc_runs_total", "counter", "Finished runs by decision."),
    ("noc_run_duration_seconds", "summary", "Run duration from alert received to audit line written."),
    ("noc_last_alert_timestamp_seconds", "gauge", "When the last firing alert arrived."),
    ("noc_last_run_timestamp_seconds", "gauge", "When the last run finished."),
    ("noc_llm_requests_total", "counter", "Triage model calls."),
    ("noc_llm_errors_total", "counter", "Triage model calls that failed (transport, HTTP status, timeout, no response)."),
    ("noc_llm_latency_seconds", "summary", "Triage model call latency, successful calls only."),
    ("noc_llm_last_success_timestamp_seconds", "gauge", "When the triage model last answered."),
    ("noc_approvals_pending", "gauge", "Parked runs waiting for a human."),
]:
    METRICS.describe(_n, _t, _h)

__all__ = ["METRICS", "Metrics", "engine_version", "time"]
