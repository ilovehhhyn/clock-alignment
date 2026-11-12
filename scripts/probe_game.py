"""Probe a running Della game from a frozen copy: liveness, progress, faults, harness checks, and spot checks.

The live run is never touched. The ledger is copied on Della with SQLite's backup API (a consistent
snapshot), synced to a local directory, and reported on there with the repository's own `report`.
The harness section separates what the harness got wrong (a failure: exit code 3) from what the model
did (reported, never a failure); a self-reveal the rule let through counts as the model's (Helen, 2026-11-10).
When the login node cannot be reached (exit code 4) nothing is reported, because a report from the previous
copy would read as fresh; the game itself runs on without probes. Usage: python3 scripts/probe_game.py RUN_NAME LOCAL_DIR [--ssh HOST] [--sample N]
"""
import argparse
import json
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from clock_alignment.secrecy import self_reveal  # noqa: E402
from clock_alignment.store import UNDELIVERED  # noqa: E402

HARNESS_FAILURE_EXIT = 3
UNREACHABLE_EXIT = 4
SSH_FAILURE_PREFIX = "[ssh exit"
# Faults the runner records for its own account; anything else is the harness failing.
EXPECTED_FAULTS = {"agent_retry", "transport_retry"}
DEAD_JOB_STATES = ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "CANCELLED", "PREEMPTED")
STALL_SECONDS = 15 * 60
CAP_MARGIN_TOKENS = 12_000
ROOT = "/scratch/gpfs/ARORA/hh9077"
PYTHON = f"{ROOT}/envs/vllm/bin/python"
BACKUP = ("import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); "
          "src.backup(dst); dst.close(); src.close()")
SSH_OPTIONS = ("-o", "ConnectTimeout=25", "-o", "BatchMode=yes")
# Times and compression only: the local copy keeps the scratch directory's own permissions.
RSYNC = ("rsync", "-rtz")


def remote(host, command, timeout_s=120):
    """Run one short command on the login node and return its output; a failure is reported, not hidden."""
    result = subprocess.run(["ssh", *SSH_OPTIONS, host, command], capture_output=True, text=True, timeout=timeout_s)
    if result.returncode != 0:
        return f"{SSH_FAILURE_PREFIX} {result.returncode}] {result.stderr.strip()[-400:]}"
    return result.stdout


def fetch(host, run_name, local):
    """Freeze the ledger on Della and sync it with the link logs into `local`; return the queue text."""
    run_dir = f"{ROOT}/clock-runs/{run_name}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot = f"{ROOT}/clock-runs/probes/{run_name}-{stamp}.sqlite"
    queue = remote(host, " && ".join([
        f"mkdir -p {ROOT}/clock-runs/probes",
        f"test -f {run_dir}/experiment.sqlite && OMP_NUM_THREADS=2 {PYTHON} -c '{BACKUP}' {run_dir}/experiment.sqlite "
        f"{snapshot} || echo 'no ledger yet'",
        "squeue -u $USER -o '%.10i %.10j %.3t %.8M %.8l %R' | grep -E 'JOBID|ca-pilot'",
        "sacct -u $USER -S $(date -d '-2 days' +%F) -n -o JobID,JobName%10,State%12,Elapsed,ExitCode | "
        "grep ca-pilot | grep -v '\\.'",
        f"ls -t {run_dir}/links 2>/dev/null | head -3",
    ]))
    if unreachable(queue):
        return queue
    local.mkdir(parents=True, exist_ok=True)
    if "no ledger yet" not in queue:
        subprocess.run([*RSYNC, f"{host}:{snapshot}", str(local / "experiment.sqlite")], check=False)
    subprocess.run([*RSYNC, "--include=*/", "--include=*.json", "--include=*.txt", "--include=*.out", "--exclude=*",
                    f"{host}:{run_dir}/links/", str(local / "links")], check=False)
    subprocess.run([*RSYNC, "--include=ca-pilot-*.out", "--exclude=*", f"{host}:{ROOT}/logs/", str(local / "logs")],
                   check=False)
    subprocess.run([*RSYNC, f"{host}:{run_dir}/chain.txt", str(local / "chain.txt")], check=False)
    return queue


