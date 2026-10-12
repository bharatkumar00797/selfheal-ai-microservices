"""The self-healing control loop: observe -> detect -> diagnose -> decide -> guard -> act -> verify."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .cluster import Cluster, ClusterError
from .detector import Detector, DetectorConfig
from .diagnosis import diagnose
from .llm import MockProvider, Provider, decide
from .models import Action, Anomaly, CauseKind, Event, EventType, Hypothesis, MetricSample
from .planner import inverse, plan_for
from .policy import Decision, SafetyPolicy


class IncidentStatus(StrEnum):
    OPEN = "open"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


@dataclass
class Incident:
    id: str
    service: str
    opened_tick: int
    signals: list[str]
    status: IncidentStatus = IncidentStatus.OPEN
    attempts: int = 0
    refuted: set[CauseKind] = field(default_factory=set)
    hypothesis: Hypothesis | None = None
    action: Action | None = None
    verify_deadline: int = 0
    healthy_streak: int = 0
    before_replicas: int = 0
    before_version: str = ""
    resolved_tick: int | None = None
    cleared: bool = False  # escalated incidents stay attached until the service is healthy again
    actions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status not in (IncidentStatus.RESOLVED, IncidentStatus.ESCALATED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.service,
            "status": str(self.status),
            "opened_tick": self.opened_tick,
            "resolved_tick": self.resolved_tick,
            "mttr_minutes": None
            if self.resolved_tick is None
            else self.resolved_tick - self.opened_tick,
            "signals": self.signals,
            "root_cause": None if self.hypothesis is None else str(self.hypothesis.cause),
            "refuted": sorted(str(c) for c in self.refuted),
            "attempts": self.attempts,
            "actions": self.actions,
        }


@dataclass
class HealerConfig:
    verify_ticks: int = 3  # consecutive healthy samples needed to call it fixed
    verify_timeout: int = 5
    dependency_wait_ticks: int = 10
    dry_run: bool = False
    approver: str | None = None  # auto-approve protected actions as this person


class Healer:
    def __init__(
        self,
        cluster: Cluster,
        provider: Provider | None = None,
        policy: SafetyPolicy | None = None,
        config: HealerConfig | None = None,
        detector_config: DetectorConfig | None = None,
    ) -> None:
        self.cluster = cluster
        self.provider = provider or MockProvider()
        self.policy = policy or SafetyPolicy()
        self.config = config or HealerConfig()
        self.detector = Detector({n: cluster.spec(n) for n in cluster.services}, detector_config)
        self.history: dict[str, list[MetricSample]] = {n: [] for n in cluster.services}
        self.incidents: list[Incident] = []
        self.events: list[Event] = []

    # ------------------------------------------------------------------ helpers
    def _emit(self, inc: Incident, type_: EventType, message: str, **data: Any) -> None:
        self.events.append(
            Event(
                tick=self.cluster.tick,
                service=inc.service,
                type=type_,
                message=message,
                data=data | {"incident": inc.id},
            )
        )

    def active_incident(self, service: str) -> Incident | None:
        for inc in self.incidents:
            if inc.service == service and inc.active:
                return inc
        return None

    def _escalated_uncleared(self, service: str) -> bool:
        return any(
            i.service == service and i.status is IncidentStatus.ESCALATED and not i.cleared
            for i in self.incidents
        )

    # ------------------------------------------------------------------ loop
    def step(self) -> dict[str, MetricSample]:
        samples = self.cluster.step()
        for name, s in samples.items():
            self.history[name].append(s)
            del self.history[name][:-60]
        anomalies = {a.service: a for a in self.detector.observe(samples)}

        for inc in [i for i in self.incidents if i.status is IncidentStatus.VERIFYING]:
            self._verify(inc, samples[inc.service])

        for inc in self.incidents:
            if inc.status is IncidentStatus.ESCALATED and not inc.cleared:
                inc.healthy_streak = 0 if inc.service in anomalies else inc.healthy_streak + 1
                if inc.healthy_streak >= self.config.verify_ticks:
                    inc.cleared = True
                    self._emit(inc, EventType.WARNING, "service healthy again after escalation")

        for inc in [i for i in self.incidents if i.status is IncidentStatus.OPEN]:
            if inc.service in anomalies:
                inc.healthy_streak = 0
                continue
            inc.healthy_streak += 1
            if inc.healthy_streak >= self.config.verify_ticks:
                inc.status = IncidentStatus.RESOLVED
                inc.resolved_tick = self.cluster.tick
                self._emit(
                    inc, EventType.RESOLVED, "recovered without action (symptom of an upstream fix)"
                )

        for name, anomaly in anomalies.items():
            found = self.active_incident(name)
            if found is None and self._escalated_uncleared(name):
                continue  # humans already paged; do not re-page every minute
            if found is None:
                found = Incident(
                    id=f"inc-{len(self.incidents) + 1}",
                    service=name,
                    opened_tick=self.cluster.tick,
                    signals=[str(s) for s in anomaly.signals],
                )
                self.incidents.append(found)
                self._emit(
                    found,
                    EventType.ANOMALY,
                    f"{name}: {', '.join(found.signals)}",
                    **anomaly.details,
                )
            if found.status is IncidentStatus.OPEN:
                self._remediate(found, anomaly)
        return samples

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.step()

    # ------------------------------------------------------------------ phases
    def _upstream_incident(self, inc: Incident) -> Incident | None:
        for dep in self.cluster.spec(inc.service).dependencies:
            other = self.active_incident(dep)
            if other and not self.cluster.spec(dep).external:
                return other
        return None

    def _remediate(self, inc: Incident, anomaly: Anomaly) -> None:
        upstream = self._upstream_incident(inc)
        if upstream and self.cluster.tick - inc.opened_tick < self.config.dependency_wait_ticks:
            return  # let the root-cause service heal first; caller errors are a symptom

        if self.cluster.spec(inc.service).external:
            self._escalate(
                inc,
                "external dependency is failing: notify the provider, callers are protected by circuit breakers",
            )
            return
        if inc.attempts >= self.policy.max_attempts_per_incident:
            self._escalate(inc, "attempt budget exhausted")
            return
        candidates = [
            h
            for h in diagnose(anomaly, self.cluster, self.history[inc.service])
            if h.cause not in inc.refuted
        ]
        if not candidates:
            self._escalate(inc, "no remaining evidence-backed hypothesis")
            return
        context = {
            "service": inc.service,
            "signals": inc.signals,
            "metrics": anomaly.details,
            "recent_deploys": [
                {"from": d.from_version, "to": d.to_version, "note": d.note}
                for d in self.cluster.recent_deploys(inc.service, 30)
            ],
            "refuted": sorted(str(c) for c in inc.refuted),
        }
        choice = decide(self.provider, context, candidates)
        hyp = candidates[choice.index]
        inc.hypothesis = hyp
        self._emit(
            inc,
            EventType.DIAGNOSIS,
            f"likely {hyp.cause} ({hyp.confidence:.0%}): {choice.reasoning}",
            source=choice.source,
            evidence=hyp.evidence,
            candidates=[str(c.cause) for c in candidates],
        )
        action = plan_for(hyp, self.cluster)
        if action is None:
            inc.refuted.add(hyp.cause)
            self._emit(inc, EventType.WARNING, f"no remediation available for {hyp.cause}")
            return
        result = self.policy.check(action, self.cluster)
        action = result.action
        self._emit(
            inc,
            EventType.PLAN,
            f"{action.kind} {action.params or ''} - {action.reason}".strip(),
            policy=str(result.decision),
            reasons=result.reasons,
        )
        if result.decision is Decision.DENY:
            self._emit(inc, EventType.BLOCKED, "; ".join(result.reasons))
            if not any("cooldown" in r for r in result.reasons):
                self._escalate(inc, "policy denied the remediation")
            return
        if self.config.dry_run:
            self._emit(inc, EventType.DRY_RUN, f"would run {action.kind} on {action.service}")
            self._escalate(inc, "dry-run mode: human must act")
            return
        if result.decision is Decision.REQUIRE_APPROVAL:
            inc.action = action
            inc.status = IncidentStatus.AWAITING_APPROVAL
            self._emit(inc, EventType.APPROVAL_REQUIRED, "; ".join(result.reasons))
            if self.config.approver:
                self.approve(inc.id, self.config.approver)
            return
        self._execute(inc, action)

    def approve(self, incident_id: str, approver: str) -> Incident:
        inc = next((i for i in self.incidents if i.id == incident_id), None)
        if inc is None or inc.status is not IncidentStatus.AWAITING_APPROVAL or inc.action is None:
            raise ValueError("no pending approval for that incident")
        approver = approver.strip()[:60]
        if not approver:
            raise ValueError("approver name required")
        self._emit(inc, EventType.APPROVED, f"approved by {approver}")
        self._execute(inc, inc.action, approved_by=approver)
        return inc

    def reject(self, incident_id: str, approver: str, reason: str = "") -> Incident:
        """Human declines a protected action; escalate instead of executing."""
        inc = next((i for i in self.incidents if i.id == incident_id), None)
        if inc is None or inc.status is not IncidentStatus.AWAITING_APPROVAL or inc.action is None:
            raise ValueError("no pending approval for that incident")
        approver = approver.strip()[:60]
        if not approver:
            raise ValueError("approver name required")
        reason = " ".join(reason.split())[:500]
        note = f"rejected by {approver}" + (f": {reason}" if reason else "")
        self._emit(inc, EventType.BLOCKED, note)
        self._escalate(inc, f"human rejected the remediation ({approver})")
        return inc

    def _execute(self, inc: Incident, action: Action, approved_by: str | None = None) -> None:
        st = self.cluster.state(action.service)
        inc.before_replicas, inc.before_version = st.replicas, st.version
        try:
            outcome = self.cluster.apply(action)
        except ClusterError as exc:
            self._emit(inc, EventType.BLOCKED, f"execution failed: {exc}")
            if inc.hypothesis:
                inc.refuted.add(inc.hypothesis.cause)
            inc.status = IncidentStatus.OPEN
            return
        self.policy.record(action, self.cluster.tick)
        inc.attempts += 1
        inc.action = action
        inc.status = IncidentStatus.VERIFYING
        inc.healthy_streak = 0
        inc.verify_deadline = self.cluster.tick + self.config.verify_timeout
        inc.actions.append(
            {
                "tick": self.cluster.tick,
                "action": str(action.kind),
                "params": action.params,
                "approved_by": approved_by,
                "result": "pending",
            }
        )
        self._emit(inc, EventType.EXECUTED, outcome)

    def _verify(self, inc: Incident, sample: MetricSample) -> None:
        if self.detector.is_healthy(sample):
            inc.healthy_streak += 1
        else:
            inc.healthy_streak = 0
        if inc.healthy_streak >= self.config.verify_ticks:
            inc.actions[-1]["result"] = "fixed"
            inc.status = IncidentStatus.RESOLVED
            inc.resolved_tick = self.cluster.tick
            self._emit(
                inc, EventType.VERIFIED, f"healthy for {inc.healthy_streak} consecutive minutes"
            )
            self._emit(
                inc, EventType.RESOLVED, f"resolved in {inc.resolved_tick - inc.opened_tick} min"
            )
            return
        if self.cluster.tick < inc.verify_deadline:
            return
        inc.actions[-1]["result"] = "ineffective"
        assert inc.action is not None and inc.hypothesis is not None
        inc.refuted.add(inc.hypothesis.cause)
        self._emit(
            inc,
            EventType.VERIFY_FAILED,
            f"{inc.action.kind} did not restore health; hypothesis {inc.hypothesis.cause} refuted",
        )
        undo = inverse(inc.action, self.cluster, inc.before_replicas, inc.before_version)
        if undo is not None:
            try:
                self._emit(inc, EventType.REVERTED, self.cluster.apply(undo))
            except ClusterError as exc:
                self._emit(inc, EventType.WARNING, f"revert failed: {exc}")
        inc.status = IncidentStatus.OPEN

    def _escalate(self, inc: Incident, reason: str) -> None:
        inc.status = IncidentStatus.ESCALATED
        self._emit(inc, EventType.ESCALATED, f"paging on-call: {reason}")

    # ------------------------------------------------------------------ report
    def report(self) -> dict[str, Any]:
        resolved = [i for i in self.incidents if i.status is IncidentStatus.RESOLVED]
        mttrs = [i.resolved_tick - i.opened_tick for i in resolved if i.resolved_tick is not None]
        return {
            "ticks": self.cluster.tick,
            "provider": self.provider.name,
            "incidents": [i.to_dict() for i in self.incidents],
            "summary": {
                "total": len(self.incidents),
                "resolved": len(resolved),
                "escalated": sum(i.status is IncidentStatus.ESCALATED for i in self.incidents),
                "open": sum(i.active for i in self.incidents),
                "mean_mttr_minutes": round(sum(mttrs) / len(mttrs), 1) if mttrs else None,
            },
            "events": [e.model_dump(mode="json") for e in self.events],
        }
