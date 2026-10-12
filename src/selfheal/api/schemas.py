"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from selfheal.api.jobs import JobState
from selfheal.api.settings import AccessMode

SIMULATED_NOTE = (
    "Actions execute against a simulated microservice cluster; no real infrastructure is touched."
)


def _no_control_chars(value: str, field: str) -> str:
    if any(ord(ch) < 32 for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    return value.strip()


class RunCreate(BaseModel):
    """Start a healing loop for a packaged chaos scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
        description="packaged scenario id (see GET /api/scenarios)",
        examples=["bad-deploy"],
    )
    ticks: int | None = Field(default=None, ge=1, le=1000)
    dry_run: bool = False
    provider: Literal["mock", "openai", "groq", "ollama"] | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1, max_length=80, description="name recorded on the audit")
    reason: str = Field(default="", max_length=500)

    @field_validator("approver")
    @classmethod
    def _clean_approver(cls, value: str) -> str:
        value = _no_control_chars(value, "approver")
        if not value:
            raise ValueError("approver must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        return " ".join(value.split())


class RunSummaryOut(BaseModel):
    id: str
    state: JobState
    scenario: str
    title: str
    provider: str
    dry_run: bool = False
    ticks: int | None = None
    pending_approvals: int = 0
    resolved: int = 0
    escalated: int = 0
    open: int = 0
    created_at: datetime
    finished_at: datetime | None = None
    simulated: bool = True


class RunDetailOut(RunSummaryOut):
    simulated_note: str = SIMULATED_NOTE
    started_at: datetime | None = None
    summary: dict[str, Any] | None = None
    incidents: list[dict[str, Any]] = Field(default_factory=list)
    services: list[dict[str, Any]] = Field(default_factory=list)
    pending: list[dict[str, Any]] = Field(default_factory=list)
    event_count: int = 0
    passed: bool | None = None
    error: str | None = None


class RunCreatedOut(RunDetailOut):
    """Response of ``POST /api/runs``.

    In background mode the run is still ``queued``/``running``: poll the events endpoint. In
    sync (serverless) mode it has finished and ``events`` holds every entry.
    """

    events: list[dict[str, Any]] = Field(default_factory=list)


class RunListOut(BaseModel):
    runs: list[RunSummaryOut]


class EventsOut(BaseModel):
    id: str
    state: JobState
    done: bool
    next_cursor: int = Field(description="pass as ?since= to fetch only newer events")
    events: list[dict[str, Any]]
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] | None = None
    services: list[dict[str, Any]] = Field(default_factory=list)


class DecisionOut(BaseModel):
    run_id: str
    incident_id: str
    status: str
    simulated: bool = True
    simulated_note: str = SIMULATED_NOTE
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] | None = None


class ScenarioOut(BaseModel):
    id: str
    title: str
    description: str
    expected_root_service: str
    expected_action: str
    expected_status: str
    ticks: int


class ScenarioListOut(BaseModel):
    scenarios: list[ScenarioOut]


class ConfigOut(BaseModel):
    version: str
    auth_required: bool
    access_mode: AccessMode
    caller_role: Literal["operator", "approver"] | None = Field(
        default=None, description="role of the presented key (null when not authenticated)"
    )
    providers: list[str]
    default_provider: str
    max_ticks_cap: int
    sync_runs: bool = Field(description="scenario runs finish within POST /api/runs")
    simulated_environment: bool = True
    simulated_note: str = SIMULATED_NOTE
    scenarios: list[str]


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    active_runs: int