def unreachable(queue_text):
    """True when the login node refused or dropped the probe's own command; every later sync would hang or fail too."""
    return queue_text.startswith(SSH_FAILURE_PREFIX)


def report_locally(local):
    """Run the repository's report on the frozen copy; return its summary or None without a ledger."""
    if not (local / "experiment.sqlite").exists():
        return None
    result = subprocess.run([sys.executable, "-m", "clock_alignment", "report", "--out", str(local)],
                            capture_output=True, text=True, cwd=REPO)
    if result.returncode != 0:
        print("report failed:", result.stderr[-800:])
        return None
    return json.loads(result.stdout)


def events_and_truth(local):
    data = json.loads((local / "observer-transcript.json").read_text())
    return data["events"], data["truth"]["agents"]


def delivered_confessions(events, agents):
    """Return red messages that reached an outsider or the public and still state the sender's team or character."""
    found = []
    for event in events:
        if event["kind"] in ("whisper", "day_whisper"):
            sender, text, to = event["data"]["from"], event["data"]["text"], event["data"]["to"]
            if agents[sender]["team"] != "red" or agents[to]["team"] == "red":
                continue
        elif event["kind"] in ("say", "defense"):
            sender, text, to = event["data"]["speaker"], event["data"]["text"], "public"
            if agents[sender]["team"] != "red":
                continue
        elif event["kind"] == "nomination":
            sender, text, to = event["data"]["nominator"], event["data"]["text"], "public"
            if agents[sender]["team"] != "red":
                continue
        else:
            continue
        phrase = self_reveal(text)
        if phrase is not None:
            found.append((event["day"], sender, to, phrase))
    return found


