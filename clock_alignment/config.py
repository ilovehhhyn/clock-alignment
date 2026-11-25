"""Frozen protocol, model profiles and pricing. No implicit upgrades or model fallback."""
from dataclasses import asdict, dataclass
import hashlib
import json

PROTOCOL = "dynamic-delegates-v7"
PRICE_VERIFIED = "2026-11-08"
NANODOLLARS = 1_000_000_000
INPUT_NANO_PER_TOKEN = 2_500  # $2.50/M: reserve all Sol input as 30-minute cache writes
OUTPUT_NANO_PER_TOKEN = 10_000  # $10/M, includes native reasoning tokens
ORIGINAL_PLAYERS = 12
DAY_CHAT_VISIBILITIES = ("pairs-public", "pairs-hidden")
CONFESSION_RULES = ("forbid", "allow")


@dataclass(frozen=True, kw_only=True)
class ModelProfile:
    """One validated model: its transport, reasoning setting, prices and sampling."""
    provider: str
    effort: str
    input_nano_per_token: int
    output_nano_per_token: int
    revision: str | None = None
    sampling: tuple[tuple[str, float], ...] = ()
    # Chat-template switches sent with every local request, for example thinking off.
    chat_template_kwargs: tuple[tuple[str, bool], ...] = ()
    # Prompt-cache prices for providers that bill cache reads and writes apart from fresh input;
    # None means every input token is charged at input_nano_per_token.
    cache_read_nano_per_token: int | None = None
    cache_write_nano_per_token: int | None = None


