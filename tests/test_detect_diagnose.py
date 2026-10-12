from selfheal.cluster import Cluster, Fault, FaultKind
from selfheal.detector import Detector, DetectorConfig
from selfheal.diagnosis import diagnose
from selfheal.models import CauseKind, MetricSample, ServiceSpec, Signal


def sample(**kw):
    base = dict(
        tick=1,
        service="api",
        rps=100,
        error_rate=0.004,
        p95_latency_ms=40,
        cpu_pct=40,
        memory_pct=40,
        replicas=2,
        healthy_replicas=2,
        version="1.0.0",
    )
    return MetricSample(**(base | kw))


def test_debounce_requires_consecutive_breaches():
    d = Detector({"api": ServiceSpec(name="api")}, DetectorConfig(debounce_ticks=2))
    assert d.observe({"api": sample(error_rate=0.5)}) == []
    assert d.observe({"api": sample(error_rate=0.004)}) == []  # streak reset
    assert d.observe({"api": sample(error_rate=0.5)}) == []
    found = d.observe({"api": sample(error_rate=0.5)})
    assert found and Signal.ERROR_RATE in found[0].signals


def test_thresholds_cover_all_signals():
    d = Detector({"api": ServiceSpec(name="api")})
    sigs, _ = d.breaches(
        sample(error_rate=1, p95_latency_ms=900, cpu_pct=99, memory_pct=95, healthy_replicas=0)
    )
    assert set(sigs) == {
        Signal.AVAILABILITY,
        Signal.ERROR_RATE,
        Signal.LATENCY,
        Signal.CPU,
        Signal.MEMORY,
    }


def test_ewma_flags_memory_growth_before_hard_limit():
    d = Detector({"api": ServiceSpec(name="api")}, DetectorConfig(warmup_ticks=3))
    for _ in range(5):
        d.observe({"api": sample(memory_pct=40)})
    sigs, details = d.breaches(sample(memory_pct=75))
    assert Signal.MEMORY in sigs and "memory_pct_z" in details


def _diag(faults, service, ticks):
    specs = [ServiceSpec(name="db"), ServiceSpec(name="api", dependencies=["db"])]
    c = Cluster(specs, faults)
    det = Detector({s.name: s for s in specs})
    hist = []
    for _ in range(ticks):
        out = c.step()
        hist.append(out[service])
        anomalies = {a.service: a for a in det.observe(out)}
    return diagnose(anomalies[service], c, hist)


def test_diagnoses_bad_deploy():
    hyps = _diag([Fault(FaultKind.BAD_DEPLOY, "api", 2, to_version="1.1.0")], "api", 4)
    assert hyps[0].cause is CauseKind.BAD_DEPLOY and hyps[0].target == "1.0.0"


def test_diagnoses_memory_leak():
    hyps = _diag([Fault(FaultKind.MEMORY_LEAK, "api", 1)], "api", 7)
    assert hyps[0].cause is CauseKind.MEMORY_LEAK


def test_diagnoses_dependency_failure():
    hyps = _diag([Fault(FaultKind.OUTAGE, "db", 2)], "api", 4)
    assert hyps[0].cause is CauseKind.DEPENDENCY_FAILURE and hyps[0].target == "db"


def test_diagnoses_stuck_process_and_traffic():
    assert _diag([Fault(FaultKind.STUCK, "api", 2)], "api", 4)[0].cause is CauseKind.STUCK_PROCESS
    hyps = _diag([Fault(FaultKind.TRAFFIC_SPIKE, "api", 2, magnitude=4)], "api", 4)
    assert hyps[0].cause is CauseKind.TRAFFIC_SPIKE
