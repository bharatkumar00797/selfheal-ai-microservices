"""Deployment configs stay consistent with the app: env var names, $PORT, health checks."""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from selfheal.scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "selfheal"


def _known_env_vars() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        names |= set(re.findall(r'"(SELFHEAL_[A-Z_]+)"', path.read_text(encoding="utf-8")))
    return names


def _documented_env_vars() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^(SELFHEAL_[A-Z_]+)=", text, flags=re.MULTILINE))


def test_env_example_documents_every_setting() -> None:
    known = _known_env_vars()
    assert {
        "SELFHEAL_PROVIDER",
        "SELFHEAL_API_KEYS",
        "SELFHEAL_SYNC_RUNS",
        "SELFHEAL_TRUST_PROXY",
    } <= known
    assert known == _documented_env_vars()


def test_render_blueprint() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "runtime: docker" in text
    assert "healthCheckPath: /healthz" in text
    keys = set(re.findall(r"- key: (\S+)", text))
    assert keys and keys <= _known_env_vars()
    assert "SELFHEAL_API_KEY\n" not in text


def test_fly_config_matches_port_and_health_check() -> None:
    fly = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))
    assert fly["build"]["dockerfile"] == "Dockerfile"
    assert int(fly["env"]["PORT"]) == fly["http_service"]["internal_port"]
    assert set(fly["env"]) - {"PORT"} <= _known_env_vars()
    assert fly["http_service"]["checks"][0]["path"] == "/healthz"


def test_railway_config() -> None:
    railway = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    assert railway["build"]["builder"] == "DOCKERFILE"
    assert railway["deploy"]["healthcheckPath"] == "/healthz"


def test_dockerfile_honours_port() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "${PORT:-8000}" in text and "USER selfheal" in text and "HEALTHCHECK" in text


def test_vercel_bundles_package_data_and_routes_everything_to_the_app() -> None:
    vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    function = vercel["functions"]["api/index.py"]
    assert function["includeFiles"] == "src/selfheal/**"
    assert {"source": "/(.*)", "destination": "/api/index"} in vercel["rewrites"]
    assert (SRC / "api" / "static" / "index.html").is_file()
    assert (SRC / "scenarios.py").is_file()


def test_serverless_entrypoint_is_a_sync_public_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SELFHEAL_API_KEYS",
        "SELFHEAL_APPROVER_KEYS",
        "SELFHEAL_DEV_MODE",
        "SELFHEAL_SYNC_RUNS",
        "SELFHEAL_MAX_CONCURRENT_RUNS",
        "SELFHEAL_MAX_TICKS_CAP",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("VERCEL", "1")
    spec = importlib.util.spec_from_file_location("vercel_entry", ROOT / "api" / "index.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.app) as client:
        cfg = client.get("/api/config").json()
        assert cfg["access_mode"] == "public-demo"
        assert cfg["sync_runs"] is True
        assert set(cfg["scenarios"]) == set(SCENARIOS)
        assert "SelfHeal" in client.get("/").text

        resp = client.post("/api/runs", json={"scenario": "bad-deploy"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "finished"
        assert body["passed"] is True
