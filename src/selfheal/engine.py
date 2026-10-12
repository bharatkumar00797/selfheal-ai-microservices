"""Runs a scenario end to end and returns a JSON-serialisable report."""

from __future__ import annotations

from typing import Any

from .healer import Healer, HealerConfig
from .llm import Provider
from .scenarios import get_scenario


def run_scenario(
    scenario_id: str,
    provider: Provider | None = None,
    config: HealerConfig | None = None,
    ticks: int | None = None,
    seed: int = 7,
) -> tuple[Healer, dict[str, Any]]:
    scenario = get_scenario(scenario_id)
    cluster = scenario.build(seed=seed)
    healer = Healer(cluster, provider=provider, config=config)
    herrings = sorted(scenario.red_herring_deploys)
    for _ in range(ticks or scenario.ticks):
        while herrings and herrings[0][0] <= cluster.tick + 1:
            _, svc, version, note = herrings.pop(0)
            cluster.tick += 1  # deploy lands at the start of the next tick
            cluster.schedule_deploy(svc, version, note)
            cluster.tick -= 1
        healer.step()
    report = healer.report()
    report["scenario"] = {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "expected_root_service": scenario.expected_root_service,
        "expected_action": scenario.expected_action,
        "expected_status": scenario.expected_status,
    }
    report["passed"] = evaluate(report)
    return healer, report


def evaluate(report: dict[str, Any]) -> bool:
    sc = report["scenario"]
    for inc in report["incidents"]:
        if inc["service"] != sc["expected_root_service"]:
            continue
        if sc["expected_status"] == "awaiting_approval":
            if inc["status"] == "awaiting_approval":
                return True
            # with an approver the same action must have run, approved, and fixed it
            approved = [a for a in inc["actions"] if a["approved_by"] and a["result"] == "fixed"]
            return bool(approved and approved[-1]["action"] == sc["expected_action"])
        effective = [a for a in inc["actions"] if a["result"] == "fixed"]
        return bool(
            inc["status"] == sc["expected_status"]
            and effective
            and effective[-1]["action"] == sc["expected_action"]
        )
    return False
