"""Background scenario runs with bounded concurrency and an in-memory store."""

from __future__ import annotations

import copy
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from selfheal.engine import evaluate, pending_approvals, run_scenario, snapshot
from selfheal.healer import Healer, HealerConfig
from selfheal.llm import MockProvider, Provider, provider_from_env
from selfheal.scenarios import SCENARIOS, Scenario, get_scenario

JobState = Literal["queued", "running", "finished", "error"]
ProgressFn = Callable[[dict[str, Any]], None]
RunFn = Callable[
    [str, Provider, HealerConfig, int | None, ProgressFn], tuple[Healer, dict[str, Any]]
]


def default_run(
    scenario_id: str,
    provider: Provider,
    config: HealerConfig,
    ticks: int | None,
    on_progress: ProgressFn,
) -> tuple[Healer, dict[str, Any]]:
    return run_scenario(
        scenario_id, provider=provider, config=config, ticks=ticks, on_progress=on_progress
    )


def _now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return "run_" + secrets.token_hex(6)


class QueueFull(RuntimeError):
    """Too many runs are queued or running."""


class ApprovalError(RuntimeError):
    """Approve/reject cannot proceed (wrong state, missing incident, ...)."""


@dataclass
class RunRecord:
    """One scenario run as tracked by the service.

    The worker thread mutates its own live :class:`Healer`; readers only ever see
    ``view``, a deep-copied snapshot published after each tick, so a poll never observes a
    half-updated object.
    """

    id: str
    owner: str
    scenario: Scenario
    provider: str
    ticks: int | None
    dry_run: bool
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    state: JobState = "queued"
    view: dict[str, Any] | None = None
    healer: Healer | None = None
    report: dict[str, Any] | None = None
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def done(self) -> bool:
        return self.state in ("finished", "error")

    def publish(self, snap: dict[str, Any]) -> None:
        with self.lock:
            self.view = copy.deepcopy(snap)

    def read(self) -> dict[str, Any] | None:
        with self.lock:
            return None if self.view is None else copy.deepcopy(self.view)


class RunManager:
    """Owns the worker pool and the run records.

    Runs execute in a thread pool. ``max_active`` bounds queued + running work so a burst
    cannot pile up unbounded jobs (callers get :class:`QueueFull` -> HTTP 503); the oldest
    finished runs are evicted beyond ``max_kept``. With ``inline=True`` there is no pool:
    :meth:`submit` runs the scenario in the calling thread (serverless mode).
    """

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_active: int = 8,
        max_kept: int = 200,
        run_fn: RunFn = default_run,
        inline: bool = False,
        continue_ticks: int = 12,
    ) -> None:
        self.inline = inline
        self._continue_ticks = continue_ticks
        self._executor = (
            None
            if inline
            else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="selfheal")
        )
        self._max_active = max_active
        self._max_kept = max_kept
        self._run_fn = run_fn
        self._records: OrderedDict[str, RunRecord] = OrderedDict()
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        owner: str,
        scenario_id: str,
        provider: Provider,
        config: HealerConfig,
        ticks: int | None,
    ) -> RunRecord:
        scenario = get_scenario(scenario_id)
        record = RunRecord(
            id=new_id(),
            owner=owner,
            scenario=scenario,
            provider=provider.name,
            ticks=ticks,
            dry_run=config.dry_run,
        )
        with self._lock:
            if self._active_unlocked() >= self._max_active:
                raise QueueFull("Too many healing runs in progress; try again shortly")
            self._records[record.id] = record
            self._evict_unlocked()
        if self._executor is None:
            self._execute(record, provider, config, ticks)
        else:
            self._executor.submit(self._execute, record, provider, config, ticks)
        return record

    def get(self, run_id: str, owner: str | None) -> RunRecord | None:
        """Look up a run; ``owner=None`` means the caller may see every run."""
        with self._lock:
            record = self._records.get(run_id)
        if record is None or (owner is not None and record.owner != owner):
            return None
        return record

    def list(self, owner: str | None, limit: int = 50) -> list[RunRecord]:
        with self._lock:
            records = [
                r for r in reversed(self._records.values()) if owner is None or r.owner == owner
            ]
        return records[:limit]

    def active_count(self) -> int:
        with self._lock:
            return self._active_unlocked()

    def decide(
        self,
        record: RunRecord,
        incident_id: str,
        approver: str,
        *,
        approved: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        """Apply a human decision on a protected action and continue the loop briefly."""
        with record.lock:
            healer = record.healer
            if healer is None or record.state != "finished":
                raise ApprovalError("the scenario run has not finished yet")
            sc_meta = {
                "id": record.scenario.id,
                "title": record.scenario.title,
                "description": record.scenario.description,
                "expected_root_service": record.scenario.expected_root_service,
                "expected_action": record.scenario.expected_action,
                "expected_status": record.scenario.expected_status,
            }
            try:
                if approved:
                    healer.approve(incident_id, approver)
                else:
                    healer.reject(incident_id, approver, reason)
            except ValueError as exc:
                raise ApprovalError(str(exc)) from exc
            for _ in range(self._continue_ticks):
                healer.step()
            snap = snapshot(healer, scenario=sc_meta)
            report = healer.report()
            report["scenario"] = sc_meta
            report["passed"] = evaluate(report)
            report["services"] = snap["services"]
            report["pending_approvals"] = snap["pending_approvals"]
            record.view = copy.deepcopy(snap)
            record.report = report
            pending = pending_approvals(healer)
            status = next(
                (i.status.value for i in healer.incidents if i.id == incident_id), "unknown"
            )
            return {
                "incident_id": incident_id,
                "status": status,
                "pending_approvals": pending,
                "summary": report["summary"],
            }

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _active_unlocked(self) -> int:
        return sum(1 for r in self._records.values() if not r.done)

    def _evict_unlocked(self) -> None:
        overflow = len(self._records) - self._max_kept
        if overflow <= 0:
            return
        for rid in [rid for rid, r in self._records.items() if r.done][:overflow]:
            del self._records[rid]

    def _execute(
        self, record: RunRecord, provider: Provider, config: HealerConfig, ticks: int | None
    ) -> None:
        with record.lock:
            record.state = "running"
            record.started_at = _now()
        try:
            healer, report = self._run_fn(
                record.scenario.id, provider, config, ticks, record.publish
            )
        except Exception as exc:  # a crashed run must never take down the worker
            with record.lock:
                record.state = "error"
                record.error = f"{type(exc).__name__}: {exc}"[:500]
                record.finished_at = _now()
            return
        with record.lock:
            record.healer = healer
            record.report = report
            record.publish(
                snapshot(
                    healer,
                    scenario={
                        "id": record.scenario.id,
                        "title": record.scenario.title,
                        "description": record.scenario.description,
                        "expected_root_service": record.scenario.expected_root_service,
                        "expected_action": record.scenario.expected_action,
                        "expected_status": record.scenario.expected_status,
                    },
                )
            )
            record.state = "finished"
            record.finished_at = _now()


def build_provider(name: str) -> Provider:
    if name == "mock":
        return MockProvider()
    return provider_from_env({**dict(__import__("os").environ), "SELFHEAL_PROVIDER": name})


def list_scenario_ids() -> list[str]:
    return sorted(SCENARIOS)
