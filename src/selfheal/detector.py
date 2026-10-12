"""Anomaly detection: SLO thresholds plus an EWMA baseline, debounced over ticks."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Anomaly, MetricSample, ServiceSpec, Signal


@dataclass
class _Baseline:
    mean: dict[str, float] = field(default_factory=dict)
    var: dict[str, float] = field(default_factory=dict)
    count: int = 0


@dataclass
class DetectorConfig:
    warmup_ticks: int = 3
    debounce_ticks: int = 2
    alpha: float = 0.3
    z_threshold: float = 4.0
    cpu_pct: float = 90.0
    memory_pct: float = 88.0


TRACKED = ("error_rate", "p95_latency_ms", "cpu_pct", "memory_pct")


class Detector:
    """Flags a service only after `debounce_ticks` consecutive breaching samples.

    Baselines are only updated with healthy samples so a slow degradation cannot
    teach the detector that "broken" is normal.
    """

    def __init__(self, specs: dict[str, ServiceSpec], config: DetectorConfig | None = None) -> None:
        self.specs = specs
        self.config = config or DetectorConfig()
        self._base: dict[str, _Baseline] = {name: _Baseline() for name in specs}
        self._streak: dict[str, int] = dict.fromkeys(specs, 0)

    def breaches(self, s: MetricSample) -> tuple[list[Signal], dict[str, float]]:
        spec, cfg = self.specs[s.service], self.config
        signals: list[Signal] = []
        details: dict[str, float] = {}
        if s.healthy_replicas == 0:
            signals.append(Signal.AVAILABILITY)
        if s.error_rate > spec.slo_error_rate:
            signals.append(Signal.ERROR_RATE)
        if s.p95_latency_ms > spec.slo_latency_ms:
            signals.append(Signal.LATENCY)
        if s.cpu_pct > cfg.cpu_pct:
            signals.append(Signal.CPU)
        if s.memory_pct > cfg.memory_pct:
            signals.append(Signal.MEMORY)
        base = self._base[s.service]
        if base.count >= cfg.warmup_ticks:
            for metric, sig in zip(
                TRACKED, (Signal.ERROR_RATE, Signal.LATENCY, Signal.CPU, Signal.MEMORY), strict=True
            ):
                value = float(getattr(s, metric))
                std = max(base.var[metric] ** 0.5, 0.05 * abs(base.mean[metric]) + 1e-3)
                z = (value - base.mean[metric]) / std
                if z > cfg.z_threshold:
                    details[f"{metric}_z"] = round(z, 1)
                    # z-score alone is a warning sign; it becomes a signal only for memory
                    # (leaks are visible long before the hard threshold)
                    if sig is Signal.MEMORY and sig not in signals and value > 60:
                        signals.append(sig)
        details.update(
            error_rate=s.error_rate,
            p95_latency_ms=s.p95_latency_ms,
            cpu_pct=s.cpu_pct,
            memory_pct=s.memory_pct,
        )
        return signals, details

    def _learn(self, s: MetricSample) -> None:
        base, a = self._base[s.service], self.config.alpha
        for metric in TRACKED:
            v = float(getattr(s, metric))
            if base.count == 0:
                base.mean[metric], base.var[metric] = v, 0.0
            else:
                diff = v - base.mean[metric]
                base.mean[metric] += a * diff
                base.var[metric] = (1 - a) * (base.var[metric] + a * diff * diff)
        base.count += 1

    def observe(self, samples: dict[str, MetricSample]) -> list[Anomaly]:
        found: list[Anomaly] = []
        for name, s in samples.items():
            signals, details = self.breaches(s)
            if not signals:
                self._streak[name] = 0
                self._learn(s)
                continue
            self._streak[name] += 1
            if self._streak[name] >= self.config.debounce_ticks:
                found.append(Anomaly(service=name, tick=s.tick, signals=signals, details=details))
        return found

    def is_healthy(self, s: MetricSample) -> bool:
        return not self.breaches(s)[0]
