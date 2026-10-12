"""Configuration for the HTTP service, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from selfheal.config import env_bool, env_int

AccessMode = Literal["api-key", "dev", "public-demo"]


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def running_serverless() -> bool:
    """True on serverless platforms where background threads do not outlive a request."""
    return any(os.getenv(name) for name in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME"))


@dataclass(frozen=True)
class ApiSettings:
    """Service settings.

    Access modes:

    * keys configured -> every ``/api`` call needs ``X-API-Key`` (or a Bearer token).
      ``SELFHEAL_API_KEYS`` grants the *operator* role (start and follow runs);
      ``SELFHEAL_APPROVER_KEYS`` grants the *approver* role (also approve/reject protected actions).
    * ``dev_mode``    -> no auth, caller acts as approver; meant for ``localhost`` only.
    * neither         -> public demo: anonymous callers may only replay packaged scenarios with
      the offline mock provider. Approvals are allowed because every action executes against
      the simulated cluster, and responses say so.

    ``sync_runs`` runs the scenario inside ``POST /api/runs`` instead of a background worker.
    Serverless hosts need it (auto-enabled when ``VERCEL`` or ``AWS_LAMBDA_FUNCTION_NAME`` is set)
    because threads are frozen once a response is sent.
    """

    operator_keys: tuple[str, ...] = ()
    approver_keys: tuple[str, ...] = ()
    dev_mode: bool = False
    cors_origins: tuple[str, ...] = ()
    rate_limit_per_minute: int = 120
    run_limit_per_minute: int = 6
    max_concurrent_runs: int = 2
    max_queued_runs: int = 8
    max_runs_kept: int = 200
    max_ticks_cap: int = 120
    trust_proxy: bool = False
    sync_runs: bool = False

    @property
    def auth_enabled(self) -> bool:
        return bool(self.operator_keys or self.approver_keys)

    @property
    def access_mode(self) -> AccessMode:
        if self.auth_enabled:
            return "api-key"
        return "dev" if self.dev_mode else "public-demo"

    @property
    def public_demo(self) -> bool:
        return self.access_mode == "public-demo"

    @classmethod
    def from_env(cls) -> ApiSettings:
        return cls(
            operator_keys=_env_list("SELFHEAL_API_KEYS"),
            approver_keys=_env_list("SELFHEAL_APPROVER_KEYS"),
            dev_mode=env_bool("SELFHEAL_DEV_MODE", False),
            cors_origins=_env_list("SELFHEAL_CORS_ORIGINS"),
            rate_limit_per_minute=env_int("SELFHEAL_RATE_LIMIT_PER_MINUTE", 120),
            run_limit_per_minute=env_int("SELFHEAL_RUN_LIMIT_PER_MINUTE", 6),
            max_concurrent_runs=max(1, env_int("SELFHEAL_MAX_CONCURRENT_RUNS", 2)),
            max_queued_runs=max(1, env_int("SELFHEAL_MAX_QUEUED_RUNS", 8)),
            max_runs_kept=max(10, env_int("SELFHEAL_MAX_RUNS_KEPT", 200)),
            max_ticks_cap=max(1, env_int("SELFHEAL_MAX_TICKS_CAP", 120)),
            trust_proxy=env_bool("SELFHEAL_TRUST_PROXY", False),
            sync_runs=env_bool("SELFHEAL_SYNC_RUNS", running_serverless()),
        )
