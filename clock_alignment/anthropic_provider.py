"""Serve gameplay from the Claude Messages API with the same prompt text, schema, receipts and limits.

The observation text, action schema and allowances are the OpenAI path's, byte for byte; the
system prompt is the shared one with two sentences reworded (prompts.CLAUDE_REWORDING). The observation text is sent as consecutive text blocks so that the part a player will see
again in its next call that day can be read back from Anthropic's prompt cache; joined, the blocks
are exactly the shared text. Replies are normalized into the OpenAI receipt shape before any check
or settlement, and the store charges them at the profile's cache-aware prices.
"""
import json
import os
import time

from .config import canonical
from .prompts import CLAUDE_SYSTEM
from .provider import (ContextOverflow, InvalidResponse, ProviderFailure, TransportFailure, attempt_call_id,
                       attempt_turn, observation_text, settle_action, try_call_id, validate_receipt, wire_json)
from .schema import request_schema
from .store import IntegrityError, PendingCall

API_KEY_ENV = "ANTHROPIC_API_KEY"
# A player's consecutive calls are 10 to 25 minutes apart, past the 5-minute default lifetime.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
# Cache read points fall every 4,096 characters (about 1,000 tokens); a lookup searches the 20 blocks
# before a breakpoint, and one player's view grows far less than 20 chunks between its calls.
CHUNK_CHARS = 4_096
# Structured outputs reject these JSON Schema limits; schema.validate_action enforces them after the reply.
UNSUPPORTED_SCHEMA_KEYS = ("maxItems", "maxLength", "minimum", "maximum")
# The SDK resends 429, 5xx and dropped connections with backoff before a call counts as failed.
SDK_MAX_RETRIES = 6
THINKING = {"type": "adaptive", "display": "summarized"}  # summaries are kept for observers only
COUNTED_FIELDS = ("model", "system", "messages", "thinking", "output_config")
REFUSAL_FAULT = "model_refusal"


def claude_schema(node):
    """Return a copy of a JSON Schema in the form Claude's structured outputs accept.

    Drops the rejected keywords, and rewrites a list of types ({"type": ["string", "null"], "enum": [...]})
    as anyOf with one branch per type, each keeping only its own enum values, because the API checks
    every enum value against each listed type.
    """
    if isinstance(node, list):
        return [claude_schema(value) for value in node]
    if not isinstance(node, dict):
        return node
    node = {key: claude_schema(value) for key, value in node.items() if key not in UNSUPPORTED_SCHEMA_KEYS}
    if not isinstance(node.get("type"), list):
        return node
    types, enum = node.pop("type"), node.pop("enum", None)
    description = node.pop("description", None)
    branches = []
    for name in types:
        branch = {"type": name}
        if name == "null":
            branches.append(branch)
            continue
        branch.update(node)
        if enum is not None:
            values = [value for value in enum if json_type(value) == name]
            if not values:
                continue
            branch["enum"] = values
        branches.append(branch)
    merged = {"anyOf": branches}
    if description is not None:
        merged["description"] = description
    return merged


