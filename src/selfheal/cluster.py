"""Deterministic simulated microservice cluster with fault injection.

The healer never touches this object directly except through :meth:`Cluster.apply`,
which is the single mutation path for remediation actions.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum

from .models import Action, ActionKind, MetricSample, ServiceSpec


class FaultKind(StrEnum):
    BAD_DEPLOY = "bad_deploy"
    MEMORY_LEAK = "memory_leak"
    TRAFFIC_SPIKE = "traffic_spike"
    OUTAGE = "outage"
    STUCK = "stuck"


@dataclass
class Fault:
    kind: FaultKind
    service: str
    start_tick: int
    magnitude: float = 1.0
    to_version: str | None = None  # bad_deploy: the version that introduces the bug
    active: bool = False


@dataclass
class DeployRecord:
    tick: int
    service: str
    from_version: str
    to_version: str
    note: str = ""


@dataclass
class ServiceState:
    spec: ServiceSpec
    replicas: int
    version: str
    previous_version: str | None = None
    memory_pct: float = 38.0
    restarts: int = 0
    open_breakers: set[str] = field(default_factory=set)
    bad_versions: set[str] = field(default_factory=set)
    leaking: bool = False
    stuck: bool = False
    down: bool = False
    load_multiplier: float = 1.0


class ClusterError(ValueError):
    """Raised when an action cannot be applied to the cluster."""


class Cluster:
    def __init__(self, services: list[ServiceSpec], faults: list[Fault], seed: int = 7) -> None:
        names = [s.name for s in services]
        if len(set(names)) != len(names):
            raise ClusterError("duplicate service names")
        for s in services:
            for dep in s.dependencies:
                if dep not in names:
                    raise ClusterError(f"{s.name} depends on unknown service {dep}")
        self.tick = 0
        self.rng = random.Random(seed)
        self.states = {
            s.name: ServiceState(spec=s, replicas=s.replicas, version=s.version) for s in services
        }
        self.faults = faults
        self.deploys: list[DeployRecord] = []
        self._last: dict[str, MetricSample] = {}

    # ----------------------------------------------------------------- inspection
    @property
    def services(self) -> list[str]:
        return list(self.states)

    def spec(self, name: str) -> ServiceSpec:
        return self._state(name).spec

    def state(self, name: str) -> ServiceState:
        return self._state(name)

    def last(self, name: str) -> MetricSample | None:
        return self._last.get(name)

    def _state(self, name: str) -> ServiceState:
        if name not in self.states:
            raise ClusterError(f"unknown service {name}")
        return self.states[name]

    def recent_deploys(self, service: str, within: int) -> list[DeployRecord]:
        return [d for d in self.deploys if d.service == service and self.tick - d.tick <= within]

    # ----------------------------------------------------------------- simulation
    def _activate_faults(self) -> None:
        for f in self.faults:
            if f.active or f.start_tick > self.tick:
                continue
            f.active = True
            st = self._state(f.service)
            if f.kind is FaultKind.BAD_DEPLOY:
                new = f.to_version or _bump(st.version)
                self._deploy(st, new, note="scheduled release")
                st.bad_versions.add(new)
            elif f.kind is FaultKind.MEMORY_LEAK:
                st.leaking = True
            elif f.kind is FaultKind.TRAFFIC_SPIKE:
                st.load_multiplier = f.magnitude
            elif f.kind is FaultKind.OUTAGE:
                st.down = True
            elif f.kind is FaultKind.STUCK:
                st.stuck = True

    def schedule_deploy(self, service: str, to_version: str, note: str = "") -> None:
        """Harmless deploy (used for red herrings)."""
        self._deploy(self._state(service), to_version, note)

    def _deploy(self, st: ServiceState, version: str, note: str) -> None:
        self.deploys.append(DeployRecord(self.tick, st.spec.name, st.version, version, note))
        st.previous_version, st.version = st.version, version

    def _dependency_error(self, st: ServiceState) -> float:
        err = 0.0
        for dep in st.spec.dependencies:
            if dep in st.open_breakers:
                continue  # fail fast with a fallback instead of timing out
            sample = self._last.get(dep)
            if sample is not None:
                err = max(err, sample.error_rate * 0.9)
        return err

    def step(self) -> dict[str, MetricSample]:
        """Advance one tick and return fresh samples for every service."""
        self.tick += 1
        self._activate_faults()
        samples: dict[str, MetricSample] = {}
        # dependencies first so callers see this tick's dependency health
        for name in self._topological():
            st = self.states[name]
            spec = st.spec
            noise = self.rng.uniform(-0.03, 0.03)
            rps = spec.base_rps * st.load_multiplier * (1 + noise)
            healthy = st.replicas
            if st.leaking:
                st.memory_pct = min(100.0, st.memory_pct + 9.0)
                if st.memory_pct >= 100.0:
                    healthy = max(0, st.replicas - 1 - st.replicas // 2)
            util = rps / max(1.0, healthy * spec.capacity_rps_per_replica)
            cpu = min(100.0, 12 + util * 55 + noise * 100)
            latency = spec.base_latency_ms * (1 + max(0.0, util - 0.8) * 12) * (1 + noise)
            error = 0.004 + max(0.0, util - 1.0) * 0.35
            if st.version in st.bad_versions:
                error += 0.32
                latency *= 1.4
            if st.stuck:
                latency = max(latency, spec.slo_latency_ms * 6)
                cpu = 4.0 + abs(noise) * 10
                error += 0.08
            if st.memory_pct >= 92:
                error += 0.15
                latency *= 2
            if st.down:
                error, latency, cpu, healthy = 1.0, spec.slo_latency_ms * 10, 0.0, 0
            error = max(error, self._dependency_error(st))
            if st.open_breakers:
                error += 0.01  # fallback responses are slightly degraded
            samples[name] = MetricSample(
                tick=self.tick,
                service=name,
                rps=round(rps, 2),
                error_rate=round(min(1.0, max(0.0, error)), 4),
                p95_latency_ms=round(latency, 1),
                cpu_pct=round(max(0.0, cpu), 1),
                memory_pct=round(st.memory_pct, 1),
                replicas=st.replicas,
                healthy_replicas=healthy,
                version=st.version,
            )
            self._last[name] = samples[name]
        return samples

    def _topological(self) -> list[str]:
        order: list[str] = []
        seen: set[str] = set()

        def visit(n: str, stack: frozenset[str]) -> None:
            if n in seen:
                return
            if n in stack:
                raise ClusterError("dependency cycle")
            for d in self.states[n].spec.dependencies:
                visit(d, stack | {n})
            seen.add(n)
            order.append(n)

        for n in self.states:
            visit(n, frozenset())
        return order

    # ----------------------------------------------------------------- actions
    def apply(self, action: Action) -> str:
        """Apply a validated remediation. Returns a human readable result."""
        st = self._state(action.service)
        if st.spec.external:
            raise ClusterError(f"{action.service} is external and cannot be changed")
        kind = action.kind
        if kind is ActionKind.RESTART:
            st.restarts += 1
            st.memory_pct = 38.0
            st.stuck = False
            st.leaking = False  # restart releases leaked memory; the code fix is a follow-up
            return f"restarted {st.replicas} replicas of {action.service}"
        if kind is ActionKind.SCALE_OUT:
            replicas = int(action.params["replicas"])
            if not 1 <= replicas <= st.spec.max_replicas:
                raise ClusterError("replica count outside allowed range")
            old, st.replicas = st.replicas, replicas
            return f"scaled {action.service} {old} -> {replicas} replicas"
        if kind is ActionKind.ROLLBACK:
            target = str(action.params["to_version"])
            if not any(
                d.service == action.service and d.from_version == target for d in self.deploys
            ):
                raise ClusterError(f"{target} was never deployed for {action.service}")
            current = st.version
            self._deploy(st, target, note="rollback")
            return f"rolled back {action.service} {current} -> {target}"
        if kind is ActionKind.CIRCUIT_BREAK:
            dep = str(action.params["dependency"])
            if dep not in st.spec.dependencies:
                raise ClusterError(f"{dep} is not a dependency of {action.service}")
            if action.params.get("close"):
                st.open_breakers.discard(dep)
                return f"closed circuit breaker {action.service} -> {dep}"
            st.open_breakers.add(dep)
            return f"opened circuit breaker {action.service} -> {dep} (serving fallback)"
        raise ClusterError(f"unsupported action {kind}")


def _bump(version: str) -> str:
    prefix = "v" if version.startswith("v") else ""
    major, minor, _ = (int(p) for p in version.lstrip("v").split("."))
    return f"{prefix}{major}.{minor + 1}.0"
