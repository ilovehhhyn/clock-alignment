"""Raise a paused game's output limits, record the change, and announce it to every player.

The protocol is frozen, so this is the one sanctioned mid-game change: limits may only rise, the
run must be paused between requests, and the amendment, the old and new code fingerprints and the
announcement are kept in the run's metadata. Raising both limits by the same delta for every
family changes no one's standing relative to anyone else.
"""
import time

from .config import Config
from .engine import Engine
from .store import Store, code_fingerprint

# The Messages API's largest max_tokens for the Claude models this harness serves (Opus 5.5, Haiku 5.5).
CLAUDE_MAX_OUTPUT_TOKENS = 128_000


def require_raised_limits(old, *, max_output_tokens, family_tokens):
    """Reject an amendment that lowers or keeps the limits, or that the model cannot serve."""
    if old.family_tokens is None:
        raise ValueError("this run has no family allocation; amend only a run with family_tokens set")
    if max_output_tokens < old.max_output_tokens or family_tokens < old.family_tokens:
        raise ValueError(f"limits may only rise: per answer {old.max_output_tokens} -> {max_output_tokens}, "
                         f"per family {old.family_tokens} -> {family_tokens}")
    if (max_output_tokens, family_tokens) == (old.max_output_tokens, old.family_tokens):
        raise ValueError("the amendment changes nothing; pass at least one higher limit")
    if old.profile.provider == "anthropic" and max_output_tokens > CLAUDE_MAX_OUTPUT_TOKENS:
        raise ValueError(f"max_output_tokens {max_output_tokens} exceeds the Messages API maximum "
                         f"{CLAUDE_MAX_OUTPUT_TOKENS} for {old.model}")


def render_announcement(old, new):
    """Return the Storyteller's public text for raised limits; it names both old and new values."""
    return (f"Storyteller announcement to everyone: from now on, one answer may generate up to "
            f"{new.max_output_tokens:,} tokens (was {old.max_output_tokens:,}), and each original's family "
            f"generated-token allocation is {new.family_tokens:,} tokens for the whole game (was "
            f"{old.family_tokens:,}). Tokens already generated still count, so every family's remaining "
            f"allocation rises by {new.family_tokens - old.family_tokens:,}. No other rule changes.")


def amend_limits(directory, *, max_output_tokens, family_tokens):
    """Raise a paused run's per-answer and family limits; return the stored amendment record."""
    store = Store(directory)
    try:
        with store.runner_lock():
            if not store.initialized():
                raise FileNotFoundError(f"no experiment in {directory}")
            store.verify(check_code=False)
            old = store.config
            require_raised_limits(old, max_output_tokens=max_output_tokens, family_tokens=family_tokens)
            new = Config(**{**old.to_dict(), "max_output_tokens": max_output_tokens, "family_tokens": family_tokens})
            engine = Engine.restore(store.checkpoint())
            text = render_announcement(old, new)
            engine.amend_output_limits(max_output_tokens=max_output_tokens, family_tokens=family_tokens, text=text)
            record = {"applied": time.time(), "day": engine.day, "phase": engine.stage, "announcement": text,
                      "old": {"max_output_tokens": old.max_output_tokens, "family_tokens": old.family_tokens},
                      "new": {"max_output_tokens": max_output_tokens, "family_tokens": family_tokens},
                      "old_code_fingerprint": store.meta("code_fingerprint"),
                      "new_code_fingerprint": code_fingerprint()}
            store.apply_limit_amendment(config=new, day=engine.day, snapshot=engine.snapshot(), record=record)
            return record
    finally:
        store.close()
