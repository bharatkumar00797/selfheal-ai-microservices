import json

import pytest

from selfheal.cli import main
from selfheal.engine import run_scenario
from selfheal.healer import HealerConfig
from selfheal.scenarios import SCENARIOS, get_scenario


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_every_scenario_meets_expectation(sid):
    _, report = run_scenario(sid)
    assert report["passed"], json.dumps(report["events"], indent=1)


def test_red_herring_deploy_is_refuted_and_reverted():
    healer, report = run_scenario("traffic-spike")
    inc = next(i for i in report["incidents"] if i["service"] == "catalog")
    assert inc["refuted"] == ["bad_deploy"]
    assert [a["result"] for a in inc["actions"]] == ["ineffective", "fixed"]
    assert healer.cluster.state("catalog").version == "3.2.0"  # rollback undone
    assert any(e["type"] == "reverted" for e in report["events"])


def test_external_outage_escalates_once_and_protects_callers():
    healer, report = run_scenario("dependency-outage")
    esc = [i for i in report["incidents"] if i["service"] == "bank-gateway"]
    assert len(esc) == 1 and esc[0]["status"] == "escalated"
    assert "bank-gateway" in healer.cluster.state("payments").open_breakers


def test_protected_service_waits_for_approval_then_heals():
    healer, _ = run_scenario("stuck-database")
    inc = next(i for i in healer.incidents if i.service == "orders-db")
    assert inc.status == "awaiting_approval" and healer.cluster.state("orders-db").restarts == 0
    healer.approve(inc.id, "Bharat")
    healer.run(4)
    assert inc.status == "resolved" and inc.actions[-1]["approved_by"] == "Bharat"
    with pytest.raises(ValueError):
        healer.approve(inc.id, "Bharat")


def test_injection_note_does_not_change_the_action():
    healer, report = run_scenario("prompt-injection-deploy")
    executed = [e for e in report["events"] if e["type"] == "executed"]
    assert [e["service"] for e in executed] == ["inventory"]
    assert all(s.replicas <= 3 for s in healer.cluster.states.values())


def test_dry_run_never_mutates():
    healer, report = run_scenario("bad-deploy", config=HealerConfig(dry_run=True))
    assert healer.cluster.state("checkout").version == "2.5.0"
    assert any(e["type"] == "dry_run" for e in report["events"])
    assert not any(e["type"] == "executed" for e in report["events"])


def test_report_is_deterministic_and_has_mttr():
    a, b = run_scenario("bad-deploy")[1], run_scenario("bad-deploy")[1]
    assert a == b and a["summary"]["mean_mttr_minutes"] == 3.0


def test_unknown_scenario():
    with pytest.raises(KeyError):
        get_scenario("nope")


def test_cli(capsys):
    assert main(["scenarios"]) == 0
    assert "traffic-spike" in capsys.readouterr().out
    assert main(["run", "bad-deploy"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["run", "memory-leak", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert main(["run", "bad-deploy", "--ticks", "0"]) == 2
    assert main(["run", "stuck-database", "--approver", "Bharat"]) == 0


def test_cli_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "selfheal 0.1.0" in capsys.readouterr().out
