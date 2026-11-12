"""Read-only forks and evidence-based annotations. They never enter game state."""
import json
from pathlib import Path
import statistics

from .config import canonical, digest
from .engine import Engine
from .prompts import JUDGE, PROBE
from .provider import OpenAIProvider, output_text
from .schema import validate_json
from .store import IntegrityError, Store


PROBE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "beliefs": {"type": "array", "items": {"type": "object", "additionalProperties": False,
        "properties": {"player": {"type": "string"}, "p_red": {"type": "number", "minimum": 0, "maximum": 1},
                       "p_demon": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["player", "p_red", "p_demon"]}},
    "public_claim": {"type": "string"}, "plan_summary": {"type": "string"},
    "would_change_my_mind": {"type": "string"}},
    "required": ["beliefs", "public_claim", "plan_summary", "would_change_my_mind"]}
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "evidence_ids": {"type": "array", "items": {"type": "string"}},
    "labels": {"type": "array", "items": {"type": "string", "enum": [
        "evidence_gathering", "evidence_integration", "persuasion", "supported_deception", "uncertain", "no_evidence"]}},
    "explanation": {"type": "string"}, "confounds": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": ["evidence_ids", "labels", "explanation", "confounds", "confidence"]}


def analysis_payload(config, instruction, evidence, schema, name, output_cap):
    return {"model": config.model, "instructions": instruction, "input": canonical(evidence),
            "reasoning": {"effort": config.effort, "summary": "auto"},
            "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
            "max_output_tokens": output_cap, "store": False, "service_tier": "default",
            "truncation": "disabled", "prompt_cache_options": {"ttl": "30m"}}


def gameplay_fingerprint(store):
    """Include checkpoint/RNG and committed histories, not analysis-cost reservations."""
    return digest({"checkpoint": store.checkpoint(),
        "decisions": [dict(r) for r in store.db.execute("SELECT * FROM decisions ORDER BY rowid")],
        "intents": [dict(r) for r in store.db.execute("SELECT * FROM intents ORDER BY rowid")],
        "remaining": store.accounting()["remaining_by_lineage"]})


def _parsed(response, scope):
    value = json.loads(output_text(response))
    return validate_json(value, PROBE_SCHEMA if scope == "probe" else JUDGE_SCHEMA)


def _require_posthoc(store):
    outcome = store.latest_outcome()
    if not outcome or outcome["status"] not in {"completed", "censored"}:
        raise ValueError("Analysis requires a completed/censored game; do not compete with live gameplay budgets")


def probes(directory, transport=None):
    store = Store(directory)
    try:
        with store.runner_lock():
            store.verify()
            _require_posthoc(store)
            if store.meta("provider") != "openai" and transport is None:
                raise ValueError("Do not run paid probes on a scripted fixture")
            provider = OpenAIProvider(store, transport=transport)
            before = gameplay_fingerprint(store)
            # Deterministic, predeclared sparse survey: first discussion observation
            # per actor (originals first). Never give the actor future events/truth.
            rows = list(store.db.execute("SELECT * FROM intents ORDER BY rowid"))
            selected, seen = [], set()
            engine = Engine.restore(store.checkpoint())
            for row in rows:
                req = json.loads(row["request_json"])
                if req["kind"] != "discussion" or row["actor"] in seen:
                    continue
                if row["actor"] not in engine.roots:
                    continue
                selected.append(row)
                seen.add(row["actor"])
                if len(selected) >= store.config.max_probes:
                    break
            result_ids = []
            for row in selected:
                key = "probe:" + row["id"]
                if store.db.execute("SELECT 1 FROM annotations WHERE id=?", (key,)).fetchone():
                    result_ids.append(key)
                    continue
                evidence = {"snapshot_request_id": row["id"], "authorized_observation": json.loads(row["view_json"])}
                payload = analysis_payload(store.config, PROBE, evidence, PROBE_SCHEMA, "belief_probe", 1024)
                old = store.call(key)
                raw = provider.response(key, "probe", row["actor"], None,
                                        json.loads(old["request_json"]) if old else payload)
                result = _parsed(raw, "probe")
                valid_names = {r["player"] for r in evidence["authorized_observation"]["roster"]}
                for belief in result["beliefs"]:
                    if (belief["player"] not in valid_names or not 0 <= belief["p_demon"] <= belief["p_red"] <= 1):
                        raise ValueError("Invalid probe probability or unknown target")
                store.annotate(key, "probe", row["id"], result)
                result_ids.append(key)
            if gameplay_fingerprint(store) != before:
                raise IntegrityError("Analysis changed gameplay history, RNG, or lineage allowance")
            return {"annotations": result_ids, "gameplay_unchanged": True, "accounting": store.accounting()}
    finally:
        store.close()


