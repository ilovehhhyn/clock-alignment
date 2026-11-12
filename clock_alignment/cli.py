"""Offline by default. Paid generation, local models and analysis each require explicit opt-in."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from .analysis import judge, probes, report
from .config import Config, LOCAL_MODELS
from .local_provider import API_KEY_ENV
from .runner import run

ARMS = ("dynamic-voting", "solo", "advisory", "copied-ballot")
LOCAL_MODEL = "Qwen/Qwen3-4B-Instruct-2507"
DEMO_DAYS = 6


def preflight():
    config = Config()
    # No network calls, key reads, package installs, or GPU allocations.
    with tempfile.TemporaryDirectory(prefix="clock-alignment-preflight-") as root:
        statuses = {}
        for arm in ARMS:
            result = run(Path(root) / arm, seed=7, config=Config(arm=arm, max_days=2))
            if result["status"] not in {"completed", "censored"}:
                raise RuntimeError(f"Offline {arm} fixture failed: {result['error']}")
            statuses[arm] = result["status"]
        return {"offline_preflight": "passed", "fixture_arms": statuses,
                "budget_bound": config.bound(), "paid_api_calls": 0,
                "not_verified": ["Account/model access", "Live API compatibility and latency",
                                 "Model competence or game-completion rate"]}


def positive_days(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description="Budgeted Clocktower-inspired swarm research")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="Run four offline fixtures, no API calls")
    helps = {"demo": "Scripted infrastructure fixture", "run": "Paid OpenAI game",
             "local": "Game against a loopback vLLM server (no API spend)"}
    for name, text in helps.items():
        p = sub.add_parser(name, help=text)
        p.add_argument("--out", required=True)
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--arm", choices=ARMS, default="dynamic-voting")
        p.add_argument("--resume", action="store_true", help="Reuse saved state, frozen config and receipts")
        p.add_argument("--max-days", type=positive_days, required=name == "local",
                       default=DEMO_DAYS if name == "demo" else None,
                       help="Announced final day; omit for a paid game to play until a team wins")
        if name == "run":
            p.add_argument("--allow-paid", action="store_true")
        if name == "local":
            p.add_argument("--endpoint", required=True, help="http://127.0.0.1:<port> of the vLLM server")
            p.add_argument("--model", choices=LOCAL_MODELS, default=LOCAL_MODEL,
                           help="Served model; it must match the vLLM server's --served-model-name")
            p.add_argument("--max-input-tokens", type=positive_days, default=Config.max_input_tokens,
                           help="Input cap per call; the server's --max-model-len must cover it plus 4096")
            p.add_argument("--family-tokens", type=positive_days, default=Config.family_tokens,
                           help="Generated-token allowance of an original and all its children for the whole game")
    p = sub.add_parser("report", help="Offline observer-only exports and diagnostics")
    p.add_argument("--out", required=True)
    for name in ("probes", "judge"):
        p = sub.add_parser(name, help="Paid read-only analysis, charged to the original run")
        p.add_argument("--out", required=True)
        p.add_argument("--allow-paid", action="store_true")
        if name == "judge":
            p.add_argument("--request-id", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"run", "probes", "judge"}:
        if not args.allow_paid:
            parser.error("Paid calls are disabled; add --allow-paid only when ready to spend from this run's cap")
        if not os.environ.get("OPENAI_API_KEY"):
            parser.error("OPENAI_API_KEY is missing; set it in your shell without committing it")
    if args.command == "local" and not os.environ.get(API_KEY_ENV):
        parser.error(f"{API_KEY_ENV} is missing; export the vLLM server's key in the job environment")
    if args.command == "preflight":
        result = preflight()
    elif args.command == "demo":
        result = run(args.out, args.seed, Config(arm=args.arm, max_days=args.max_days), resume=args.resume)
    elif args.command == "run":
        result = run(args.out, args.seed, Config(arm=args.arm, max_days=args.max_days),
                     provider="openai", resume=args.resume)
    elif args.command == "local":
        config = Config(model=args.model, effort="none", arm=args.arm, max_days=args.max_days,
                        max_input_tokens=args.max_input_tokens, family_tokens=args.family_tokens)
        result = run(args.out, args.seed, config, provider="vllm", resume=args.resume, endpoint=args.endpoint)
    elif args.command == "report":
        result = report(args.out)
    elif args.command == "probes":
        result = probes(args.out)
    else:
        result = judge(args.out, args.request_id)
    print(json.dumps(result, indent=2))
    if result.get("status") in {"invalid", "incomplete"}:
        raise SystemExit(1)
