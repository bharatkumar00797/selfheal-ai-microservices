"""Safety policy: every proposed action passes through here before it can run."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .cluster import Cluster
from .models import Action, ActionKind


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass
class PolicyResult:
    decision: Decision
    reasons: list[str]
    action: Action


@dataclass
class SafetyPolicy:
    allowed_actions: set[ActionKind] = field(default_factory=lambda: set(ActionKind))
    cooldown_ticks: int = 3
    max_actions_per_window: int = 6
    window_ticks: int = 30
    max_scale_step: int = 6
    max_attempts_per_incident: int = 3
    approval_actions: set[ActionKind] = field(default_factory=set)
    _history: list[tuple[int, str]] = field(default_factory=list)

    def check(self, action: Action, cluster: Cluster) -> PolicyResult:
        reasons: list[str] = []
        tick = cluster.tick
        try:
            st = cluster.state(action.service)
        except ValueError as exc:
            return PolicyResult(Decision.DENY, [str(exc)], action)
        if action.kind not in self.allowed_actions:
            return PolicyResult(Decision.DENY, [f"{action.kind} is not allowlisted"], action)
        if st.spec.external:
            return PolicyResult(Decision.DENY, [f"{action.service} is an external service"], action)
        recent = [t for t, s in self._history if tick - t < self.window_ticks]
        if len(recent) >= self.max_actions_per_window:
            return PolicyResult(Decision.DENY, ["global action budget exhausted"], action)
        last = [t for t, s in self._history if s == action.service]
        if last and tick - last[-1] < self.cooldown_ticks:
            return PolicyResult(Decision.DENY, [f"{action.service} is in cooldown"], action)

        if action.kind is ActionKind.SCALE_OUT:
            raw = action.params.get("replicas")
            if not isinstance(raw, int) or isinstance(raw, bool):
                return PolicyResult(Decision.DENY, ["replicas must be an integer"], action)
            clamped = min(raw, st.spec.max_replicas, st.replicas + self.max_scale_step)
            clamped = max(clamped, 1)
            if clamped != raw:
                reasons.append(f"replicas clamped {raw} -> {clamped}")
                action = action.model_copy(update={"params": {"replicas": clamped}})
        elif action.kind is ActionKind.ROLLBACK:
            if not isinstance(action.params.get("to_version"), str):
                return PolicyResult(Decision.DENY, ["to_version required"], action)
        elif action.kind is ActionKind.CIRCUIT_BREAK:
            if action.params.get("dependency") not in st.spec.dependencies:
                return PolicyResult(Decision.DENY, ["breaker target is not a dependency"], action)
        elif action.params:
            return PolicyResult(Decision.DENY, ["restart takes no parameters"], action)

        if st.spec.protected or action.kind in self.approval_actions:
            reasons.append(f"{action.service} is protected: human approval required")
            return PolicyResult(Decision.REQUIRE_APPROVAL, reasons, action)
        return PolicyResult(Decision.ALLOW, reasons or ["within policy"], action)

    def record(self, action: Action, tick: int) -> None:
        self._history.append((tick, action.service))