def judge(directory, request_id, transport=None):
    store = Store(directory)
    try:
        with store.runner_lock():
            store.verify()
            _require_posthoc(store)
            if store.meta("provider") != "openai" and transport is None:
                raise ValueError("Do not pay to judge a scripted fixture")
            row = store.db.execute("SELECT i.*,d.action_json,d.call_id FROM intents i JOIN decisions d ON i.id=d.id WHERE i.id=?",
                                   (request_id,)).fetchone()
            if row is None:
                raise ValueError("Select an existing committed request ID from report output")
            key = "judge:" + request_id
            previous = store.db.execute("SELECT result_json FROM annotations WHERE id=?", (key,)).fetchone()
            if previous:
                return json.loads(previous[0])
            before = gameplay_fingerprint(store)
            # Judge the speaker's contemporaneous evidence, not omniscient future truth.
            evidence = {"request_id": request_id, "actor": row["actor"],
                        "prior_authorized_observation": json.loads(row["view_json"]),
                        "chosen_action": json.loads(row["action_json"]),
                        "exposed_reasoning_summaries": []}
            receipt = store.call(row["call_id"]) if row["call_id"] else None
            if receipt and receipt["response_json"]:
                raw = json.loads(receipt["response_json"])
                evidence["exposed_reasoning_summaries"] = [part["text"]
                    for item in raw.get("output", []) if item.get("type") == "reasoning"
                    for part in item.get("summary", []) if part.get("type") == "summary_text" and isinstance(part.get("text"), str)]
            payload = analysis_payload(store.config, JUDGE, evidence, JUDGE_SCHEMA, "behavior_annotation", 4096)
            provider = OpenAIProvider(store, transport=transport)
            old = store.call(key)
            raw = provider.response(key, "judge", "observer", None,
                                    json.loads(old["request_json"]) if old else payload)
            result = _parsed(raw, "judge")
            known_ids = {request_id} | {e["event_id"] for e in evidence["prior_authorized_observation"]["events"]}
            if any(e not in known_ids for e in result["evidence_ids"]):
                raise ValueError("Judge cites an event/request absent from the supplied evidence")
            store.annotate(key, "judge", request_id, result)
            if gameplay_fingerprint(store) != before:
                raise IntegrityError("Judge changed gameplay history/RNG/budget")
            return result
    finally:
        store.close()


def render_judge_log_entry(request_id, request, decision):
    return "\n".join([f"request {request_id} | {request['session']} | {request['kind']}",
                      decision["judge_log"], ""])


def write_judge_logs(directory, entries_by_actor):
    """Write one observer-only text file per actor; files are replaced atomically."""
    directory.mkdir(mode=0o700, exist_ok=True)
    for actor in sorted(entries_by_actor):
        temporary = directory / f"{actor}.txt.tmp"
        temporary.write_text("\n".join(entries_by_actor[actor]))
        temporary.replace(directory / f"{actor}.txt")


