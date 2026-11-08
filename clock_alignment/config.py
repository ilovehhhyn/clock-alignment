"""Frozen protocol and pricing. No implicit upgrades or model fallback."""
from dataclasses import asdict, dataclass
import hashlib
import json

PROTOCOL = "dynamic-delegates-v1"
PRICE_VERIFIED = "2026-11-08"
NANODOLLARS = 1_000_000_000
INPUT_NANO_PER_TOKEN = 2_500  # $2.50/M: reserve all input as short cache writes
OUTPUT_NANO_PER_TOKEN = 10_000  # $10/M, includes native reasoning tokens


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Config:
    protocol: str = PROTOCOL
    model: str = "gpt-6.1-sol"
    effort: str = "medium"
    arm: str = "dynamic-voting"
    family_tokens: int = 32_768
    max_children: int = 2
    max_depth: int = 3
    max_days: int = 6
    night_rounds: int = 2
    discussion_rounds: int = 2
    max_input_tokens: int = 48_000
    max_output_tokens: int = 4_096
    min_output_tokens: int = 512
    max_paid_calls: int = 1_500
    dollar_limit: int = 90
    analysis_dollar_limit: int = 8
    analysis_output_tokens: int = 24_576
    max_probes: int = 12
    timeout_seconds: int = 180
    price_verified: str = PRICE_VERIFIED

    def __post_init__(self):
        if self.protocol != PROTOCOL or self.model != "gpt-6.1-sol" or self.effort != "medium":
            raise ValueError("Unvalidated protocol/model/effort; update the protocol and pricing explicitly")
        if self.arm not in {"dynamic-voting", "solo", "advisory", "copied-ballot"}:
            raise ValueError("Unknown experimental arm")
        for key, value in asdict(self).items():
            if isinstance(value, int) and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"{key} must be a positive integer")
        if not 0 < self.dollar_limit < 100 or self.analysis_dollar_limit > self.dollar_limit:
            raise ValueError("Total dollar cap must be below $100 and contain the analysis cap")
        if self.min_output_tokens > self.max_output_tokens or self.max_input_tokens > 48_000:
            raise ValueError("Unsupported context/output geometry")
        if self.price_verified != PRICE_VERIFIED:
            raise ValueError("Pricing manifest mismatch")

    def to_dict(self):
        return asdict(self)

    def bound(self):
        input_nano = self.max_paid_calls * self.max_input_tokens * INPUT_NANO_PER_TOKEN
        output_nano = (12 * self.family_tokens + self.analysis_output_tokens) * OUTPUT_NANO_PER_TOKEN
        return {"max_input_usd": input_nano / NANODOLLARS,
                "max_output_usd": output_nano / NANODOLLARS,
                "uncapped_geometry_usd": (input_nano + output_nano) / NANODOLLARS,
                "enforced_spend_bound_usd": min((input_nano + output_nano) / NANODOLLARS, self.dollar_limit),
                "hard_usd_cap": self.dollar_limit,
                "game_output_tokens": 12 * self.family_tokens,
                "analysis_output_tokens": self.analysis_output_tokens,
                "pricing_date": self.price_verified}