def json_type(value):
    """Return the JSON Schema type name of an enum value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "integer" if isinstance(value, int) else "number"
    raise ValueError(f"enum value {value!r} has no JSON Schema type")


def stable_prefix_length(text, view):
    """Return how many leading characters of the observation text the player's next call that day repeats.

    canonical() sorts keys, so the text opens with {"authorized_observation":{"day":D,"events":[...
    and a later call on the same day only appends events; everything after the events array may change.
    """
    head = canonical({"authorized_observation": {"day": view["day"], "events": view["events"]}})
    stable = head[:-len("]}}")]
    if not text.startswith(stable):
        raise ValueError("the observation text must open with its day and events; the cache split assumes it")
    return len(stable)


def observation_blocks(text, stable_length):
    """Split the text into fixed-width blocks, with a cache breakpoint on the last block inside the stable prefix.

    Blocks start at multiples of CHUNK_CHARS, so the blocks of an earlier call that lie inside its
    stable prefix reappear byte for byte in the next call and its breakpoint can be read back.
    """
    full_chunks = stable_length // CHUNK_CHARS
    blocks = [{"type": "text", "text": text[index * CHUNK_CHARS:(index + 1) * CHUNK_CHARS]} for index in range(full_chunks)]
    if full_chunks > 0:
        blocks[-1]["cache_control"] = dict(CACHE_CONTROL)
    blocks.append({"type": "text", "text": text[full_chunks * CHUNK_CHARS:]})
    return blocks


def claude_action_payload(config, request, view, allowance, displayed_balance):
    text = observation_text(request, view, allowance, displayed_balance)
    schema = claude_schema(request_schema(request["legal"], speaking=allowance.may_speak))
    return {"model": config.model, "max_tokens": allowance.output_cap,
            "system": [{"type": "text", "text": CLAUDE_SYSTEM, "cache_control": dict(CACHE_CONTROL)}],
            "messages": [{"role": "user", "content": observation_blocks(text, stable_prefix_length(text, view))}],
            "thinking": dict(THINKING),
            "output_config": {"effort": config.effort, "format": {"type": "json_schema", "schema": schema}}}


def joined_text(payload):
    """Return the user turn's text as the model reads it: its blocks joined in order."""
    return "".join(block["text"] for block in payload["messages"][0]["content"])


def require_payload_contract(config, payload):
    messages = payload.get("messages")
    content = messages[0].get("content") if isinstance(messages, list) and len(messages) == 1 else None
    blocks_ok = (isinstance(content, list) and len(content) >= 1
                 and all(block.get("type") == "text" and isinstance(block.get("text"), str) for block in content))
    output = payload.get("output_config", {})
    if (set(payload) != {"model", "max_tokens", *COUNTED_FIELDS} or payload["model"] != config.model
            or payload["system"] != [{"type": "text", "text": CLAUDE_SYSTEM, "cache_control": CACHE_CONTROL}]
            or messages[0].get("role") != "user" or not blocks_ok or payload["thinking"] != THINKING
            or set(output) != {"effort", "format"} or output["effort"] != config.effort
            or output["format"].get("type") != "json_schema"):
        raise ValueError("Request violates the frozen Claude provider contract")


def normalize_receipt(raw, model, counted):
    """Map one Messages API reply onto the OpenAI receipt shape; malformed usage stays unsettled.

    input_tokens is the whole prompt (fresh + cache reads + cache writes); its details keep the split
    the store needs to charge cache-aware prices.
    """
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    fresh, read, written = (usage.get("input_tokens"), usage.get("cache_read_input_tokens") or 0,
                            usage.get("cache_creation_input_tokens") or 0)
    parts = (fresh, read, written)
    total = sum(parts) if all(type(part) is int for part in parts) else None
    content = raw.get("content") if isinstance(raw.get("content"), list) else []
    texts = [block.get("text") for block in content if isinstance(block, dict) and block.get("type") == "text"]
    stop = raw.get("stop_reason")
    receipt = {"id": raw.get("id"), "model": raw.get("model"), "service_tier": "default",
               "status": "completed" if stop == "end_turn" else "incomplete",
               "usage": {"input_tokens": total, "output_tokens": usage.get("output_tokens"),
                         "input_tokens_details": {"cached_tokens": read, "cache_write_tokens": written},
                         "output_tokens_details": {"reasoning_tokens": 0}},
               "output": [{"type": "message", "content": [{"type": "output_text", "text": text} for text in texts]}],
               "provider_response": raw, "_local_counted_input_tokens": counted, "_requested_model": model}
    if stop == "max_tokens":
        receipt["incomplete_details"] = {"reason": "max_output_tokens"}
    elif stop == "refusal":
        details = raw.get("stop_details") if isinstance(raw.get("stop_details"), dict) else {}
        receipt["incomplete_details"] = {"reason": f"refusal ({details.get('category')})"}
    elif stop != "end_turn":
        receipt["incomplete_details"] = {"reason": f"unexpected stop {stop!r}"}
    return receipt


class SDKTransport:
    """Send counts and generations through the official SDK; a generation streams to its final message."""

    def __init__(self, key, timeout):
        import anthropic  # needed only for live Claude runs; the offline suite never loads it
        self.anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=SDK_MAX_RETRIES)

    def count(self, body):
        return self._call(lambda: self.client.messages.count_tokens(**body).input_tokens)

    def generate(self, payload):
        def send():
            with self.client.messages.stream(**payload) as stream:
                return stream.get_final_message().to_dict()
        return self._call(send)

    def _call(self, send):
        errors = self.anthropic
        try:
            return send()
        except errors.APIConnectionError as error:
            raise TransportFailure(f"{type(error).__name__}: {error}") from error
        except errors.APIStatusError as error:
            if error.status_code == 429 or error.status_code >= 500:
                raise TransportFailure(f"HTTP {error.status_code} after {SDK_MAX_RETRIES} retries: {error}") from error
            raise ProviderFailure(f"HTTP {error.status_code}: {error}") from error


class AnthropicProvider:
    name = "anthropic"
    # A failed paid try keeps its full dollar hold and charges no tokens, so the runner may send a new try.
    transport_retry_safe = True

    def __init__(self, store, transport=None):
        self.store = store
        self.config = store.config
        if self.config.profile.provider != "anthropic":
            raise ValueError(f"Model {self.config.model!r} is not served by the Claude Messages API")
        if transport is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise ProviderFailure(f"Set {API_KEY_ENV} in the run's environment; never pass it as an argument")
            transport = SDKTransport(key, self.config.timeout_seconds)
        self.transport = transport

    def release_interrupted(self, call_id):
        self.store.abandon(call_id, "interrupted")

    def count_tokens(self, text, call_id):
        """Count `text` with the model's tokenizer as one user turn; a few framing tokens are included."""
        try:
            count = self.transport.count({"model": self.config.model, "messages": [{"role": "user", "content": text}]})
        except ProviderFailure as error:
            raise ProviderFailure(f"could not count the spoken tokens of settled call {call_id}; "
                                  "resume the run to count its saved receipt") from error
        if type(count) is not int or count < 0:
            raise ProviderFailure(f"token count returned {count!r} for spoken text of {call_id}")
        return count

    def response(self, call_id, payload, actor, family, session, message_cap):
        payload = json.loads(wire_json(payload))
        require_payload_contract(self.config, payload)
        old = self.store.call(call_id)
        if old:
            if old["request_json"] != canonical(payload):
                raise IntegrityError("Replay payload differs from saved request")
            if old["status"] != "completed":
                raise PendingCall("An unsettled paid try must be abandoned and a new try sent, never replayed")
            return validate_receipt(self.config, json.loads(old["response_json"]), payload["max_tokens"])
        self.store.reserve(call_id, "game", actor, family, payload["max_tokens"], payload, session, message_cap)
        started = time.monotonic()
        counted = self._send(call_id, lambda: self.transport.count({key: payload[key] for key in COUNTED_FIELDS}))
        if type(counted) is not int or not 0 <= counted <= self.config.max_input_tokens:
            self.store.abandon(call_id, "ContextOverflow")
            raise ContextOverflow(f"Exact input count {counted!r} exceeds {self.config.max_input_tokens}; nothing truncated")
        receipt = normalize_receipt(self._send(call_id, lambda: self.transport.generate(payload)), payload["model"], counted)
        # Keep the complete receipt before checking its shape or applying any action.
        self.store.finish(call_id, receipt, time.monotonic() - started)
        return validate_receipt(self.config, receipt, payload["max_tokens"])

    def _send(self, call_id, send):
        try:
            return send()
        except TransportFailure:
            self.store.abandon(call_id, "transport")
            raise
        except ProviderFailure:
            self.store.fail(call_id, "provider")
            raise

    def act(self, request, view, allowance, family, attempt=1, notice=None, transport_try=1):
        """Return the actor's validated action; `allowance` is None only when replaying a saved call."""
        call_id = try_call_id(attempt_call_id(request["id"], attempt), transport_try)
        old = self.store.call(call_id)
        payload = json.loads(old["request_json"]) if old else claude_action_payload(
            self.config, attempt_turn(request, attempt, notice), view, allowance,
            self.store.daily_balance(family, view["day"]))
        response = self.response(call_id, payload, request["actor"], family, request["session"],
                                 None if allowance is None else allowance.message_remaining)
        reason = response.get("incomplete_details", {}).get("reason")
        if response["status"] == "incomplete" and reason != "max_output_tokens":
            if old is None and reason.startswith("refusal"):
                self.store.fault(REFUSAL_FAULT, f"{call_id}: {reason}")
            raise InvalidResponse(f"The reply ended with {reason}; no action was taken from it")
        return settle_action(self.store, call_id, response, payload["max_tokens"],
                             lambda text: self.count_tokens(text, call_id))
