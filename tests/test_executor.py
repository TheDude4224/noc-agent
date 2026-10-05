import pytest

from noc_agent.executor import execute, render
from noc_agent.models import Alert, Runbook


def alert(**labels):
    return Alert(alertname="X", instance="box-1:9100", labels=labels)


def test_render_quotes_label_values_so_they_cannot_inject():
    a = alert(service="nginx; rm -rf /")
    out = render("systemctl restart {label.service}", a)
    assert out == "systemctl restart 'nginx; rm -rf /'"


def test_render_host_and_instance():
    assert render("ssh {host} && echo {instance}", alert()) == "ssh box-1 && echo box-1:9100"


def test_render_missing_label_fails_loudly():
    with pytest.raises(KeyError):
        render("echo {label.nope}", alert())


def test_render_leaves_unknown_braces_alone():
    assert render("docker inspect -f {{.State.Running}}", alert()) == "docker inspect -f {{.State.Running}}"


def test_execute_success_with_verify():
    rb = Runbook(id="t", description="", matches=["X"], command="echo hi", verify="true", reversible=True)
    r = execute(rb, alert())
    assert r.exit_code == 0 and r.stdout.strip() == "hi" and not r.verify_failed


def test_execute_verify_fails_with_rollback():
    rb = Runbook(id="t", description="", matches=["X"], command="echo did", verify="false",
                 rollback="echo undid", reversible=True)
    r = execute(rb, alert())
    assert r.exit_code != 0 and r.rolled_back and r.verify_failed
    assert "rollback exit 0" in r.stderr


def test_execute_verify_fails_without_rollback():
    rb = Runbook(id="t", description="", matches=["X"], command="true", verify="false", reversible=True)
    r = execute(rb, alert())
    assert r.verify_failed and not r.rolled_back


def test_execute_command_failure_skips_verify():
    rb = Runbook(id="t", description="", matches=["X"], command="exit 3", verify="echo should-not-run", reversible=True)
    r = execute(rb, alert())
    assert r.exit_code == 3 and "should-not-run" not in r.stdout


def test_execute_timeout():
    rb = Runbook(id="t", description="", matches=["X"], command="sleep 5", reversible=True, timeout=1)
    r = execute(rb, alert())
    assert r.exit_code == 124 and "timed out" in r.stderr
