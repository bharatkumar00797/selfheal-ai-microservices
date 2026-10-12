"""Maps a hypothesis to the allowlisted remediation that addresses it."""

from __future__ import annotations

import math

from .cluster import Cluster
from .models import Action, ActionKind, CauseKind, Hypothesis

TARGET_UTILISATION = 0.6


def plan_for(h: Hypothesis, cluster: Cluster) -> Action | None:
    st = cluster.state(h.service)
    if h.cause is CauseKind.BAD_DEPLOY and h.target:
        return Action(
            kind=ActionKind.ROLLBACK,
            service=h.service,
            params={"to_version": h.target},
            reason=f"roll back to last known good {h.target}",
        )
    if h.cause in (CauseKind.MEMORY_LEAK, CauseKind.STUCK_PROCESS):
        return Action(
            kind=ActionKind.RESTART, service=h.service, reason=f"restart to clear {h.cause}"
        )
    if h.cause is CauseKind.TRAFFIC_SPIKE:
        sample = cluster.last(h.service)
        rps = sample.rps if sample else st.spec.base_rps
        need = math.ceil(rps / (st.spec.capacity_rps_per_replica * TARGET_UTILISATION))
        return Action(
            kind=ActionKind.SCALE_OUT,
            service=h.service,
            params={"replicas": max(st.replicas + 1, need)},
            reason=f"add capacity for {rps:.0f} rps",
        )
    if h.cause is CauseKind.DEPENDENCY_FAILURE and h.target:
        return Action(
            kind=ActionKind.CIRCUIT_BREAK,
            service=h.service,
            params={"dependency": h.target},
            reason=f"stop cascading failures from {h.target}",
        )
    return None


def inverse(
    action: Action, cluster: Cluster, before_replicas: int, before_version: str
) -> Action | None:
    """Undo action for an ineffective remediation (restart has no inverse)."""
    if action.kind is ActionKind.SCALE_OUT:
        return Action(
            kind=ActionKind.SCALE_OUT,
            service=action.service,
            params={"replicas": before_replicas},
            reason="revert ineffective scale-out",
        )
    if action.kind is ActionKind.ROLLBACK:
        return Action(
            kind=ActionKind.ROLLBACK,
            service=action.service,
            params={"to_version": before_version},
            reason="revert ineffective rollback",
        )
    if action.kind is ActionKind.CIRCUIT_BREAK:
        return Action(
            kind=ActionKind.CIRCUIT_BREAK,
            service=action.service,
            params={**action.params, "close": True},
            reason="close ineffective breaker",
        )
    return None