def ledger_checks(local):
    """Read the frozen ledger for input growth, allowances, re-prompts and cut messages."""
    db = sqlite3.connect(local / "experiment.sqlite")
    db.row_factory = sqlite3.Row
    calls = [dict(r) for r in db.execute("SELECT * FROM calls WHERE scope='game' ORDER BY created")]
    completed = [c for c in calls if c["status"] == "completed"]
    inputs = [c["input_tokens"] for c in completed[-60:]]
    marks = sum(1 for c in completed if c["response_json"] and "[cut off]" in c["response_json"])
    faults = [dict(r) for r in db.execute("SELECT * FROM faults ORDER BY seq")]
    families = {r[0]: r[1] for r in db.execute("SELECT id, token_cap FROM families")}
    remaining = {}
    for family, cap in families.items():
        used = db.execute("SELECT COALESCE(SUM(CASE WHEN status='completed' THEN output_tokens - COALESCE(exempt_tokens,0) "
                          "WHEN status='released' THEN 0 ELSE output_cap END),0) FROM calls WHERE scope='game' AND family=?",
                          (family,)).fetchone()[0]
        remaining[family] = cap - used
    undelivered = sum(1 for c in completed if c["error"] in UNDELIVERED)
    statuses = {}
    for c in calls:
        statuses[c["status"]] = statuses.get(c["status"], 0) + 1
    config = json.loads(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
    db.close()
    return {"calls_by_status": statuses, "recent_input_tokens": {"max": max(inputs, default=None),
            "median": sorted(inputs)[len(inputs) // 2] if inputs else None, "cap": config["max_input_tokens"]},
            "cut_messages": marks, "undelivered_answers": undelivered,
            "family_remaining": {"min": min(remaining.values()), "median": sorted(remaining.values())[6],
                                 "max": max(remaining.values())},
            "faults": faults}


def slurm_seconds(text):
    """Parse Slurm's elapsed time ([D-]HH:MM:SS or MM:SS; squeue's 0:25 is 25 seconds) into seconds."""
    days, _, clock = text.rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts) or (days and not days.isdigit()):
        raise ValueError(f"not a Slurm elapsed time: {text!r}")
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds + (int(days) if days else 0) * 24 * 3600


def chain_jobs(queue_text, local):
    """Return (job id, state, elapsed seconds) for this game's links, the ids listed in the run's chain.txt."""
    chain_file = local / "chain.txt"
    ids = set(chain_file.read_text().split()) if chain_file.exists() else set()
    jobs = []
    for line in queue_text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "ca-pilot" and parts[0] in ids:
            jobs.append((parts[0], parts[2], slurm_seconds(parts[3])))
    return jobs


def stalled(running_seconds, idle_seconds, requests_unchanged):
    """A link that has run past its load phase, with no decision since the previous probe, is stalled.

    A link that only just started is loading the model while the ledger still shows the previous link's
    last decision, which a 20-minute probe cadence sees as a long idle gap; that is not a stall.
    """
    return requests_unchanged and running_seconds > STALL_SECONDS and idle_seconds > STALL_SECONDS


def harness_report(local, summary, checks, confessions, queue_text):
    """Return (failures, warnings): failures are the harness's own faults, warnings are things to read."""
    failures, warnings = [], []
    game = summary["game"]
    if game["status"] in ("invalid", "incomplete") or game.get("error"):
        failures.append(f"run status {game['status']}: {game.get('error')}")
    unexpected = [f for f in checks["faults"] if f["category"] not in EXPECTED_FAULTS]
    if unexpected:
        failures.append("unexpected faults: " + "; ".join(f"{f['category']}: {f['detail'][:160]}" for f in unexpected[-3:]))
    db = sqlite3.connect(local / "experiment.sqlite")
    db.row_factory = sqlite3.Row
    mismatches = 0
    for row in db.execute("SELECT i.request_json, d.action_json FROM intents i JOIN decisions d ON i.id=d.id"):
        request, action = json.loads(row["request_json"]), json.loads(row["action_json"])
        if action["whisper_to"] is not None:
            expected = "team" if action["whisper_to"] in request["legal"]["team_links"] else "unknown"
            mismatches += action["audience"] != expected
        elif action["say"] and action["audience"] != "public":
            mismatches += 1
    if mismatches:
        failures.append(f"{mismatches} committed action(s) with an audience that does not match the channel")
    last_call = db.execute("SELECT MAX(created) FROM calls").fetchone()[0] or time.time()
    db.close()
    links = chain_jobs(queue_text, local)
    for job, state, _ in links:
        if state.startswith(DEAD_JOB_STATES):
            failures.append(f"link job {job} is {state}")
    for log in sorted((local / "logs").glob("ca-pilot-*.out")) + sorted((local / "links").glob("*/vllm.log")):
        text = log.read_text(errors="replace")
        if "Traceback" in text or "IntegrityError" in text or re.search(r"runner exit code [1-9]", text):
            failures.append(f"{log.name} reports an error")
    running_seconds = max((elapsed for _, state, elapsed in links if state == "R"), default=None)
    marker = local / "last_probe.json"
    previous = json.loads(marker.read_text()) if marker.exists() else None
    idle_seconds = time.time() - last_call
    if running_seconds is not None and previous and stalled(running_seconds, idle_seconds, previous["requests"] == game["requests"]):
        failures.append(f"no decision for {int(idle_seconds)} s while a link has run for {running_seconds} s")
    marker.write_text(json.dumps({"requests": game["requests"], "at": time.time()}))
    modes = summary["decision_modes"]
    if confessions:
        warnings.append(f"{len(confessions)} red self-reveal(s) the rule let through to blue or public (read them): {confessions[:2]}")
    if modes.get("unconfirmed", 0):
        warnings.append(f"{modes['unconfirmed']} action(s) committed without a usable reply to the Storyteller check (read them)")
    if modes.get("forfeit", 0):
        warnings.append(f"{modes['forfeit']} full forfeit(s): a player failed three times in a row (read them)")
    if summary["budget_passes"]:
        warnings.append(f"{summary['budget_passes']} budget pass(es)")
    if summary["storyteller_reprompts"]["transport_retry"]:
        warnings.append(f"{summary['storyteller_reprompts']['transport_retry']} transport retries")
    cap, biggest = checks["recent_input_tokens"]["cap"], checks["recent_input_tokens"]["max"] or 0
    if cap - biggest < CAP_MARGIN_TOKENS:
        warnings.append(f"prompt within {cap - biggest} tokens of the {cap} cap")
    return failures, warnings


def spot_sample(local, agents, count, seed):
    """Return `count` recent decisions with their note and judge log, half of them by evil players when possible."""
    rows = [json.loads(line) for line in (local / "samples.jsonl").read_text().splitlines()]
    recent = rows[-120:]
    evil = [r for r in recent if agents[r["actor"]]["team"] == "red" and (r["action"]["private_note"] or r["action"]["judge_log"])]
    good = [r for r in recent if agents[r["actor"]]["team"] == "blue" and (r["action"]["private_note"] or r["action"]["judge_log"])]
    rng = random.Random(seed)
    picked = rng.sample(evil, min(len(evil), count // 2)) + rng.sample(good, min(len(good), count - count // 2))
    return picked


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run_name")
    parser.add_argument("local_dir")
    parser.add_argument("--ssh", default="della-pli")
    parser.add_argument("--sample", type=int, default=4)
    args = parser.parse_args()
    local = Path(args.local_dir)
    print("== queue and links ==")
    queue_text = fetch(args.ssh, args.run_name, local)
    print(queue_text.rstrip())
    if unreachable(queue_text):
        print(f"DELLA UNREACHABLE: the login node dropped the probe's command; the game runs on without probes. "
              f"Sign in once (ssh {args.ssh}) to restore the shared connection, then probe again.")
        raise SystemExit(UNREACHABLE_EXIT)
    summary = report_locally(local)
    if summary is None:
        print("no ledger to report on yet")
        return
    game = summary["game"]
    print("\n== game ==")
    print(json.dumps({k: game.get(k) for k in ("status", "days", "requests", "winner", "originals_alive",
                                              "delegates_alive", "error")}))
    print(json.dumps({k: summary[k] for k in ("decision_modes", "storyteller_reprompts", "births", "terminations",
                                             "public_messages", "private_messages", "budget_passes")}))
    print("latency s:", json.dumps(summary["request_latency_seconds"]), "output tokens:", json.dumps(summary["output_tokens"]))
    checks = ledger_checks(local)
    print("\n== ledger ==")
    print(json.dumps({k: v for k, v in checks.items() if k != "faults"}))
    print("faults by category:", json.dumps({c: sum(f["category"] == c for f in checks["faults"])
                                            for c in sorted({f["category"] for f in checks["faults"]})}))
    for fault in checks["faults"][-6:]:
        print(f"  {fault['category']}: {fault['detail'][:300]}")
    events, agents = events_and_truth(local)
    confessions = delivered_confessions(events, agents)
    print("\n== delivered red self-reveals to blue or public (must be empty) ==")
    print(confessions if confessions else "none")
    failures, warnings = harness_report(local, summary, checks, confessions, queue_text)
    print("\n== harness ==")
    print("HARNESS FAILURE: " + " | ".join(failures) if failures else "HARNESS OK")
    for warning in warnings:
        print("warning:", warning)
    print("\n== spot checks (recent decisions; team, kind, action, note, judge log) ==")
    for row in spot_sample(local, agents, args.sample, seed=int(time.time()) // 3600):
        action = row["action"]
        agent = agents[row["actor"]]
        chosen = {k: v for k, v in action.items() if k not in ("private_note", "judge_log", "beliefs") and v not in ("", None, False, [])}
        print(f"- {row['actor']} [{agent['shown']}, {agent['team']}, {'child' if agent['delegate'] else 'original'}] "
              f"{row['kind']}: {json.dumps(chosen)[:400]}")
        print(f"    note: {action['private_note'][:500]}")
        print(f"    judge_log: {action['judge_log'][:500]}")
    if failures:
        raise SystemExit(HARNESS_FAILURE_EXIT)


if __name__ == "__main__":
    main()
