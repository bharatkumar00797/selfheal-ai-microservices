"""Runs a scenario end to end and returns a JSON-serialisable report."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from .healer import Healer, HealerConfig, IncidentStatus
from .llm import Provider
from .scenarios import get_scenario

ProgressFn = Callable[[dict[str, Any]], None]


def _service_snapshot(healer: Healer) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in healer.cluster.services:
        spec = healer.cluster.spec(name)
        st = healer.cluster.state(name)
        sample = healer.cluster.last(name)
        history = healer.history.get(name, [])
        # Keep a short sparkline window (last 30 ticks) without sharing live objects.
        spark = [
            {
                "tick": s.tick,
                "error_rate": s.error_rate,
                "p95_latency_ms": s.p95_latency_ms,
                "cpu_pct": s.cpu_pct,
                "memory_pct": s.memory_pct,
                "rps": s.rps,
            }
            for s in history[-30:]
        ]
        out.append(
            {
                "name": name,
                "version": st.version,
                "replicas": st.replicas,
                "max_replicas": spec.max_replicas,
                "dependencies": list(spec.dependencies),
                "protected": spec.protected,
                "external": spec.external,
                "open_breakers": sorted(st.open_breakers),
                "metrics": None if sample is None else sample.model_dump(mode="json"),
                "sparkline": spark,
            }
        )
    return out


def pending_approvals(healer: Healer) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for inc in healer.incidents:
        if inc.status is not IncidentStatus.AWAITING_APPROVAL or inc.action is None:
            continue
        pending.append(
            {
                "incident_id": inc.id,
                "service": inc.service,
                "action": str(inc.action.kind),
                "params": dict(inc.action.params),
                "hypothesis": None if inc.hypothesis is None else str(inc.hypothesis.cause),
                "evidence": [] if inc.hypothesis is None else list(inc.hypothesis.evidence),
            }
        )
    return pending


def snapshot(healer: Healer, *, scenario: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deep-copied view of the healer for API readers (never share live objects)."""
    report = healer.report()
    view = {
        "ticks": report["ticks"],
        "provider": report["provider"],
        "incidents": report["incidents"],
        "summary": report["summary"],
        "events": report["events"],
        "services": _service_snapshot(healer),
        "pending_approvals": pending_approvals(healer),
        "scenario": scenario,
    }
    return copy.deepcopy(view)


def run_scenario(
    scenario_id: str,
    provider: Provider | None = None,
    config: HealerConfig | None = None,
    ticks: int | None = None,
    seed: int = 7,
    on_progress: ProgressFn | None = None,
) -> tuple[Healer, dict[str, Any]]:
    scenario = get_scenario(scenario_id)
    cluster = scenario.build(seed=seed)
    healer = Healer(cluster, provider=provider, config=config)
    sc_meta = {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "expected_root_service": scenario.expected_root_service,
        "expected_action": scenario.expected_action,
        "expected_status": scenario.expected_status,
    }
    herrings = sorted(scenario.red_herring_deploys)
    for _ in range(ticks or scenario.ticks):
        while herrings and herrings[0][0] <= cluster.tick + 1:
            _, svc, version, note = herrings.pop(0)
            cluster.tick += 1  # deploy lands at the start of the next tick
            cluster.schedule_deploy(svc, version, note)
            cluster.tick -= 1
        healer.step()
        if on_progress is not None:
            on_progress(snapshot(healer, scenario=sc_meta))
    report = healer.report()
    report["scenario"] = sc_meta
    report["passed"] = evaluate(report)
    report["services"] = _service_snapshot(healer)
    report["pending_approvals"] = pending_approvals(healer)
    if on_progress is not None:
        on_progress(snapshot(healer, scenario=sc_meta))
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