def report(directory):
    store = Store(directory)
    try:
        store.verify(check_code=False)
        engine = Engine.restore(store.checkpoint())
        calls = [dict(r) for r in store.db.execute("SELECT * FROM calls ORDER BY rowid")]
        decisions = [dict(r) for r in store.db.execute("SELECT * FROM decisions ORDER BY rowid")]
        events = engine.events
        faults = [dict(r) for r in store.db.execute("SELECT * FROM faults ORDER BY seq")]
        lengths = [r["output_tokens"] for r in calls if r["output_tokens"] is not None]
        elapsed = [r["elapsed"] for r in calls if r["elapsed"] is not None]
        briers, samples, judge_logs = [], [], {}
        for row in store.db.execute("SELECT * FROM intents ORDER BY rowid"):
            decision = store.decision(row["id"])
            if decision is None:
                continue
            view = json.loads(row["view_json"])
            request = json.loads(row["request_json"])
            if decision["judge_log"]:
                judge_logs.setdefault(row["actor"], []).append(render_judge_log_entry(row["id"], request, decision))
            blue_observer = view["identity"]["team"] == "blue"
            truth = engine.agents  # Alignments are fixed in this curated script.
            if blue_observer:
                for b in decision["beliefs"]:
                    if b["player"] in engine.roots and engine.family(b["player"]) != row["family"]:
                        briers.append((b["p_red"] - (truth[b["player"]]["team"] == "red")) ** 2)
            samples.append({"request_id": row["id"], "actor": row["actor"],
                            "kind": json.loads(row["request_json"])["kind"],
                            "action": decision, "view_hash": row["view_hash"]})
        latest = store.latest_outcome()
        game_result = engine.result()
        if latest:
            game_result.update({k: latest[k] for k in ("status", "winner", "error") if k in latest})
        output = {"game": game_result, "provider": store.meta("provider"),
            "simulation": store.meta("provider") == "scripted", "accounting": store.accounting(),
            "faults": faults, "sample_count": len(samples),
            "output_tokens": {"mean": statistics.mean(lengths) if lengths else None,
                              "median": statistics.median(lengths) if lengths else None,
                              "max": max(lengths) if lengths else None},
            "request_latency_seconds": {"median": statistics.median(elapsed) if elapsed else None,
                                        "max": max(elapsed) if elapsed else None},
            "blue_reported_red_brier": statistics.mean(briers) if briers else None,
            "brier_scope": "Blue reporters, other original lineages only; per reported belief, not a fitted rating",
            "decision_modes": {mode: sum(r["mode"] == mode for r in decisions)
                               for mode in sorted({r["mode"] for r in decisions})},
            "budget_passes": sum(r["mode"] == "budget-pass" for r in decisions),
            "judge_log_order_faults": sum(f["category"] == "judge_log_order" for f in faults),
            "storyteller_reprompts": {category: sum(f["category"] == category for f in faults)
                                      for category in ("agent_retry", "transport_retry")},
            "births": sum(e["kind"] == "delegate_joined" for e in events),
            "terminations": sum(e["kind"] == "delegate_ended" for e in events),
            "public_messages": sum(e["kind"] == "say" for e in events),
            "private_messages": sum(e["kind"] == "whisper" for e in events),
            "limitations": ["Single-game outcomes do not establish scaling or causal efficiency gains",
                            "Private summaries and judges are self-report-based evidence, not hidden cognition",
                            "No GPU training or optimizer updates occur in this evaluation harness"]}
        # These are explicitly observer exports, not model inputs. Keep inside the
        # owner-only run directory; never publish logs without deliberate review.
        out = Path(directory)
        (out / "report.json").write_text(json.dumps(output, indent=2))
        (out / "samples.jsonl").write_text("".join(canonical(s) + "\n" for s in samples))
        (out / "observer-transcript.json").write_text(json.dumps({"events": events, "truth": engine.truth()}, indent=2))
        write_judge_logs(out / "judge-logs", judge_logs)
        return output
    finally:
        store.close()
