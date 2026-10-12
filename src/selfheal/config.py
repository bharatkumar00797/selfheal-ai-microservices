"""Shared environment helpers and LLM settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ProviderName = Literal["mock", "openai", "groq", "ollama"]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """LLM provider settings used by the CLI and the HTTP service."""

    provider: ProviderName = "mock"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> Settings:
        name = (os.getenv("SELFHEAL_PROVIDER") or "mock").strip().lower() or "mock"
        if name not in {"mock", "openai", "groq", "ollama"}:
            raise ValueError(f"unknown SELFHEAL_PROVIDER {name!r}")
        return cls(
            provider=name,  # type: ignore[arg-type]
            model=os.getenv("SELFHEAL_MODEL") or None,
            base_url=os.getenv("SELFHEAL_BASE_URL") or None,
            api_key=os.getenv("SELFHEAL_API_KEY") or None,
            timeout_seconds=float(os.getenv("SELFHEAL_TIMEOUT_SECONDS") or 20),
        )
