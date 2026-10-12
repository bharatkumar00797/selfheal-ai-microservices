"""Core domain models shared by the simulator, detector, diagnosis and healer."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


def _check_name(value: str) -> str:
    if not NAME_RE.match(value):
        raise ValueError("names must be lowercase letters, digits and dashes (max 40 chars)")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceSpec(Strict):
    """Static description of a microservice in the simulated cluster."""

    name: str
    version: str = "1.0.0"
    replicas: int = Field(default=2, ge=1, le=50)
    max_replicas: int = Field(default=10, ge=1, le=50)
    base_rps: float = Field(default=100.0, gt=0, le=100_000)
    capacity_rps_per_replica: float = Field(default=80.0, gt=0, le=100_000)
    base_latency_ms: float = Field(default=40.0, gt=0, le=10_000)
    slo_latency_ms: float = Field(default=300.0, gt=0, le=60_000)
    slo_error_rate: float = Field(default=0.05, gt=0, le=1)
    dependencies: list[str] = Field(default_factory=list, max_length=10)
    protected: bool = False
    external: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_name(v)

    @field_validator("dependencies")
    @classmethod
    def _deps(cls, v: list[str]) -> list[str]:
        return [_check_name(d) for d in v]

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not VERSION_RE.match(v):
            raise ValueError("version must look like 1.2.3")
        return v


class MetricSample(Strict):
    """One observation of one service at one tick (1 tick = 1 simulated minute)."""

    tick: int
    service: str
    rps: float
    error_rate: float
    p95_latency_ms: float
    cpu_pct: float
    memory_pct: float
    replicas: int
    healthy_replicas: int
    version: str


class Signal(StrEnum):
    ERROR_RATE = "error_rate"
    LATENCY = "latency"
    CPU = "cpu"
    MEMORY = "memory"
    AVAILABILITY = "availability"


class Anomaly(Strict):
    service: str
    tick: int
    signals: list[Signal]
    details: dict[str, float]


class CauseKind(StrEnum):
    BAD_DEPLOY = "bad_deploy"
    MEMORY_LEAK = "memory_leak"
    TRAFFIC_SPIKE = "traffic_spike"
    DEPENDENCY_FAILURE = "dependency_failure"
    STUCK_PROCESS = "stuck_process"


class ActionKind(StrEnum):
    RESTART = "restart_service"
    SCALE_OUT = "scale_out"
    ROLLBACK = "rollback_deploy"
    CIRCUIT_BREAK = "open_circuit_breaker"


class Hypothesis(Strict):
    cause: CauseKind
    service: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]
    target: str | None = None  # e.g. the failing dependency


class Action(Strict):
    kind: ActionKind
    service: str
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class EventType(StrEnum):
    ANOMALY = "anomaly"
    DIAGNOSIS = "diagnosis"
    PLAN = "plan"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    EXECUTED = "executed"
    DRY_RUN = "dry_run"
    VERIFIED = "verified"
    VERIFY_FAILED = "verify_failed"
    REVERTED = "reverted"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    WARNING = "warning"


class Event(Strict):
    tick: int
    service: str
    type: EventType
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
