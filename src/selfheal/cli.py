"""Command line interface: `selfheal scenarios | run <scenario>`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__
from .engine import run_scenario
from .healer import HealerConfig
from .llm import provider_from_env
from .scenarios import SCENARIOS

ICONS = {
    "anomaly": "!",
    "diagnosis": "?",
    "plan": ">",
    "executed": "*",
    "verified": "+",
    "resolved": "+",
    "verify_failed": "x",
    "reverted": "<",
    "blocked": "#",
    "approval_required": "@",
    "approved": "@",
    "escalated": "^",
    "dry_run": "~",
    "warning": "-",
}


def _cmd_scenarios() -> int:
    for s in SCENARIOS.values():
        print(f"{s.id:<26} {s.title}")
        print(f"{'':<26} expected: {s.expected_action} on {s.expected_root_service}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        provider = provider_from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config = HealerConfig(dry_run=args.dry_run, approver=args.approver)
    try:
        _, report = run_scenario(args.scenario, provider=provider, config=config, ticks=args.ticks)
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        sc = report["scenario"]
        print(f"Scenario: {sc['title']}  (provider: {report['provider']})")
        print(sc["description"])
        print()
        for e in report["events"]:
            print(
                f"t+{e['tick']:>3}m [{ICONS.get(e['type'], ' ')}] {e['service']:<13} {e['message']}"
            )
        s = report["summary"]
        print()
        print(
            f"incidents: {s['total']}  resolved: {s['resolved']}  escalated: {s['escalated']}  "
            f"open: {s['open']}  mean MTTR: {s['mean_mttr_minutes']} min"
        )
        print("result:", "PASS" if report["passed"] else "UNEXPECTED")
    return 0 if report["passed"] or args.dry_run else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="selfheal", description="AI self-healing microservices control loop"
    )
    p.add_argument("--version", action="version", version=f"selfheal {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scenarios", help="list packaged chaos scenarios")
    run = sub.add_parser("run", help="run a scenario through the healing loop")
    run.add_argument("scenario", choices=sorted(SCENARIOS))
    run.add_argument(
        "--ticks", type=int, default=None, help="simulated minutes (default per scenario)"
    )
    run.add_argument("--dry-run", action="store_true", help="plan only, never execute")
    run.add_argument("--approver", default=None, help="auto-approve protected actions as NAME")
    run.add_argument("--json", action="store_true", help="print the full JSON report")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scenarios":
        return _cmd_scenarios()
    if args.ticks is not None and not 1 <= args.ticks <= 1000:
        print("error: --ticks must be between 1 and 1000", file=sys.stderr)
        return 2
    return _cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
