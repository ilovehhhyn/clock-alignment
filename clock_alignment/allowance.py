"""Decide per-call output allowances, spoken-token cuts, judge-log refunds and the final day without storage.

Per call:  output_cap = min(max_output_tokens, family_remaining), None meaning that limit is
           switched off; a family remainder below min_output_tokens is a declared pass.
Message:   say plus whisper may use at most the session's remaining spoken tokens; a longer
           message keeps a word prefix that fits together with CUT_OFF_MARK, and below
           MIN_SPOKEN_TOKENS the message fields are closed.
Refund:    judge_log tokens ~= floor(utf8_bytes / JUDGE_LOG_BYTES_PER_TOKEN), bounded by the
           call's visible (non-reasoning) output, and zero unless judge_log was written last.
Horizon:   a paid day d is declared final at the start of its night when
           game_spend + previous_cycle_cost * percent / 100 > game_dollar_limit.
           A fixed max_days horizon is the engine's own rule and needs no projection.
"""
from dataclasses import dataclass
import json

from .config import NANODOLLARS
from .schema import PROPERTIES

# English averages about 4 UTF-8 bytes per token. Dividing by 5 under-refunds typical
# text, so a family is not charged less than it generated. The count is approximate.
JUDGE_LOG_BYTES_PER_TOKEN = 5
LIMITS = ("call", "family")
CUT_OFF_MARK = " [cut off]"
SPOKEN_FIELDS = ("say", "whisper")
# Fewer spoken tokens than this cannot carry one word plus the cut-off mark (about four tokens),
# so the message fields close instead of inviting a message that would vanish.
MIN_SPOKEN_TOKENS = 8


@dataclass(frozen=True, kw_only=True)
class CallAllowance:
    """Output tokens one model call may generate, which limit set that number, and its spoken tokens left."""
    output_cap: int
    limit: str
    message_remaining: int | None

    @property
    def may_speak(self):
        return self.message_remaining is None or self.message_remaining >= MIN_SPOKEN_TOKENS

    def __post_init__(self):
        if self.limit not in LIMITS or type(self.output_cap) is not int or self.output_cap <= 0:
            raise ValueError(f"invalid call allowance {self.output_cap!r}/{self.limit!r}")


@dataclass(frozen=True, kw_only=True)
class DeclaredPass:
    """A neutral action the rules impose because an allowance cannot fund a call."""
    mode: str


def decide_allowance(config, family_remaining, message_remaining):
    """Return the CallAllowance for the next call, or the DeclaredPass the rules require."""
    limits = [(config.max_output_tokens, "call")]
    if family_remaining is not None:
        limits.append((family_remaining, "family"))
    if family_remaining is not None and family_remaining < config.min_output_tokens:
        return DeclaredPass(mode="budget-pass")
    cap, limit = min(limits, key=lambda pair: (pair[0], LIMITS.index(pair[1])))
    return CallAllowance(output_cap=cap, limit=limit, message_remaining=message_remaining)


def fit_text(text, cap, count, *, limit):
    """Return (kept, tokens): `text` whole if count(text) <= cap, else a word prefix plus the mark that fits.

    The prefix is found by proportional steps downward, so it is a fitting prefix, not always the
    longest one. cap None means no limit. Whitespace runs inside a cut message collapse to single
    spaces, and the kept text never exceeds `limit` characters. count must be deterministic.
    """
    tokens = count(text) if text else 0
    if cap is None or tokens <= cap:
        return text, tokens
    words = text.split()
    keep = min(len(words) - 1, len(words) * cap // max(tokens, 1))
    while keep > 0:
        candidate = " ".join(words[:keep]) + CUT_OFF_MARK
        tokens = count(candidate)
        if tokens <= cap and len(candidate) <= limit:
            return candidate, tokens
        keep = min(keep - 1, keep * cap // max(tokens, 1))
    return "", 0


def fit_message(action, cap, count):
    """Cut the action's spoken fields to `cap` tokens in all and return (action, spoken tokens).

    A message emptied by the cut sends nothing, so its recipient and audience are cleared.
    """
    used = 0
    for key in SPOKEN_FIELDS:
        if not action[key]:
            continue
        remaining = None if cap is None else cap - used
        kept, tokens = fit_text(action[key], remaining, count, limit=PROPERTIES[key]["maxLength"])
        action = {**action, key: kept}
        if kept == "":
            action["audience"] = None
            if key == "whisper":
                action["whisper_to"] = None
        used += tokens
    return action, used


def judge_log_written_last(raw_text):
    """Report whether the model emitted judge_log as the final top-level key."""
    pairs = json.loads(raw_text, object_pairs_hook=lambda items: items)
    keys = [key for key, _ in pairs]
    return len(keys) > 0 and keys[-1] == "judge_log" and keys.count("judge_log") == 1


def judge_log_refund(*, raw_text, action, output_tokens, reasoning_tokens):
    """Return the judge-log tokens to exclude from allowances, or 0 if the order was violated."""
    if not judge_log_written_last(raw_text):
        return 0
    estimate = len(action["judge_log"].encode()) // JUDGE_LOG_BYTES_PER_TOKEN
    return min(estimate, output_tokens - reasoning_tokens)


def projected_over_budget(*, game_spend_nano, previous_cycle_nano, config):
    """Decide whether one more night-and-day cycle would pass the gameplay dollar ceiling."""
    if previous_cycle_nano is None:
        return False
    projected = game_spend_nano + previous_cycle_nano * config.final_day_projection_percent // 100
    return projected > config.game_dollar_limit * NANODOLLARS
