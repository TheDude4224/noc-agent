"""noc-agent command line.

  noc-agent serve                      run the Alertmanager webhook
  noc-agent demo                       replay examples/alerts.json through the loop
  noc-agent handle alert.json          run one alert from a file
  noc-agent approve <run_id>           execute a parked irreversible action
  noc-agent audit [-n 20]              print the last N audit lines
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import Agent
from .config import load_config
from .models import Alert
from .notify import format_line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="noc-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve")
    d = sub.add_parser("demo")
    d.add_argument("--alerts", default="examples/alerts.json")
    h = sub.add_parser("handle")
    h.add_argument("file")
    a = sub.add_parser("approve")
    a.add_argument("run_id")
    t = sub.add_parser("audit")
    t.add_argument("-n", type=int, default=20)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    agent = Agent(cfg)

    if args.cmd == "serve":
        import uvicorn
        from .server import create_app
        print(f"noc-agent listening on {cfg.server.host}:{cfg.server.port}  dry_run={cfg.policy.dry_run}  runbooks={len(agent.runbooks)}")
        uvicorn.run(create_app(agent), host=cfg.server.host, port=cfg.server.port, log_level="warning")
        return 0

    if args.cmd == "demo":
        alerts = [Alert.model_validate(x) for x in json.loads(Path(args.alerts).read_text())]
        print(f"replaying {len(alerts)} alerts  dry_run={cfg.policy.dry_run}  provider={cfg.llm.provider}\n")
        for al in alerts:
            agent.handle(al)
        print(f"\naudit written to {cfg.audit.path}")
        return 0

    if args.cmd == "handle":
        al = Alert.model_validate(json.loads(Path(args.file).read_text()))
        rec = agent.handle(al)
        return 0 if rec.decision != "error" else 1

    if args.cmd == "approve":
        try:
            rec = agent.approve(args.run_id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        return 0 if rec.decision == "executed" else 1

    if args.cmd == "audit":
        for rec in agent.audit.tail(args.n):
            print(f"{rec.ts:%Y-%m-%d %H:%M:%S} {rec.run_id} {format_line(rec)}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
