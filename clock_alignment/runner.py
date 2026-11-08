"""Advance exactly one committed decision at a time; round barriers live in Engine."""
import json
import random
import time

from .config import Config, digest
from .engine import Engine
from .provider import OpenAIProvider, ProviderFailure, action_payload, parse_action
from .schema import empty_action
from .store import BudgetLimit, IntegrityError, PendingCall, Store


class ScriptedProvider:
    """A deterministic plumbing fixture, NEVER a model or scientific baseline."""
    name = "scripted"

    def __init__(self, store):
        self.store, self.config = store, store.config

    def act(self, request, view, remaining, family):
        call_id = "game:" + request["id"]
        old = self.store.call(call_id)
        if old:
            if old["status"] != "completed":
                raise PendingCall("Scripted call pending")
            return parse_action(json.loads(old["response_json"]))
        payload = action_payload(self.config, request, view, remaining,
                                 self.store.daily_balance(family, view["day"]))
        self.store.reserve(call_id, "game", request["actor"], family, payload["max_output_tokens"], payload)
        rng = random.Random(digest({"request": request, "actor": view["self"]}))
        legal = request["legal"]
        action = empty_action()
        action["private_note"] = "SCRIPTED FIXTURE: exercise legal actions and persistence; no model inference."
        if legal["spawn"] and view["day"] <= 2:
            action["spawn"] = True
        if legal["terminate"] and view["day"] >= 3 and rng.random() < .35:
            action["terminate"] = legal["terminate"][0]
        if legal["whisper_to"]:
            action["whisper_to"] = rng.choice(legal["whisper_to"])
            action["whisper"] = "Fixture message: compare public claims next day."
        if legal["targets"]:
            action["targets"] = rng.sample(legal["targets"], legal["n_targets"])
        if legal["say"]:
            action["say"] = "Fixture public statement. I am uncertain about the other roles."
        if legal["nominate"]:
            action["nominate"] = rng.choice(legal["nominate"])
        if legal["vote"]:
            action["vote"] = rng.random() < .75
        raw = {"id": "fixture-" + request["id"], "model": self.config.model,
               "status": "completed", "service_tier": "default",
               "usage": {"input_tokens": min(self.config.max_input_tokens, len(payload["input"]) // 3 + 1),
                         "output_tokens": min(256, remaining),
                         "output_tokens_details": {"reasoning_tokens": 0}},
               "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(action)}]}]}
        self.store.finish(call_id, raw, 0)
        return parse_action(raw)


def run(directory, seed=7, config=None, paid=False, resume=False):
    store = Store(directory)
    try:
        with store.runner_lock():
            if resume:
                if not store.initialized():
                    raise FileNotFoundError("No experiment to resume")
                store.verify()
                expected = "openai" if paid else "scripted"
                if store.meta("provider") != expected:
                    raise IntegrityError("Cannot change providers on resume")
                engine = Engine.restore(store.checkpoint())
            else:
                config = config or Config()
                engine = Engine(config.to_dict(), seed)
                store.initialize(config, seed, engine, "openai" if paid else "scripted")
            cfg = store.config
            provider = OpenAIProvider(store) if paid else ScriptedProvider(store)
            started = time.monotonic()
            status = None
            error_detail = None
            try:
                while True:
                    # A local STOP file is an operator pause, not a game loss.
                    if (store.directory / "STOP").exists():
                        status = "paused"
                        break
                    request = engine.next_request()
                    if request is None:
                        store.commit_terminal(engine.snapshot())
                        break
                    actor = request["actor"]
                    family = engine.family(actor)
                    view = engine.view(actor)
                    # Establish the readout even if this actor must budget-pass.
                    store.daily_balance(family, view["day"])
                    store.save_intent(request, view, family)
                    remaining = store.remaining(family)
                    old_call = store.call("game:" + request["id"])
                    if cfg.arm == "copied-ballot" and view["identity"]["delegate"]:
                        action, mode, call_id = engine.pass_action(request), "copied-ballot-control", None
                    elif old_call is None and remaining < cfg.min_output_tokens:
                        action, mode, call_id = engine.pass_action(request), "budget-pass", None
                    else:
                        action = provider.act(request, view, remaining, family)
                        mode, call_id = "model" if paid else "scripted", "game:" + request["id"]
                    engine.apply(request["id"], action)
                    # Decision and resulting state are committed in ONE transaction.
                    store.commit_action(request["id"], action, engine.snapshot(), call_id, mode)
            except BudgetLimit as error:
                status, error_detail = "incomplete", str(error)
                store.fault("resource_limit", error_detail)
            except (ProviderFailure, PendingCall, IntegrityError, ValueError, RuntimeError) as error:
                status, error_detail = "invalid", f"{type(error).__name__}: {error}"
                store.fault("run_invalid", error_detail)
            result = {**engine.result(), "provider": provider.name,
                      "elapsed_seconds_this_session": time.monotonic() - started,
                      "accounting": store.accounting(), "seed": store.meta("seed"),
                      "config": cfg.to_dict(), "error": error_detail,
                      "actual_paid_calls": store.accounting()["calls"] if paid else 0,
                      "simulation": not paid}
            if status is not None:
                result.update(status=status, winner=None)
            store.outcome(result)
            # Atomic derived artifact; the append-only SQLite ledger remains authoritative.
            tmp = store.directory / "result.json.tmp"
            tmp.write_text(json.dumps(result, indent=2))
            tmp.replace(store.directory / "result.json")
            return result
    finally:
        store.close()
