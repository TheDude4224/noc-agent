# noc-agent

[![tests](https://github.com/TheDude4224/noc-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/TheDude4224/noc-agent/actions/workflows/tests.yml)

A small, honest reference implementation of an AI-assisted Network Operations Center.

Alert comes in. A model triages it against a fixed menu of runbooks. Deterministic guardrails decide whether anything runs. Every run writes one audit line. The model never writes a command; it only picks from the menu, and seven gates sit between its pick and your infrastructure.

I built the production version of this pattern for a private-5G operator (where a major U.S. carrier licensed it) and for my own infrastructure practice, where it cut customer mean time to resolution by about 82%. This repo is the pattern stripped to ~770 lines so you can read it in an afternoon and run it on a laptop in five minutes.

**It is not a product.** It is the shape of a thing that works, with the guardrails that make it safe to leave running overnight.

## Five-minute demo

```bash
git clone https://github.com/TheDude4224/noc-agent && cd noc-agent
pip install -e ".[dev]"
python -m noc_agent.cli -c examples/config.demo.yaml demo
```

No model, no servers, nothing touched. A canned "model" replays triage for seven alerts and you watch every gate fire:

```
replaying 7 alerts  dry_run=True  provider=fake

[DRY RUN] ServiceDown on web-03.lab.local | runbook=restart-service | conf=0.92 | policy.dry_run is true; nothing executed
[DRY RUN] DiskSpaceLow on db-01.lab.local | runbook=clear-journal-logs | conf=0.88 | policy.dry_run is true; nothing executed
[NEEDS APPROVAL] PrimaryWANDown on rtr-fenton.lab.local | runbook=failover-to-secondary-wan | conf=0.85 | runbook 'failover-to-secondary-wan' is irreversible; parked for human approval | approve with: noc-agent approve d14cc1e104cf
[BLOCKED] HighErrorRate on pay-api-01.lab.local | runbook=restart-service | conf=0.80 | label tier=payments is on the never-automate list
[ESCALATED] WeirdCertThing on vpn-01.lab.local | conf=0.30 | The alert itself says the cause is unclear and none of the runbooks address TLS. A miss is cheaper than a wrong action.
[DRY RUN] ServiceDown on web-03.lab.local | runbook=restart-service | conf=0.92 | policy.dry_run is true; nothing executed
[DRY RUN] ServiceDown on web-03.lab.local | runbook=restart-service | conf=0.92 | policy.dry_run is true; nothing executed
```

Now turn dry-run off, with runbooks swapped for harmless local commands, and watch it actually execute, verify, fail verification, park for approval, and hit the rate cap:

```bash
python -m noc_agent.cli -c examples/config.live-demo.yaml demo
```

```
[FIXED] ServiceDown on web-03.lab.local | runbook=restart-service | conf=0.92 | ran and verified
[ERROR] DiskSpaceLow on db-01.lab.local | runbook=clear-journal-logs | conf=0.88 | ran, but verify failed and no rollback is defined; needs a human
[NEEDS APPROVAL] PrimaryWANDown on rtr-fenton.lab.local | runbook=failover-to-secondary-wan | conf=0.85 | ... | approve with: noc-agent approve fedc61f124f4
[BLOCKED] HighErrorRate on pay-api-01.lab.local | runbook=restart-service | conf=0.80 | label tier=payments is on the never-automate list
[ESCALATED] WeirdCertThing on vpn-01.lab.local | conf=0.30 | ...
[FIXED] ServiceDown on web-03.lab.local | runbook=restart-service | conf=0.92 | ran and verified
[BLOCKED] ServiceDown on web-03.lab.local | runbook=restart-service | conf=0.92 | per-alert cap reached (2) for ServiceDown/web-03.lab.local:9100
```

Approve the parked failover as the human on call:

```bash
python -m noc_agent.cli -c examples/config.live-demo.yaml approve fedc61f124f4
# [FIXED] PrimaryWANDown on rtr-fenton.lab.local | runbook=failover-to-secondary-wan | human-approved parked run fedc61f124f4
```

Then read the audit log, which is the only thing that matters at 2am:

```bash
python -m noc_agent.cli audit -n 10
```

## How it works

```
Alertmanager ──POST /alertmanager──▶ normalize ──▶ triage (LLM) ──▶ policy gates ──▶ execute ──▶ audit + notify
                                                      │                  │              │
                                            picks a runbook id     7 deterministic   render, run,
                                            from a fixed menu      checks, in order  verify, rollback
```

**Triage** (`triage.py`): the model sees the alert and a numbered menu of runbooks that claim that alert (by alertname and, optionally, labels). It returns a runbook id, a confidence, two sentences of reasoning, and a blast-radius guess. Anything else, including bad JSON, a made-up id, or a timeout, becomes `escalate-to-human`. The model has no tool to run commands. It cannot invent one.

**Policy** (`policy.py`): seven gates, cheapest and hardest first, all after the model so it can't argue with them:

1. Model punted or chose an id that doesn't exist → **escalated**
2. Alert carries a `never_automate_labels` label (`tier=payments`, `tier=auth`) → **blocked**
3. The chosen runbook's `matches` don't claim this alert (alertname and label matchers) → **blocked** (the model's choice is not enough; the runbook has to claim the alert too)
4. Confidence below `min_confidence_to_act` → **blocked**
5. Per-alert or per-hour action cap reached → **blocked** (this is what stops a loop)
6. Runbook is `reversible: false` → **needs-approval**, parked to a file for a human
7. `policy.dry_run` is true (the default) → **dry-run**

Only then does anything execute.

**Executor** (`executor.py`): renders `{host}`, `{instance}`, `{label.x}` placeholders with shell quoting (a label value of `nginx; rm -rf /` becomes a harmless quoted string), runs the command with a timeout, runs `verify`, and if verify fails runs `rollback`. Missing placeholder values fail loudly instead of expanding to nothing.

**Audit** (`audit.py`): append-only JSONL, one line per run, flushed immediately. Alert, triage, decision, rendered command, exit code, stdout/stderr tail, reason, duration. `noc-agent audit` prints it; `GET /audit` serves it.

**Metrics** (`metrics.py`): `GET /metrics` in Prometheus text format, no extra dependency. Watch the watcher: `noc_alerts_received_total` is counted *before* handling and `noc_runs_total{decision}` after, so "received but never finished" is visible; `noc_llm_requests_total` / `noc_llm_errors_total` / `noc_llm_latency_seconds` show whether the triage model answers; `noc_dry_run`, `noc_runbooks`, `noc_approvals_pending`, `noc_last_run_timestamp_seconds` and `noc_build_info{version}` round it out. Suggested alerts: `up == 0` (engine down), `increase(noc_llm_errors_total[15m]) > 0 and increase(noc_llm_requests_total[15m]) == increase(noc_llm_errors_total[15m])` (model unreachable), `increase(noc_alerts_received_total[15m]) > 0 and sum(increase(noc_runs_total[15m])) == 0` (stalled). Deliver those through the monitoring stack, never through the agent itself.

## Runbooks are the whole attack surface

`runbooks/runbooks.yaml` is the only place a command can come from. Keep it short. Every entry is:

```yaml
- id: restart-service
  description: Restart a systemd service on the affected host. Safe for stateless services.
  matches: ["ServiceDown", "HighErrorRate"]
  command: "ssh -o BatchMode=yes {host} 'sudo systemctl restart {label.service}'"
  verify:  "ssh -o BatchMode=yes {host} 'systemctl is-active {label.service}'"
  reversible: true
  timeout: 60
```

`reversible: false` means a person approves every time, regardless of confidence. Use it for anything that affects more than one host, drops sessions, or changes routing. The `failover-to-secondary-wan` and `reboot-host` entries are examples. If you forget the field it defaults to `false`, which is the cautious direction.

The model reads `description`. Write it for the model the way you'd write it for a new hire on the night shift.

### Scoping a runbook with label matchers

Each `matches` entry is one alternative, written like a PromQL selector without the metric. Entries are ORed; matchers inside one pair of braces are ANDed:

```yaml
matches:
  - "ServiceDown"                                   # that alertname (any labels)
  - 'GuestStopped{node="pve-01", id=~"lxc/.*"}'     # that alertname, only for LXC guests on pve-01
  - '{team="network", severity!="info"}'            # any alertname carrying these labels
  - "*"                                             # anything (escalate-to-human uses this)
```

Operators are `=`, `!=`, `=~`, `!~`. Regexes are fully anchored, as in Prometheus (`job=~"node"` means exactly `node`). A missing label compares as the empty string. Matchers see the alert's labels plus `alertname`, `instance`, `host` and `severity`. A selector that doesn't parse stops the runbook file from loading; it never silently matches nothing or everything. The same check is policy gate 3, so a label-scoped runbook can't be used outside its scope even if the model picks it.

## Pointing it at a real lab

1. Copy `config.example.yaml` to `config.yaml`. Point `llm.base_url` at anything OpenAI-compatible. I run it against Ollama (`llama3.1:8b` is enough for this task) and vLLM on a self-hosted GPU box; OpenAI and OpenRouter work with an API key in `NOC_LLM_API_KEY`.
2. Edit `runbooks/runbooks.yaml` for your hosts. The defaults assume SSH key auth and passwordless sudo for the agent's user; scope that user tightly.
3. Leave `dry_run: true`. Run `noc-agent serve` and point Alertmanager at it:

   ```yaml
   receivers:
     - name: noc-agent
       webhook_configs:
         - url: http://noc-agent-host:8088/alertmanager
   ```

4. Watch `audit.jsonl` for a week. Every line says what it *would* have done. When you stop disagreeing with it, flip `dry_run` to false for the reversible runbooks only.
5. Set `notify.webhook_url` to a Slack, Teams, or ntfy endpoint so the "needs approval" lines reach a phone.

## The production rules this encodes

These came from running the real thing, not from a design doc:

- **A miss is cheaper than a wrong action.** Low confidence and off-menu answers escalate. Nothing clever happens on ambiguity.
- **The model proposes; policy disposes.** Every gate is deterministic code that runs after the model. There is no prompt that unlocks a gate.
- **Allowlist, never blocklist.** Runbooks are the complete set of possible actions. There is no "run this command" tool.
- **Irreversible means a human, every time.** Not "a human when confidence is low." Every time.
- **Cap it.** Per-alert and per-hour limits, because the failure mode of an automation loop is not one wrong action, it is four hundred.
- **Verify, then roll back.** A fix that didn't take is worse than no fix, because now the alert looks handled.
- **One audit line per run, before anything else.** If the process dies after executing, the line is already on disk.
- **It stays quiet until something is worth your attention.** The notifier sends one terse line. If it did something, it says what. If it didn't, it says why.

## What's deliberately not here

- No multi-step agent loops. One alert, one decision, one action. Chaining is where blast radius comes from.
- No "ask the model to write the command." See above.
- No database. JSONL and a directory of approval files are enough until they aren't, and you'll know when.
- No LangChain, no framework. ~770 lines of Python, FastAPI for the webhook, httpx for the model, that's it.

## Layout

```
noc_agent/
  models.py     Alert, Runbook, Triage, AuditRecord
  config.py     YAML in, pydantic out
  triage.py     prompt, OpenAI-compatible call, fake provider, hardening
  policy.py     the seven gates
  executor.py   render, run, verify, rollback
  agent.py      the loop; approvals
  audit.py      JSONL
  notify.py     stdout + webhook
  server.py     Alertmanager receiver
  cli.py        serve / demo / handle / approve / audit
runbooks/runbooks.yaml        the allowlist
examples/                     demo configs, canned model, sample alerts, local runbooks
tests/                        28 tests; every gate has one
```

## Run it as a container

```bash
docker build -t noc-agent .
cp examples/config.live-demo.yaml config.yaml   # edit llm/policy; paths below are the mounts
docker run -d --name noc-agent -p 8088:8088 \
  -v $PWD/config.yaml:/config/config.yaml:ro \
  -v $PWD/runbooks:/runbooks:ro \
  -v noc-state:/var/lib/noc-agent \
  -v $PWD/keys:/root/.ssh:ro \
  noc-agent
curl -s localhost:8088/healthz
```

The image holds the code only. Config, runbooks, SSH keys and the audit log are mounts, so an
upgrade is `docker pull` + restart and nothing else moves. `openssh-client` is the one system
package: runbooks reach hosts over forced-command SSH with the mounted keys. `compose.example.yml`
is the same thing as a service. In `config.yaml` point `runbooks_file` at `/runbooks/runbooks.yaml`,
`audit.path` at `/var/lib/noc-agent/audit.jsonl` and `approvals_dir` at `/var/lib/noc-agent/approvals`.

## Tests

```bash
pytest
```

## License

Apache-2.0. Use it, fork it, tell me what broke.

Jason Vardon · jason@vardon.org · [linkedin.com/in/jlvardon](https://linkedin.com/in/jlvardon)
