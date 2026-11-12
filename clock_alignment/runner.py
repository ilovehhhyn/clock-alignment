"""Advance exactly one committed decision at a time; round barriers live in Engine."""
from dataclasses import dataclass
import json
import random
import time

from .allowance import DeclaredPass, decide_allowance, projected_over_budget
from .briefing import render_confirmation_notice, render_retry_notice
from .config import Config, NANODOLLARS, digest
from .engine import Engine, MessageRejected, names_player, same_choices, without_message
from .local_provider import LocalProvider
from .provider import (AllowanceCutoff, CapHit, InvalidResponse, OpenAIProvider, ProviderFailure, TransportFailure,
                       action_payload, attempt_call_id, settle_action, try_call_id)
from .schema import empty_action
from .store import BudgetLimit, IntegrityError, PendingCall, Store

PROVIDERS = ("scripted", "openai", "vllm")


def word_count(text):
    """The fixture's tokenizer: one token per whitespace-separated word."""
    return len(text.split())


class ScriptedProvider:
    """A deterministic plumbing fixture, NEVER a model or scientific baseline."""
    name = "scripted"
    transport_retry_safe = False

    def __init__(self, store):
        self.store, self.config = store, store.config

    def act(self, request, view, allowance, family, attempt=1, notice=None, transport_try=1):
        if transport_try != 1:
            raise ValueError("The scripted fixture never needs a second transport try")
        call_id = attempt_call_id(request["id"], attempt)
        old = self.store.call(call_id)
        if old:
            if old["status"] != "completed":
                raise PendingCall("Scripted call pending")
            return settle_action(self.store, call_id, json.loads(old["response_json"]),
                                 json.loads(old["request_json"])["max_output_tokens"], word_count)
        payload = action_payload(self.config, request, view, allowance,
                                 self.store.daily_balance(family, view["day"]))
        self.store.reserve(call_id, "game", request["actor"], family, payload["max_output_tokens"], payload,
                           request["session"], allowance.message_remaining)
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
            action["audience"] = "team" if action["whisper_to"] in legal["team_links"] else "unknown"
        if legal["targets"]:
            action["targets"] = rng.sample(legal["targets"], legal["n_targets"])
        if legal["say"]:
            action["say"] = "Fixture public statement. I am uncertain about the other roles."
            action["audience"] = "public"
        if legal["nominate"]:
            action["nominate"] = rng.choice(legal["nominate"])
        if legal["vote"]:
            action["vote"] = rng.random() < .75
        action["judge_log"] = "SCRIPTED FIXTURE judge log: no reasoning occurred."
        raw = {"id": "fixture-" + request["id"], "model": self.config.model,
               "status": "completed", "service_tier": "default",
               "usage": {"input_tokens": min(self.config.max_input_tokens, len(payload["input"]) // 3 + 1),
                         "output_tokens": min(256, allowance.output_cap),
                         "output_tokens_details": {"reasoning_tokens": 0}},
               "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(action)}]}]}
        self.store.finish(call_id, raw, 0)
        return settle_action(self.store, call_id, raw, payload["max_output_tokens"], word_count)


def make_provider(kind, store, endpoint=None):
    if kind == "scripted":
        return ScriptedProvider(store)
    if kind == "openai":
        return OpenAIProvider(store)
    if kind == "vllm":
        return LocalProvider(store, endpoint=endpoint)
    raise ValueError(f"unknown provider {kind!r}; choose from {PROVIDERS}")


def declare_final_day_if_due(engine, store):
    """At the start of each night, durably declare that day final if one more cycle would overspend."""
    at_night_start = (engine.status == "running" and engine.stage == "night_chat" and engine.round == 1
                      and not engine.queue and engine.final_day is None and engine.day > 1)
    if not at_night_start:
        return
    # Only finished days count, so a crash after tonight's first call cannot change the decision.
    spend, previous = store.game_spend_before_nano(engine.day), store.cycle_cost_nano(engine.day - 1)
    if not projected_over_budget(game_spend_nano=spend, previous_cycle_nano=previous, config=store.config):
        return
    engine.declare_final_day(f"gameplay spend ${spend / NANODOLLARS:.2f} plus "
                             f"{store.config.final_day_projection_percent}% of day {engine.day - 1}'s "
                             f"${previous / NANODOLLARS:.2f} would pass ${store.config.game_dollar_limit}")
    store.commit_snapshot(engine.snapshot())


# New tries per attempt in one process. A server that fails three in a row is dead: the run stops,
# Slurm ends the job, and the next link resumes from the same attempt on a fresh server.
MAX_TRANSPORT_TRIES = 3


@dataclass(frozen=True, kw_only=True)
class AttemptResult:
    """One player attempt: an action to apply, or the reason the Storyteller must wake the actor again.

    A rejected answer is exempt from the token allowances unless it ran to the per-call maximum;
    `candidate` keeps a legally chosen action whose only fault was its message.
    """
    action: dict | None
    mode: str | None
    call_id: str | None
    fresh: bool
    failure: str | None = None
    exempt: bool = True
    candidate: dict | None = None


def attempt_action(engine, store, provider, request, view, family, attempt, notice):
    """Run one player attempt, sending transport tries until the server answers.

    Transport tries never use up a player attempt. A failed or interrupted free (local) try
    is released and skipped, also on resume; a paid call is never sent twice.
    """
    attempt_id = attempt_call_id(request["id"], attempt)
    new_tries = 0
    transport_try = 0
    while True:
        transport_try += 1
        call_id = try_call_id(attempt_id, transport_try)
        old = store.call(call_id)
        if old is not None and old["status"] != "completed":
            if not provider.transport_retry_safe:
                raise PendingCall("Existing request may have been billed. Stop for reconciliation; no blind retry.")
            store.release(call_id, "interrupted")
            continue
        allowance = None
        if old is None:
            if new_tries == MAX_TRANSPORT_TRIES:
                raise ProviderFailure(f"{attempt_id}: no usable answer in {MAX_TRANSPORT_TRIES} tries")
            new_tries += 1
            allowance = decide_allowance(store.config, store.remaining(family),
                                         store.message_remaining(request["actor"], request["session"]))
            if isinstance(allowance, DeclaredPass):
                return AttemptResult(action=engine.pass_action(request), mode=allowance.mode, call_id=None, fresh=True)
        try:
            candidate = provider.act(request, view, allowance, family, attempt=attempt, notice=notice,
                                     transport_try=transport_try)
        except AllowanceCutoff:
            return AttemptResult(action=engine.pass_action(request), mode="time-ran-out", call_id=call_id,
                                 fresh=old is None)
        except CapHit as error:
            return AttemptResult(action=None, mode=None, call_id=call_id, fresh=old is None, failure=str(error),
                                 exempt=False)
        except InvalidResponse as error:
            return AttemptResult(action=None, mode=None, call_id=call_id, fresh=old is None, failure=str(error))
        except TransportFailure as error:
            if not provider.transport_retry_safe:
                raise
            store.fault("transport_retry", f"{call_id}: {error}")
            continue
        try:
            engine.check_action(request["id"], candidate)
        except MessageRejected as error:
            return AttemptResult(action=None, mode=None, call_id=call_id, fresh=old is None,
                                 failure=f"illegal message: {error}", candidate=candidate)
        except ValueError as error:
            return AttemptResult(action=None, mode=None, call_id=call_id, fresh=old is None,
                                 failure=f"illegal action: {error}")
        return AttemptResult(action=candidate, mode="scripted" if provider.name == "scripted" else "model",
                             call_id=call_id, fresh=old is None)


# Modes of a first answer that the Storyteller checks before committing; passes and forfeits name nobody.
ANSWERED_MODES = ("model", "scripted")


def confirm_named_players(engine, store, provider, request, view, family, first, attempt):
    """Ask the actor once to confirm or change an answer that names another player; the reply is the decision.

    The check is the Storyteller's, so the first answer is superseded and not charged. A reply that is not a
    legal action is a recorded fault and leaves the first answer standing, as does a reply the allowances
    cut short. One check is asked, whatever the reply names.
    """
    if first.mode not in ANSWERED_MODES or not names_player(first.action):
        return first
    notice = render_confirmation_notice(engine.confirmation_facts(request["id"], first.action))
    check = attempt_action(engine, store, provider, request, view, family, attempt + 1, notice)
    if check.action is None:
        if check.fresh:
            store.fault("agent_retry", f"{check.call_id}: unusable reply to the Storyteller check: {check.failure}")
        if check.exempt:
            store.exempt_rejected(check.call_id)
        return AttemptResult(action=first.action, mode="unconfirmed", call_id=first.call_id, fresh=first.fresh)
    if check.mode not in ANSWERED_MODES:
        return AttemptResult(action=first.action, mode="unconfirmed", call_id=first.call_id, fresh=first.fresh)
    store.supersede(first.call_id)
    mode = "confirmed" if same_choices(first.action, check.action) else "revised"
    return AttemptResult(action=check.action, mode=mode, call_id=check.call_id, fresh=check.fresh)


def wake_until_answered(engine, store, provider, request, view, family):
    """Return the AttemptResult to commit after at most max_action_attempts private wake-ups.

    A rejected answer delivered nothing, so it is exempt from the actor's token allowances, except
    one that ran to the per-call maximum. When the last answer's only fault was its message, the
    forfeit keeps its choices and drops the speech; otherwise the forfeit is a neutral pass. A legal
    answer that names another player is confirmed once before it is returned; a forfeit is not.
    """
    attempts = store.config.max_action_attempts
    notice = None
    for attempt in range(1, attempts + 1):
        result = attempt_action(engine, store, provider, request, view, family, attempt, notice)
        if result.action is not None:
            return confirm_named_players(engine, store, provider, request, view, family, result, attempt)
        if result.fresh:
            store.fault("agent_retry", f"{result.call_id}: {result.failure}")
        silenced_last = attempt == attempts and result.candidate is not None
        if result.exempt and not silenced_last:
            store.exempt_rejected(result.call_id)
        notice = render_retry_notice(result.failure, attempt + 1, attempts)
    if result.candidate is not None:
        # The choices are delivered and charged; the undelivered speech leaves the spoken cap alone.
        silenced = without_message(result.candidate)
        engine.check_action(request["id"], silenced)
        store.silence(result.call_id)
        return AttemptResult(action=silenced, mode="speech-forfeit", call_id=result.call_id, fresh=True)
    return AttemptResult(action=engine.pass_action(request), mode="forfeit", call_id=None, fresh=True)


def play_turn(engine, store, provider, request):
    """Choose, apply and durably commit one actor's action for `request`.

    The Storyteller wakes the actor until it gives one legal action: a malformed, refused or
    illegal response is re-prompted privately, at most max_action_attempts times in all, and
    a final failure is a recorded forfeit (a neutral pass). Transport failures are retried
    only where resending is free; otherwise they stop the run as infrastructure faults.
    """
    actor = request["actor"]
    family = engine.family(actor)
    view = engine.view(actor)
    # Establish the readout even if this actor must pass.
    store.daily_balance(family, view["day"])
    store.save_intent(request, view, family)
    if store.config.arm == "copied-ballot" and view["identity"]["delegate"]:
        result = AttemptResult(action=engine.pass_action(request), mode="copied-ballot-control", call_id=None,
                               fresh=True)
    else:
        result = wake_until_answered(engine, store, provider, request, view, family)
    engine.apply(request["id"], result.action)
    # Decision and resulting state are committed in ONE transaction.
    store.commit_action(request["id"], result.action, engine.snapshot(), result.call_id, result.mode)


def run(directory, seed=7, config=None, provider="scripted", resume=False, endpoint=None):
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; choose from {PROVIDERS}")
    store = Store(directory)
    try:
        with store.runner_lock():
            if resume:
                if not store.initialized():
                    raise FileNotFoundError("No experiment to resume")
                store.verify()
                if store.meta("provider") != provider:
                    raise IntegrityError("Cannot change providers on resume")
                engine = Engine.restore(store.checkpoint())
            else:
                config = config or Config()
                if provider != "scripted" and config.profile.provider != provider:
                    raise ValueError(f"Model {config.model!r} is not served by provider {provider!r}")
                engine = Engine(config.to_dict(), seed)
                store.initialize(config, seed, engine, provider)
            cfg = store.config
            agent = make_provider(provider, store, endpoint)
            started = time.monotonic()
            status = None
            error_detail = None
            try:
                while True:
                    # A local STOP file is an operator pause, not a game loss.
                    if (store.directory / "STOP").exists():
                        status = "paused"
                        break
                    declare_final_day_if_due(engine, store)
                    request = engine.next_request()
                    if request is None:
                        store.commit_snapshot(engine.snapshot())
                        break
                    play_turn(engine, store, agent, request)
            except BudgetLimit as error:
                status, error_detail = "incomplete", str(error)
                store.fault("resource_limit", error_detail)
            except (ProviderFailure, PendingCall, IntegrityError, ValueError, RuntimeError) as error:
                status, error_detail = "invalid", f"{type(error).__name__}: {error}"
                store.fault("run_invalid", error_detail)
            accounting = store.accounting()
            result = {**engine.result(), "provider": agent.name,
                      "elapsed_seconds_this_session": time.monotonic() - started,
                      "accounting": accounting, "seed": store.meta("seed"),
                      "config": cfg.to_dict(), "error": error_detail,
                      "actual_paid_calls": accounting["calls"] if provider == "openai" else 0,
                      "simulation": provider == "scripted"}
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
