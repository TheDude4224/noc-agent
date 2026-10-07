"""Alertmanager webhook receiver.

Point Alertmanager at POST /alertmanager. Each firing alert becomes one run.
Resolved alerts are acknowledged and ignored; the agent acts on problems, not on
their absence.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .agent import Agent
from .metrics import METRICS
from .models import Alert


def normalize_alertmanager(payload: dict) -> list[Alert]:
    out: list[Alert] = []
    for a in payload.get("alerts", []):
        if a.get("status") != "firing":
            continue
        labels = dict(a.get("labels", {}))
        ann = a.get("annotations", {})
        out.append(Alert(
            alertname=labels.pop("alertname", "Unknown"),
            instance=labels.get("instance", ""),
            severity=labels.get("severity", "warning"),
            summary=ann.get("summary", ""),
            description=ann.get("description", ""),
            labels=labels,
            fingerprint=a.get("fingerprint", ""),
        ))
    return out


def create_app(agent: Agent) -> FastAPI:
    app = FastAPI(title="noc-agent", version="0.1.0")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "dry_run": agent.cfg.policy.dry_run, "runbooks": len(agent.runbooks)}

    @app.get("/metrics")
    def metrics():
        """Prometheus text format. Counters cover received alerts, runs by decision and model calls,
        so the monitoring stack can alert on an engine that is down, model-less, or stalled."""
        METRICS.set("noc_approvals_pending", agent.pending_approvals())
        return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/alertmanager")
    async def alertmanager(req: Request):
        payload = await req.json()
        alerts = normalize_alertmanager(payload)
        if alerts:
            # counted before handling: "received but never finished" is the stalled signature
            METRICS.inc("noc_alerts_received_total", by=len(alerts))
            METRICS.set("noc_last_alert_timestamp_seconds", time.time())
        results = [agent.handle(a) for a in alerts]
        return {"received": len(payload.get("alerts", [])), "handled": len(results),
                "decisions": [{"run_id": r.run_id, "alert": r.alert.alertname, "decision": r.decision} for r in results]}

    @app.get("/diagnosis/{run_id}")
    def diagnosis(run_id: str):
        d = agent.writer.get(run_id)
        if d is None:
            raise HTTPException(status_code=404, detail="no diagnosis for that run (writer off, skipped, or not done yet)")
        return d

    @app.get("/audit")
    def audit(n: int = 20):
        return [r.model_dump(mode="json") for r in agent.audit.tail(n)]

    return app
