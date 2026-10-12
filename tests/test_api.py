"""HTTP API: auth, rate limits, sync/async runs, approvals, dashboard safety."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from selfheal.api import ApiSettings, create_app
from selfheal.api.security import ApiKeyAuth, Role, fingerprint
from selfheal.config import Settings
from selfheal.scenarios import SCENARIOS

STATIC = Path(__file__).resolve().parents[1] / "src" / "selfheal" / "api" / "static"


@pytest.fixture
def public_app() -> TestClient:
    app = create_app(
        ApiSettings(sync_runs=True, max_queued_runs=4, run_limit_per_minute=30),
        base_settings=Settings(provider="mock"),
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def keyed_app() -> TestClient:
    app = create_app(
        ApiSettings(
            operator_keys=("op-secret",),
            approver_keys=("apr-secret",),
            sync_runs=True,
            max_queued_runs=4,
            run_limit_per_minute=60,
            rate_limit_per_minute=200,
        ),
        base_settings=Settings(provider="mock"),
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def async_app() -> TestClient:
    app = create_app(
        ApiSettings(dev_mode=True, sync_runs=False, max_queued_runs=4),
        base_settings=Settings(provider="mock"),
    )
    with TestClient(app) as client:
        yield client


def test_health_and_config_public(public_app: TestClient) -> None:
    h = public_app.get("/healthz")
    assert h.status_code == 200
    assert h.json()["status"] == "ok"
    cfg = public_app.get("/api/config").json()
    assert cfg["access_mode"] == "public-demo"
    assert cfg["providers"] == ["mock"]
    assert cfg["simulated_environment"] is True
    assert "provider_api_key" not in cfg
    assert set(cfg["scenarios"]) == set(SCENARIOS)


def test_auth_roles(keyed_app: TestClient) -> None:
    assert keyed_app.get("/api/runs").status_code == 401
    assert keyed_app.get("/api/runs", headers={"X-API-Key": "wrong"}).status_code == 401
    op = keyed_app.get("/api/config", headers={"X-API-Key": "op-secret"}).json()
    assert op["caller_role"] == "operator"
    apr = keyed_app.get("/api/config", headers={"Authorization": "Bearer apr-secret"}).json()
    assert apr["caller_role"] == "approver"


def test_constant_time_key_auth() -> None:
    auth = ApiKeyAuth(("alpha",), ("beta",))
    assert auth.verify("alpha") is not None
    assert auth.verify("alpha").role is Role.OPERATOR  # type: ignore[union-attr]
    assert auth.verify("beta").role is Role.APPROVER  # type: ignore[union-attr]
    assert auth.verify("nope") is None
    assert fingerprint("alpha") == hashlib.sha256(b"alpha").hexdigest()[:16]


def test_sync_run_bad_deploy(public_app: TestClient) -> None:
    resp = public_app.post("/api/runs", json={"scenario": "bad-deploy"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "finished"
    assert body["passed"] is True
    assert body["simulated"] is True
    assert any(e["type"] == "resolved" for e in body["events"])
    detail = public_app.get(f"/api/runs/{body['id']}").json()
    assert detail["resolved"] >= 1


def test_stuck_database_approval_flow(public_app: TestClient) -> None:
    resp = public_app.post("/api/runs", json={"scenario": "stuck-database"})
    assert resp.status_code == 200
    run = resp.json()
    assert run["pending_approvals"] >= 1
    pending = run["pending"]
    assert pending[0]["action"] == "restart_service"
    iid = pending[0]["incident_id"]
    bad = public_app.post(
        f"/api/runs/{run['id']}/approvals/{iid}/approve",
        json={"approver": ""},
    )
    assert bad.status_code == 422
    ok = public_app.post(
        f"/api/runs/{run['id']}/approvals/{iid}/approve",
        json={"approver": "Priya", "reason": "safe to bounce"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "resolved"
    # second approve on same incident -> 409
    again = public_app.post(
        f"/api/runs/{run['id']}/approvals/{iid}/approve",
        json={"approver": "Priya"},
    )
    assert again.status_code == 409


def test_reject_escalates(public_app: TestClient) -> None:
    run = public_app.post("/api/runs", json={"scenario": "stuck-database"}).json()
    iid = run["pending"][0]["incident_id"]
    resp = public_app.post(
        f"/api/runs/{run['id']}/approvals/{iid}/reject",
        json={"approver": "Ops", "reason": "maintenance window"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "escalated"


def test_operator_cannot_approve(keyed_app: TestClient) -> None:
    headers = {"X-API-Key": "op-secret"}
    run = keyed_app.post("/api/runs", json={"scenario": "stuck-database"}, headers=headers).json()
    iid = run["pending"][0]["incident_id"]
    denied = keyed_app.post(
        f"/api/runs/{run['id']}/approvals/{iid}/approve",
        json={"approver": "Ops"},
        headers=headers,
    )
    assert denied.status_code == 403
    ok = keyed_app.post(
        f"/api/runs/{run['id']}/approvals/{iid}/approve",
        json={"approver": "Lead"},
        headers={"X-API-Key": "apr-secret"},
    )
    assert ok.status_code == 200


def test_ownership_scoped_by_key(keyed_app: TestClient) -> None:
    a = keyed_app.post(
        "/api/runs", json={"scenario": "memory-leak"}, headers={"X-API-Key": "op-secret"}
    ).json()
    # approver sees every run; operator sees own
    listed = keyed_app.get("/api/runs", headers={"X-API-Key": "apr-secret"}).json()["runs"]
    assert any(r["id"] == a["id"] for r in listed)
    own = keyed_app.get("/api/runs", headers={"X-API-Key": "op-secret"}).json()["runs"]
    assert any(r["id"] == a["id"] for r in own)


def test_body_cap_and_extra_forbid(public_app: TestClient) -> None:
    assert (
        public_app.post("/api/runs", json={"scenario": "bad-deploy", "extra": 1}).status_code == 422
    )
    huge = "x" * (70 * 1024)
    resp = public_app.post(
        "/api/runs",
        content=huge.encode(),
        headers={"Content-Type": "application/json", "Content-Length": str(len(huge))},
    )
    assert resp.status_code == 413


def test_security_headers_and_dashboard(public_app: TestClient) -> None:
    page = public_app.get("/")
    assert page.status_code == 200
    assert "SelfHeal" in page.text
    assert "default-src 'self'" in page.headers.get("content-security-policy", "")
    assert page.headers.get("x-frame-options") == "DENY"
    api = public_app.get("/api/config")
    assert api.headers.get("cache-control") == "no-store"
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js
    assert "textContent" in js


def test_events_polling_cursor(async_app: TestClient) -> None:
    resp = async_app.post("/api/runs", json={"scenario": "bad-deploy"})
    assert resp.status_code == 202
    rid = resp.json()["id"]
    # wait until finished
    for _ in range(100):
        ev = async_app.get(f"/api/runs/{rid}/events?since=0").json()
        if ev["done"]:
            break
    assert ev["done"] is True
    assert ev["next_cursor"] == len(ev["events"])
    empty = async_app.get(f"/api/runs/{rid}/events?since={ev['next_cursor']}").json()
    assert empty["events"] == []
    assert empty["next_cursor"] == ev["next_cursor"]


def test_unknown_scenario_and_ticks_cap(public_app: TestClient) -> None:
    assert public_app.post("/api/runs", json={"scenario": "nope"}).status_code == 422
    assert (
        public_app.post("/api/runs", json={"scenario": "bad-deploy", "ticks": 9999}).status_code
        == 422
    )


def test_provider_key_never_returned(
    keyed_app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SELFHEAL_API_KEY", "sk-super-secret")
    cfg = keyed_app.get("/api/config", headers={"X-API-Key": "op-secret"}).json()
    blob = str(cfg)
    assert "sk-super-secret" not in blob
    assert "op-secret" not in blob


def test_run_creation_rate_limit() -> None:
    app = create_app(
        ApiSettings(sync_runs=True, run_limit_per_minute=2, rate_limit_per_minute=100),
        base_settings=Settings(provider="mock"),
    )
    with TestClient(app) as client:
        assert client.post("/api/runs", json={"scenario": "bad-deploy"}).status_code == 200
        assert client.post("/api/runs", json={"scenario": "memory-leak"}).status_code == 200
        limited = client.post("/api/runs", json={"scenario": "traffic-spike"})
        assert limited.status_code == 429
        assert "Retry-After" in limited.headers


def test_all_scenarios_via_api(public_app: TestClient) -> None:
    for sid, sc in SCENARIOS.items():
        run = public_app.post("/api/runs", json={"scenario": sid}).json()
        assert run["state"] == "finished", sid
        if sc.expected_status == "awaiting_approval":
            assert run["pending_approvals"] >= 1, sid
            iid = run["pending"][0]["incident_id"]
            decided = public_app.post(
                f"/api/runs/{run['id']}/approvals/{iid}/approve",
                json={"approver": "smoke"},
            ).json()
            assert decided["status"] == "resolved", sid
        else:
            assert run["passed"] is True, sid
