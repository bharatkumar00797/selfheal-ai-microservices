"""Rule-based diagnosis: turns anomalies + cluster context into ranked hypotheses.

The rules are the deterministic floor. The LLM may re-rank or pick among these
hypotheses, but it can never invent a cause the evidence does not support.
"""

from __future__ import annotations

from .cluster import Cluster
from .models import Anomaly, CauseKind, Hypothesis, MetricSample, Signal

DEPLOY_WINDOW_TICKS = 15


def diagnose(anomaly: Anomaly, cluster: Cluster, history: list[MetricSample]) -> list[Hypothesis]:
    name = anomaly.service
    spec = cluster.spec(name)
    sample = cluster.last(name)
    assert sample is not None
    sig = set(anomaly.signals)
    out: list[Hypothesis] = []
    baseline_rps = spec.base_rps

    # Dependency failure: a dependency is itself unhealthy
    for dep in spec.dependencies:
        ds = cluster.last(dep)
        dspec = cluster.spec(dep)
        if ds is not None and (ds.error_rate > dspec.slo_error_rate or ds.healthy_replicas == 0):
            out.append(
                Hypothesis(
                    cause=CauseKind.DEPENDENCY_FAILURE,
                    service=name,
                    target=dep,
                    confidence=0.85 if dspec.external else 0.6,
                    evidence=[
                        f"dependency {dep} error rate {ds.error_rate:.0%}",
                        f"{name} error rate {sample.error_rate:.0%} tracks its dependency",
                    ],
                )
            )

    deploys = cluster.recent_deploys(name, DEPLOY_WINDOW_TICKS)
    if deploys and sig & {Signal.ERROR_RATE, Signal.LATENCY}:
        d = deploys[-1]
        if d.note != "rollback":
            out.append(
                Hypothesis(
                    cause=CauseKind.BAD_DEPLOY,
                    service=name,
                    target=d.from_version,
                    confidence=0.75 if Signal.ERROR_RATE in sig else 0.55,
                    evidence=[
                        f"deploy {d.from_version} -> {d.to_version} {cluster.tick - d.tick} min before",
                        "symptoms started after the deploy",
                    ],
                )
            )

    mem_trend = [h.memory_pct for h in history[-4:]]
    if Signal.MEMORY in sig or (
        len(mem_trend) >= 3 and mem_trend[-1] - mem_trend[0] > 15 and mem_trend[-1] > 70
    ):
        out.append(
            Hypothesis(
                cause=CauseKind.MEMORY_LEAK,
                service=name,
                confidence=0.8,
                evidence=[
                    f"memory {sample.memory_pct:.0f}% and rising"
                    + (f" (from {mem_trend[0]:.0f}%)" if mem_trend else ""),
                    f"healthy replicas {sample.healthy_replicas}/{sample.replicas}",
                ],
            )
        )

    if sample.rps > baseline_rps * 1.8 and sig & {Signal.CPU, Signal.LATENCY, Signal.ERROR_RATE}:
        out.append(
            Hypothesis(
                cause=CauseKind.TRAFFIC_SPIKE,
                service=name,
                confidence=0.7,
                evidence=[
                    f"traffic {sample.rps:.0f} rps vs baseline {baseline_rps:.0f} rps",
                    f"cpu {sample.cpu_pct:.0f}%",
                ],
            )
        )

    if Signal.LATENCY in sig and sample.cpu_pct < 25 and sample.rps < baseline_rps * 1.5:
        out.append(
            Hypothesis(
                cause=CauseKind.STUCK_PROCESS,
                service=name,
                confidence=0.65,
                evidence=[
                    f"p95 latency {sample.p95_latency_ms:.0f} ms with cpu only {sample.cpu_pct:.0f}%",
                    "normal traffic: requests are queued, not processed",
                ],
            )
        )

    out.sort(key=lambda h: h.confidence, reverse=True)
    return out
