"""API-key authentication with roles and an in-memory sliding-window rate limiter."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    OPERATOR = "operator"
    APPROVER = "approver"


def fingerprint(secret: str) -> str:
    """Stable, non-reversible identifier for a key (safe to store and log)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class KeyIdentity:
    fingerprint: str
    role: Role


class ApiKeyAuth:
    """Validates presented keys against the configured sets in constant time.

    Only SHA-256 digests of the keys are kept in memory. Every configured digest is compared
    with ``hmac.compare_digest`` on every request (no early exit), so timing reveals neither
    key contents nor which key or role matched.
    """

    def __init__(self, operator_keys: tuple[str, ...], approver_keys: tuple[str, ...]) -> None:
        self._digests: list[tuple[bytes, Role]] = [
            (self._digest(k), Role.OPERATOR) for k in operator_keys if k
        ] + [(self._digest(k), Role.APPROVER) for k in approver_keys if k]

    @staticmethod
    def _digest(secret: str) -> bytes:
        return hashlib.sha256(secret.encode("utf-8")).digest()

    @property
    def enabled(self) -> bool:
        return bool(self._digests)

    def verify(self, presented: str | None) -> KeyIdentity | None:
        if not presented:
            return None
        candidate = self._digest(presented)
        role: Role | None = None
        for digest, key_role in self._digests:
            if hmac.compare_digest(candidate, digest) and role is not Role.APPROVER:
                role = key_role
        return KeyIdentity(fingerprint(presented), role) if role else None


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` hits per ``window_s`` per identity."""

    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_identities: int = 10_000,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._max_identities = max_identities
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, identity: str) -> float | None:
        """Record a hit. Returns ``None`` if allowed, else seconds until retry."""
        if self.limit <= 0:
            return None
        now = self._clock()
        with self._lock:
            hits = self._hits.get(identity)
            if hits is None:
                if len(self._hits) >= self._max_identities:
                    self._evict(now)
                hits = self._hits[identity] = deque()
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(0.0, self.window_s - (now - hits[0]))
            hits.append(now)
            return None

    def _evict(self, now: float) -> None:
        stale = [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window_s]
        for key in stale:
            del self._hits[key]
        if len(self._hits) >= self._max_identities:
            oldest = min(self._hits, key=lambda k: self._hits[k][-1])
            del self._hits[oldest]
