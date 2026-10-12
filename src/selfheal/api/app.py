"""FastAPI application: REST endpoints for healing runs plus the static dashboard."""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi import status as http
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from selfheal import __version__
from selfheal.api.jobs import (
    ApprovalError,
    QueueFull,
    RunFn,
    RunManager,
    RunRecord,
    build_provider,
    default_run,
    list_scenario_ids,
)
from selfheal.api.middleware import BodyLimitMiddleware, SecurityHeadersMiddleware
from selfheal.api.schemas import (
    SIMULATED_NOTE,
    ConfigOut,
    DecisionIn,
    DecisionOut,
    EventsOut,
    HealthOut,
    RunCreate,
    RunCreatedOut,
    RunDetailOut,
    RunListOut,
    RunSummaryOut,
    ScenarioListOut,
    ScenarioOut,
)
from selfheal.api.security import ApiKeyAuth, RateLimiter, Role
from selfheal.api.settings import ApiSettings
from selfheal.config import Settings
from selfheal.healer import HealerConfig
from selfheal.scenarios import SCENARIOS, get_scenario

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 64 * 1024
IdParam = Annotated[str, PathParam(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


@dataclass(frozen=True)
class Caller:
    """``owner`` tags new runs, ``scope`` limits visibility (``None`` = all runs),
    ``client`` keys rate limits and ``trusted`` unlocks real providers."""

    owner: str
    scope: str | None
    client: str
    role: Role
    trusted: bool
    key_id: str | None = None


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        # A proxy appends the address it saw, so the right-most entry is the one added by the
        # trusted edge; anything to its left is client-supplied and can be forged.
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
        hops = [h for h in hops if h]
        if hops:
            return hops[-1][:64]
    return request.client.host if request.client else "unknown"


def presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key and key.strip():
        return key.strip()
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _retry_after(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


@dataclass(frozen=True)
class AccessGuard:
    """Authenticates a request and applies the general per-caller rate limit."""

    settings: ApiSettings
    auth: ApiKeyAuth
    limiter: RateLimiter

    def identify(self, request: Request) -> Caller | None:
        if self.auth.enabled:
            identity = self.auth.verify(presented_key(request))
            if identity is None:
                return None
            fp = identity.fingerprint
            # Operators see their own runs; approvers review everyone's.
            scope = None if identity.role is Role.APPROVER else fp
            return Caller(fp, scope, f"key:{fp}", identity.role, True, fp[:8])
        ip = client_ip(request, self.settings.trust_proxy)
        if self.settings.dev_mode:
            return Caller("dev", None, f"ip:{ip}", Role.APPROVER, True)
        anon = "anon:" + hashlib.sha256(ip.encode()).hexdigest()[:12]
        return Caller(anon, anon, f"ip:{ip}", Role.APPROVER, False)

    def __call__(self, request: Request) -> Caller:
        who = self.identify(request)
        if who is None:
            raise HTTPException(
                http.HTTP_401_UNAUTHORIZED,
                "Missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        retry = self.limiter.check(who.client)
        if retry is not None:
            raise HTTPException(
                http.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers=_retry_after(retry)
            )
        return who


def get_caller(request: Request) -> Caller:
    guard: AccessGuard = request.app.state.guard
    return guard(request)


CallerDep = Annotated[Caller, Depends(get_caller)]


def _summary(record: RunRecord) -> RunSummaryOut:
    view = record.read()
    with record.lock:
        state, finished = record.state, record.finished_at
    summary = (view or {}).get("summary") or {}
    pending = (view or {}).get("pending_approvals") or []
    return RunSummaryOut(
        id=record.id,
        state=state,
        scenario=record.scenario.id,
        title=record.scenario.title,
        provider=record.provider,
        dry_run=record.dry_run,
        ticks=(view or {}).get("ticks") if view else None,
        pending_approvals=len(pending),
        resolved=int(summary.get("resolved", 0)),
        escalated=int(summary.get("escalated", 0)),
        open=int(summary.get("open", 0)),
        created_at=record.created_at,
        finished_at=finished,
    )


def _detail(record: RunRecord) -> RunDetailOut:
    view = record.read()
    with record.lock:
        report, error, started = record.report, record.error, record.started_at
    return RunDetailOut(
        **_summary(record).model_dump(),
        started_at=started,
        summary=(view or {}).get("summary"),
        incidents=list((view or {}).get("incidents") or []),
        services=list((view or {}).get("services") or []),
        pending=list((view or {}).get("pending_approvals") or []),
        event_count=len((view or {}).get("events") or []),
        passed=None if report is None else bool(report.get("passed")),
        error=error,
    )


def create_app(
    settings: ApiSettings | None = None,
    *,
    base_settings: Settings | None = None,
    run_fn: RunFn | None = None,
) -> FastAPI:
    """Build the application. Arguments exist mainly so tests can inject config."""
    cfg = settings or ApiSettings.from_env()
    base = base_settings or Settings.from_env()
    auth = ApiKeyAuth(cfg.operator_keys, cfg.approver_keys)
    run_limiter = RateLimiter(cfg.run_limit_per_minute)
    manager = RunManager(
        max_workers=cfg.max_concurrent_runs,
        max_active=cfg.max_queued_runs,
        max_kept=cfg.max_runs_kept,
        run_fn=run_fn or default_run,
        inline=cfg.sync_runs,
    )
    scenario_ids = list_scenario_ids()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        manager.shutdown()

    app = FastAPI(
        title="SelfHeal API",
        version=__version__,
        description=(
            "AI self-healing microservices control loop: detect anomalies, rank evidence-backed "
            "hypotheses, and apply allowlisted, verified remediation against a simulated cluster."
        ),
        lifespan=lifespan,
    )
    app.state.guard = AccessGuard(cfg, auth, RateLimiter(cfg.rate_limit_per_minute))
    app.state.manager = manager

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key", "Authorization"],
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(SecurityHeadersMiddleware)

    def load(run_id: str, who: Caller) -> RunRecord:
        record = manager.get(run_id, who.scope)
        if record is None:
            raise HTTPException(http.HTTP_404_NOT_FOUND, "Run not found")
        return record

    # ------------------------------------------------------------------ meta
    @app.get("/healthz", response_model=HealthOut, tags=["meta"])
    def healthz() -> HealthOut:
        return HealthOut(version=__version__, active_runs=manager.active_count())

    @app.get("/api/config", response_model=ConfigOut, tags=["meta"])
    def config(request: Request) -> ConfigOut:
        guard: AccessGuard = request.app.state.guard
        who = guard.identify(request) if presented_key(request) or not cfg.auth_enabled else None
        return ConfigOut(
            version=__version__,
            auth_required=cfg.auth_enabled,
            access_mode=cfg.access_mode,
            caller_role=who.role.value if who else None,
            providers=["mock"] if cfg.public_demo else ["mock", "openai", "groq", "ollama"],
            default_provider="mock" if cfg.public_demo else base.provider,
            max_ticks_cap=cfg.max_ticks_cap,
            sync_runs=cfg.sync_runs,
            scenarios=scenario_ids,
        )

    @app.get("/api/scenarios", response_model=ScenarioListOut, tags=["meta"])
    def scenarios(_: CallerDep) -> ScenarioListOut:
        out = [
            ScenarioOut(
                id=s.id,
                title=s.title,
                description=s.description,
                expected_root_service=s.expected_root_service,
                expected_action=s.expected_action,
                expected_status=s.expected_status,
                ticks=s.ticks,
            )
            for s in (SCENARIOS[i] for i in scenario_ids)
        ]
        return ScenarioListOut(scenarios=out)

    # ----------------------------------------------------------------- runs
    @app.post(
        "/api/runs",
        response_model=RunCreatedOut,
        status_code=http.HTTP_202_ACCEPTED,
        tags=["runs"],
        responses={200: {"description": "Finished run (sync mode)"}},
    )
    def create_run(body: RunCreate, who: CallerDep, response: Response) -> RunCreatedOut:
        if body.ticks is not None and body.ticks > cfg.max_ticks_cap:
            raise HTTPException(
                http.HTTP_422_UNPROCESSABLE_CONTENT, f"ticks must be <= {cfg.max_ticks_cap}"
            )
        try:
            get_scenario(body.scenario)
        except KeyError as exc:
            raise HTTPException(
                http.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown scenario {body.scenario!r}"
            ) from exc
        provider_name = body.provider or ("mock" if cfg.public_demo else base.provider)
        if provider_name != "mock" and not who.trusted:
            raise HTTPException(
                http.HTTP_403_FORBIDDEN, "Only the offline mock provider is enabled here"
            )
        # Only well-formed requests count towards the stricter run-creation limit.
        retry = run_limiter.check(who.client)
        if retry is not None:
            raise HTTPException(
                http.HTTP_429_TOO_MANY_REQUESTS,
                "Run creation limit exceeded",
                headers=_retry_after(retry),
            )
        try:
            provider = build_provider(provider_name)
        except ValueError as exc:
            raise HTTPException(http.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        config = HealerConfig(dry_run=body.dry_run)
        try:
            record = manager.submit(
                owner=who.owner,
                scenario_id=body.scenario,
                provider=provider,
                config=config,
                ticks=body.ticks,
            )
        except QueueFull as exc:
            raise HTTPException(
                http.HTTP_503_SERVICE_UNAVAILABLE, str(exc), headers={"Retry-After": "10"}
            ) from exc
        detail = _detail(record).model_dump()
        if not manager.inline:
            return RunCreatedOut(**detail)  # 202: poll the events endpoint
        response.status_code = http.HTTP_200_OK
        view = record.read() or {}
        return RunCreatedOut(**detail, events=list(view.get("events") or []))

    @app.get("/api/runs", response_model=RunListOut, tags=["runs"])
    def list_runs(who: CallerDep, limit: Annotated[int, Query(ge=1, le=100)] = 25) -> RunListOut:
        return RunListOut(runs=[_summary(r) for r in manager.list(who.scope, limit)])

    @app.get("/api/runs/{run_id}", response_model=RunDetailOut, tags=["runs"])
    def get_run(run_id: IdParam, who: CallerDep) -> RunDetailOut:
        return _detail(load(run_id, who))

    @app.get("/api/runs/{run_id}/events", response_model=EventsOut, tags=["runs"])
    def get_events(
        run_id: IdParam,
        who: CallerDep,
        since: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> EventsOut:
        record = load(run_id, who)
        view = record.read() or {}
        with record.lock:
            state, done = record.state, record.done
        events = list(view.get("events") or [])[since:]
        return EventsOut(
            id=record.id,
            state=state,
            done=done,
            next_cursor=since + len(events),
            events=events,
            pending_approvals=list(view.get("pending_approvals") or []),
            summary=view.get("summary"),
            services=list(view.get("services") or []),
        )

    def decide(
        run_id: str, incident_id: str, body: DecisionIn, who: Caller, *, approved: bool
    ) -> DecisionOut:
        if who.role is not Role.APPROVER:
            raise HTTPException(
                http.HTTP_403_FORBIDDEN, "Approving or rejecting remediation needs an approver key"
            )
        record = load(run_id, who)
        approver = f"{body.approver} (key {who.key_id})" if who.key_id else body.approver
        try:
            result = manager.decide(
                record, incident_id, approver, approved=approved, reason=body.reason
            )
        except ApprovalError as exc:
            raise HTTPException(http.HTTP_409_CONFLICT, str(exc)) from exc
        return DecisionOut(
            run_id=record.id,
            incident_id=result["incident_id"],
            status=result["status"],
            pending_approvals=result["pending_approvals"],
            summary=result["summary"],
            simulated_note=SIMULATED_NOTE,
        )

    @app.post(
        "/api/runs/{run_id}/approvals/{incident_id}/approve",
        response_model=DecisionOut,
        tags=["approvals"],
    )
    def approve_action(
        run_id: IdParam, incident_id: IdParam, body: DecisionIn, who: CallerDep
    ) -> DecisionOut:
        """Approve a protected action; it then runs against the simulator and is verified."""
        return decide(run_id, incident_id, body, who, approved=True)

    @app.post(
        "/api/runs/{run_id}/approvals/{incident_id}/reject",
        response_model=DecisionOut,
        tags=["approvals"],
    )
    def reject_action(
        run_id: IdParam, incident_id: IdParam, body: DecisionIn, who: CallerDep
    ) -> DecisionOut:
        return decide(run_id, incident_id, body, who, approved=False)

    # ------------------------------------------------------------- dashboard
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
