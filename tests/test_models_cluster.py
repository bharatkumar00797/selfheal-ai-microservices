import pytest
from pydantic import ValidationError

from selfheal.cluster import Cluster, ClusterError, Fault, FaultKind
from selfheal.models import Action, ActionKind, ServiceSpec


def make(faults=None, **kw):
    specs = [
        ServiceSpec(name="db", protected=True),
        ServiceSpec(name="api", dependencies=["db"], **kw),
        ServiceSpec(name="vendor", external=True),
    ]
    return Cluster(specs, faults or [])


def test_spec_validation():
    with pytest.raises(ValidationError):
        ServiceSpec(name="Bad Name")
    with pytest.raises(ValidationError):
        ServiceSpec(name="ok", version="latest")
    with pytest.raises(ValidationError):
        ServiceSpec(name="ok", unexpected=1)


def test_unknown_dependency_and_duplicates_rejected():
    with pytest.raises(ClusterError):
        Cluster([ServiceSpec(name="a", dependencies=["missing"])], [])
    with pytest.raises(ClusterError):
        Cluster([ServiceSpec(name="a"), ServiceSpec(name="a")], [])


def test_simulation_is_deterministic():
    a, b = make(), make()
    for _ in range(5):
        assert a.step() == b.step()


def test_healthy_baseline():
    c = make()
    s = c.step()["api"]
    assert s.error_rate < 0.05 and s.p95_latency_ms < 300 and s.healthy_replicas == 2


def test_bad_deploy_raises_errors_and_rollback_fixes():
    c = make([Fault(FaultKind.BAD_DEPLOY, "api", 1, to_version="1.1.0")])
    assert c.step()["api"].error_rate > 0.3
    c.apply(Action(kind=ActionKind.ROLLBACK, service="api", params={"to_version": "1.0.0"}))
    assert c.step()["api"].error_rate < 0.05


def test_rollback_to_unknown_version_rejected():
    c = make()
    with pytest.raises(ClusterError):
        c.apply(Action(kind=ActionKind.ROLLBACK, service="api", params={"to_version": "0.1.0"}))


def test_memory_leak_ooms_and_restart_recovers():
    c = make([Fault(FaultKind.MEMORY_LEAK, "api", 1)])
    for _ in range(8):
        s = c.step()["api"]
    assert s.memory_pct == 100 and s.healthy_replicas < s.replicas
    c.apply(Action(kind=ActionKind.RESTART, service="api"))
    assert c.step()["api"].memory_pct < 60


def test_dependency_errors_cascade_and_breaker_stops_them():
    c = make([Fault(FaultKind.OUTAGE, "db", 1)])
    assert c.step()["api"].error_rate > 0.5
    c.apply(Action(kind=ActionKind.CIRCUIT_BREAK, service="api", params={"dependency": "db"}))
    assert c.step()["api"].error_rate < 0.05


def test_external_services_cannot_be_changed():
    c = make()
    with pytest.raises(ClusterError):
        c.apply(Action(kind=ActionKind.RESTART, service="vendor"))


def test_breaker_requires_real_dependency_and_scale_bounds():
    c = make()
    with pytest.raises(ClusterError):
        c.apply(
            Action(kind=ActionKind.CIRCUIT_BREAK, service="api", params={"dependency": "vendor"})
        )
    with pytest.raises(ClusterError):
        c.apply(Action(kind=ActionKind.SCALE_OUT, service="api", params={"replicas": 99}))


def test_traffic_spike_saturates_and_scale_out_helps():
    c = make([Fault(FaultKind.TRAFFIC_SPIKE, "api", 1, magnitude=4)])
    hot = c.step()["api"]
    assert hot.cpu_pct > 90 and hot.error_rate > 0.05
    c.apply(Action(kind=ActionKind.SCALE_OUT, service="api", params={"replicas": 9}))
    cool = c.step()["api"]
    assert cool.error_rate < 0.05 and cool.cpu_pct < hot.cpu_pct
