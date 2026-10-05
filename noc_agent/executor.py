"""Run a runbook. Render placeholders, execute, verify, roll back if verify fails.

Placeholders: {host} {instance} {alertname} {label.<name>}. Anything else is left
as-is so a typo in a runbook fails loudly instead of expanding to something odd.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass

from .models import Alert, Runbook

_PLACEHOLDER = re.compile(r"\{(host|instance|alertname|label\.[A-Za-z0-9_]+)\}")


def render(template: str, alert: Alert) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key == "host":
            val = alert.host
        elif key == "instance":
            val = alert.instance
        elif key == "alertname":
            val = alert.alertname
        else:
            val = alert.labels.get(key.split(".", 1)[1], "")
        if not val:
            raise KeyError(f"placeholder {{{key}}} has no value on this alert")
        # Quote so a label value can't inject into the shell string.
        return shlex.quote(val)
    return _PLACEHOLDER.sub(sub, template)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    rolled_back: bool = False
    verify_failed: bool = False


def _run(cmd: str, timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout[-4000:], p.stderr[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def execute(runbook: Runbook, alert: Alert) -> ExecResult:
    cmd = render(runbook.command, alert)
    code, out, err = _run(cmd, runbook.timeout)
    if code != 0:
        return ExecResult(code, out, err)

    if runbook.verify:
        vcode, vout, verr = _run(render(runbook.verify, alert), runbook.timeout)
        if vcode != 0:
            err = (err + f"\nverify failed (exit {vcode}): {verr or vout}").strip()
            if runbook.rollback:
                rcode, rout, rerr = _run(render(runbook.rollback, alert), runbook.timeout)
                err += f"\nrollback exit {rcode}: {rerr or rout}".rstrip()
                return ExecResult(vcode, out, err, rolled_back=True, verify_failed=True)
            return ExecResult(vcode, out, err, verify_failed=True)

    return ExecResult(0, out, err)
