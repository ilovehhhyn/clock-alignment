"""Offline by default. Paid generation and analysis each require explicit opt-in."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from .analysis import judge, probes, report
from .config import Config
from .runner import run


def preflight():
    config = Config()
    # No network calls, key reads, package installs, or GPU allocations.
    with tempfile.TemporaryDirectory(prefix="clock-alignment-preflight-") as root:
        statuses = {}
        for arm in ("solo", "advisory", "copied-ballot", "dynamic-voting"):
            result = run(Path(root) / arm, seed=7, config=Config(arm=arm, max_days=2))
            if result["status"] not in {"completed", "censored"}:
                raise RuntimeError(f"Offline {arm} fixture failed: {result['error']}")
            statuses[arm] = result["status"]
        return {"offline_preflight": "passed", "fixture_arms": statuses,
                "budget_bound": config.bound(), "paid_api_calls": 0,
                "not_verified": ["Account/model access", "Live API compatibility and latency",
                                 "Model competence or game-completion rate"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Budgeted Clocktower-inspired swarm research")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="Run four offline fixtures, no API calls")
    for name in ("demo", "run"):
        p = sub.add_parser(name, help="Scripted infrastructure fixture" if name == "demo" else "Paid OpenAI game")
        p.add_argument("--out", required=True)
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--arm", choices=("dynamic-voting", "solo", "advisory", "copied-ballot"), default="dynamic-voting")
        p.add_argument("--resume", action="store_true", help="Reuse saved state, frozen config and receipts")
        if name == "run":
            p.add_argument("--allow-paid", action="store_true")
    p = sub.add_parser("report", help="Offline observer-only exports and diagnostics")
    p.add_argument("--out", required=True)
    for name in ("probes", "judge"):
        p = sub.add_parser(name, help="Paid read-only analysis, charged to the original run")
        p.add_argument("--out", required=True)
        p.add_argument("--allow-paid", action="store_true")
        if name == "judge":
            p.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    if args.command in {"run", "probes", "judge"}:
        if not args.allow_paid:
            parser.error("Paid calls are disabled; add --allow-paid only when ready to spend from this run's cap")
        if not os.environ.get("OPENAI_API_KEY"):
            parser.error("OPENAI_API_KEY is missing; set it in your shell without committing it")
    if args.command == "preflight":
        result = preflight()
    elif args.command in {"demo", "run"}:
        result = run(args.out, args.seed, Config(arm=args.arm), paid=args.command == "run", resume=args.resume)
    elif args.command == "report":
        result = report(args.out)
    elif args.command == "probes":
        result = probes(args.out)
    else:
        result = judge(args.out, args.request_id)
    print(json.dumps(result, indent=2))
    if result.get("status") in {"invalid", "incomplete"}:
        raise SystemExit(1)
