#!/usr/bin/env python3
"""End-to-end smoke test against a running server (stdlib only).

Usage: python3 scripts/smoke_test.py [BASE_URL] [--api-key KEY]

For every packaged scenario: start a run, wait until finished (works with background and sync
mode), check the expected outcome, and for stuck-database exercise the approval flow. Exits
non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any

EXPECTED = {
    "bad-deploy": ("resolved", "rollback_deploy"),
    "memory-leak": ("resolved", "restart_service"),
    "traffic-spike": ("resolved", "scale_out"),
    "dependency-outage": ("resolved", "open_circuit_breaker"),
    "stuck-database": ("awaiting_approval", "restart_service"),
    "prompt-injection-deploy": ("resolved", "rollback_deploy"),
}


class Client:
    def __init__(self, base: str, api_key: str | None) -> None:
        self.base = base.rstrip("/")
        self.api_key = api_key

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                raw = res.read().decode()
                ctype = res.headers.get("content-type", "")
                payload = json.loads(raw) if "json" in ctype else raw
                return res.status, payload, res.headers
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"FAIL {method} {path}: HTTP {exc.code} {exc.read()[:300]!r}") from exc


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL {message}")


def wait_done(client: Client, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while True:
        _, ev, _ = client.request("GET", f"/api/runs/{run_id}/events?since=0")
        if ev["done"]:
            _, detail, _ = client.request("GET", f"/api/runs/{run_id}")
            return detail
        check(time.monotonic() < deadline, f"{run_id}: run timed out")
        time.sleep(0.4)


def run_scenario(client: Client, name: str) -> None:
    expected_status, expected_action = EXPECTED[name]
    status, run, _ = client.request("POST", "/api/runs", {"scenario": name})
    check(status in (200, 202), f"{name}: create returned {status}")
    detail = (
        run if status == 200 and run.get("state") == "finished" else wait_done(client, run["id"])
    )
    check(detail["state"] == "finished", f"{name}: state {detail['state']} ({detail.get('error')})")
    if expected_status == "awaiting_approval":
        check(detail["pending_approvals"] >= 1, f"{name}: expected pending approval")
        pending = detail["pending"]
        check(pending[0]["action"] == expected_action, f"{name}: action {pending[0]['action']}")
        _, decided, _ = client.request(
            "POST",
            f"/api/runs/{detail['id']}/approvals/{pending[0]['incident_id']}/approve",
            {"approver": "smoke-test"},
        )
        check(decided["status"] == "resolved", f"{name}: not resolved after approval")
        print(f"PASS {name}: awaiting_approval -> approve -> resolved")
        return
    check(detail.get("passed") is True, f"{name}: expected PASS, got passed={detail.get('passed')}")
    root = next(
        (i for i in detail["incidents"] if i["service"] and expected_action),
        None,
    )
    # confirm the expected action fixed the root service
    fixed = False
    for inc in detail["incidents"]:
        for act in inc.get("actions") or []:
            if act.get("action") == expected_action and act.get("result") == "fixed":
                fixed = True
    check(fixed, f"{name}: expected fixed action {expected_action}")
    print(
        f"PASS {name}: {expected_action} -> resolved (root={None if root is None else root['service']})"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()
    client = Client(args.base, args.api_key)

    deadline = time.monotonic() + 60
    while True:
        try:
            _, health, _ = client.request("GET", "/healthz")
            break
        except (urllib.error.URLError, ConnectionError, SystemExit):
            check(time.monotonic() < deadline, "server did not become healthy")
            time.sleep(1)
    check(health["status"] == "ok", "health status")
    _, page, headers = client.request("GET", "/")
    check("SelfHeal" in page, "dashboard page")
    check("default-src 'self'" in headers.get("content-security-policy", ""), "CSP header")
    _, config, _ = client.request("GET", "/api/config")
    print(f"PASS health + dashboard: v{health['version']}, mode {config['access_mode']}")
    missing = sorted(set(EXPECTED) - set(config["scenarios"]))
    check(not missing, f"scenarios not served: {missing}")
    for name in sorted(EXPECTED):
        run_scenario(client, name)
    print(f"ALL PASS ({len(EXPECTED)} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