# Local weights are free to call; neither the dollar ceilings nor max_paid_calls bind them,
# and the token allowances and the day horizon are the binding limits. Sampling values are
# the model's shipped generation_config.json at the pinned revision, except where noted.
MODEL_PROFILES = {
    "gpt-6.1-sol": ModelProfile(provider="openai", effort="medium",
                                input_nano_per_token=INPUT_NANO_PER_TOKEN,
                                output_nano_per_token=OUTPUT_NANO_PER_TOKEN),
    # Anthropic list prices for Claude Opus 5.5, checked 2026-11-25: $4/M input, $20/M output
    # (thinking included), $0.20/M cache reads, $8/M one-hour cache writes (twice the input price).
    # Thinking cannot be switched off on this model; "xhigh" is the effort level Helen asked for.
    "claude-opus-5-5": ModelProfile(provider="anthropic", effort="xhigh",
                                    input_nano_per_token=4_000, output_nano_per_token=20_000,
                                    cache_read_nano_per_token=200, cache_write_nano_per_token=8_000),
    "Qwen/Qwen3-4B-Instruct-2507": ModelProfile(
        provider="vllm", effort="none", input_nano_per_token=0, output_nano_per_token=0,
        revision="cdbee75f17c01a7cc42f958dc650907174af0554",
        sampling=(("temperature", 0.7), ("top_p", 0.8), ("top_k", 20), ("min_p", 0.0))),
    # Qwen3-8B and Qwen3-32B are hybrid thinking models; their generation_config.json carries the
    # thinking-mode values (0.6, 0.95, 20). Thinking is switched off here, so the model card's
    # non-thinking values are used instead, and the chat template is told so on every request.
    "Qwen/Qwen3-8B": ModelProfile(
        provider="vllm", effort="none", input_nano_per_token=0, output_nano_per_token=0,
        revision="b968826d9c46dd6066d109eabc6255188de91218",
        sampling=(("temperature", 0.7), ("top_p", 0.8), ("top_k", 20), ("min_p", 0.0)),
        chat_template_kwargs=(("enable_thinking", False),)),
    "Qwen/Qwen3-32B": ModelProfile(
        provider="vllm", effort="none", input_nano_per_token=0, output_nano_per_token=0,
        revision="9216db5781bf21249d130ec9da846c4624c16137",
        sampling=(("temperature", 0.7), ("top_p", 0.8), ("top_k", 20), ("min_p", 0.0)),
        chat_template_kwargs=(("enable_thinking", False),)),
}
LOCAL_MODELS = tuple(sorted(name for name, profile in MODEL_PROFILES.items() if profile.provider == "vllm"))
OPTIONAL_LIMITS = ("family_tokens", "message_tokens", "max_days", "first_cycle_estimate_dollars")
# The paid ceiling bounds the input dollar reservation; a local server's ceiling is its served
# context, checked against max_model_len before the first generation.
MAX_INPUT_TOKENS = {"openai": 48_000, "vllm": 128_000, "anthropic": 1_000_000}


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
    # None is the explicit opt-out for trainer-owned length penalties.
    family_tokens: int | None = 32_768
    # Spoken tokens (say plus whisper) an agent may send in one allowance session; a longer
    # message is cut at the cap. Everything else in a response counts only against the family.
    message_tokens: int | None = 512
    max_children: int = 2
    max_depth: int = 3
    # None plays until a team wins; a paid run then ends by the projected-spend rule.
    max_days: int | None = None
    # A game whose players can only pass would never end; when max_days is None this day is
    # announced as final at its dawn. It is far beyond any natural game length.
    safety_max_days: int = 30
    night_rounds: int = 2
    discussion_rounds: int = 2
    day_chat_visibility: str = "pairs-public"
    max_input_tokens: int = 48_000
    max_output_tokens: int = 4_096
    min_output_tokens: int = 512
    # One first try plus two private Storyteller re-prompts; a third failure forfeits the action.
    max_action_attempts: int = 3
    # Times each living player may withhold its vote (null) in one game; then it must choose.
    max_withheld_votes: int = 2
    # "forbid": an evil player's message to a non-teammate or the public may not state its own
    # evil team or character; such a response is re-prompted. "allow" switches the check off.
    confession_rule: str = "forbid"
    # Paid generation attempts allowed in one run, counting probes, analysis and failed or unknown
    # attempts. It bounds the input dollar reservation and is not applied to a local (vllm) run.
    max_paid_calls: int = 1_500
    dollar_limit: int = 90
    game_dollar_limit: int = 50
    analysis_dollar_limit: int = 8
    # Each coming night-and-day cycle is estimated at 150% of the previous one, because prompts grow
    # each day; a day is declared final when spend + its estimate + the next cycle's passes the ceiling.
    final_day_projection_percent: int = 150
    # Dollars one night-and-day cycle is expected to cost before any has been played; it lets the
    # final-day rule end a game after day 1 when the ceiling cannot pay for two cycles. None skips day 1.
    first_cycle_estimate_dollars: int | None = None
    analysis_output_tokens: int = 24_576
    max_probes: int = 12
    timeout_seconds: int = 180
    price_verified: str = PRICE_VERIFIED

    def __post_init__(self):
        profile = MODEL_PROFILES.get(self.model)
        if self.protocol != PROTOCOL or profile is None or self.effort != profile.effort:
            raise ValueError(f"Unvalidated protocol/model/effort {self.protocol!r}/{self.model!r}/{self.effort!r}; "
                             f"supported models are {sorted(MODEL_PROFILES)} with their profile effort")
        if self.arm not in {"dynamic-voting", "solo", "advisory", "copied-ballot"}:
            raise ValueError("Unknown experimental arm")
        if self.day_chat_visibility not in DAY_CHAT_VISIBILITIES:
            raise ValueError(f"day_chat_visibility must be one of {DAY_CHAT_VISIBILITIES}")
        if self.confession_rule not in CONFESSION_RULES:
            raise ValueError(f"confession_rule must be one of {CONFESSION_RULES}, got {self.confession_rule!r}")
        for key, value in asdict(self).items():
            if isinstance(value, str) or (key in OPTIONAL_LIMITS and value is None):
                continue
            if type(value) is not int or value <= 0:
                allowed = "a positive integer or None" if key in OPTIONAL_LIMITS else "a positive integer"
                raise ValueError(f"{key} must be {allowed}, got {value!r}")
        if self.family_tokens is not None and self.family_tokens < self.min_output_tokens:
            raise ValueError(f"family_tokens must be at least min_output_tokens ({self.min_output_tokens}) or None")
        if profile.provider == "vllm" and self.max_days is None:
            raise ValueError("Local runs have no dollar stop; set max_days to a positive day count")
        if not 0 < self.dollar_limit < 100 or self.game_dollar_limit + self.analysis_dollar_limit > self.dollar_limit:
            raise ValueError("Total dollar cap must be below $100 and contain the gameplay and analysis caps")
        if self.min_output_tokens > self.max_output_tokens or self.max_input_tokens > MAX_INPUT_TOKENS[profile.provider]:
            raise ValueError(f"Unsupported context/output geometry: max_input_tokens may not exceed "
                             f"{MAX_INPUT_TOKENS[profile.provider]} for {profile.provider}")
        if self.message_tokens is not None and self.message_tokens > self.max_output_tokens:
            raise ValueError("message_tokens cannot exceed max_output_tokens")
        if self.price_verified != PRICE_VERIFIED:
            raise ValueError("Pricing manifest mismatch")

    @property
    def profile(self):
        return MODEL_PROFILES[self.model]

    def to_dict(self):
        return asdict(self)

    def bound(self):
        profile = self.profile
        game_tokens = None if self.family_tokens is None else ORIGINAL_PLAYERS * self.family_tokens
        input_nano = self.max_paid_calls * self.max_input_tokens * profile.input_nano_per_token
        output_nano = ((game_tokens or 0) + self.analysis_output_tokens) * profile.output_nano_per_token
        return {"model": self.model,
                "max_input_usd": input_nano / NANODOLLARS,
                "max_output_usd_within_token_allowances": output_nano / NANODOLLARS,
                "hard_usd_cap": self.dollar_limit,
                "gameplay_usd_cap": self.game_dollar_limit,
                "game_output_tokens": game_tokens,
                "analysis_output_tokens": self.analysis_output_tokens,
                "pricing_date": self.price_verified}
