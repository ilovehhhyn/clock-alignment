"""Responses API with exact counting, no retries, no tools, and a durable receipt.

No API key is read until a caller explicitly constructs OpenAIProvider. Transport is
injectable for offline fault tests; production has a fixed HTTPS origin and rejects
redirects. Native reasoning summaries remain observer-only.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from .allowance import fit_message, judge_log_refund
from .config import canonical, digest
from .prompts import SYSTEM
from .schema import request_schema, validate_action
from .store import IntegrityError, PendingCall


def wire_json(payload):
    """Serialize a request for the wire, keeping key order so schema fields arrive in schema order."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class ProviderFailure(RuntimeError):
    pass


class TransportFailure(ProviderFailure):
    """The server gave no usable answer; a free local call may be sent again."""


class ContextOverflow(ProviderFailure):
    pass


class InvalidResponse(ProviderFailure):
    pass


class AllowanceCutoff(RuntimeError):
    """A response hit the family allocation; the rules make it a neutral pass."""


class CapHit(InvalidResponse):
    """A response ran to the per-call maximum; it is re-prompted but its tokens stay charged."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderFailure("Provider redirect rejected; never forward credentials to another origin")


class HTTPTransport:
    def __init__(self, key, timeout):
        self.key, self.timeout = key, timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, path, payload, call_id):
        if path not in {"/responses", "/responses/input_tokens"}:
            raise ValueError("Unsupported API route")
        request = urllib.request.Request("https://api.openai.com/v1" + path,
            data=wire_json(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                     "X-Client-Request-Id": digest({"call": call_id, "path": path})})
        with self.opener.open(request, timeout=self.timeout) as response:
            body = response.read(16_000_001)
        if len(body) > 16_000_000:
            raise ProviderFailure("Provider response exceeds 16 MB")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ProviderFailure("Provider response must be an object")
        return result


def attempt_call_id(request_id, attempt):
    """Name the durable call for one attempt; the first attempt keeps the original identifier."""
    return f"game:{request_id}" if attempt == 1 else f"game:{request_id}:attempt-{attempt}"


def try_call_id(attempt_id, transport_try):
    """Name one transport try of an attempt; only free local calls ever use a second try."""
    return attempt_id if transport_try == 1 else f"{attempt_id}:try-{transport_try}"


def attempt_turn(request, attempt, notice):
    """Return the turn an attempt shows the actor: the request, plus the Storyteller's private notice on a retry."""
    return request if attempt == 1 else {**request, "attempt": attempt, "storyteller_notice": notice}


def observation_text(request, view, allowance, displayed_balance):
    """Render the one user-turn text every provider sends, byte for byte."""
    return canonical({"authorized_observation": view, "turn": request,
                      "lineage_tokens_at_start_of_night": displayed_balance,
                      "message_tokens_remaining": allowance.message_remaining})


def action_payload(config, request, view, allowance, displayed_balance):
    return {"model": config.model, "instructions": SYSTEM,
            "input": observation_text(request, view, allowance, displayed_balance),
            "reasoning": {"effort": config.effort, "summary": "auto"},
            "text": {"format": {"type": "json_schema", "name": "game_action", "strict": True,
                                "schema": request_schema(request["legal"], speaking=allowance.may_speak)}},
            "max_output_tokens": allowance.output_cap,
            "store": False, "service_tier": "default", "truncation": "disabled",
            "prompt_cache_options": {"ttl": "30m"}}


def hit_output_cap(response):
    details = response.get("incomplete_details")
    return (response.get("status") == "incomplete" and isinstance(details, dict)
            and details.get("reason") == "max_output_tokens")


def settle_action(store, call_id, response, output_cap, count_tokens):
    """Turn a saved receipt into a validated action, cut its message to the spoken cap, and record the refunds.

    A cap hit below the per-call geometry was set by the family allocation and is the rules'
    declared pass; a cap hit at the per-call geometry is a charged failure, so running past the
    maximum never earns free tokens. count_tokens(text) measures spoken tokens with the model's
    own tokenizer and must be deterministic.
    """
    if hit_output_cap(response):
        if output_cap < store.config.max_output_tokens:
            raise AllowanceCutoff(f"call {call_id} reached its {output_cap}-token allocation")
        raise CapHit(f"the response ran past the {output_cap}-token maximum for one answer and was cut off; "
                     "answer in fewer tokens")
    action, spoken = fit_message(parse_action(response), store.call(call_id)["message_cap"], count_tokens)
    store.record_message_tokens(call_id, spoken)
    usage = response["usage"]
    reasoning = usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
    refund = judge_log_refund(raw_text=output_text(response), action=action,
                              output_tokens=usage["output_tokens"], reasoning_tokens=reasoning)
    if store.record_exemption(call_id, refund) and refund == 0 and action["judge_log"]:
        store.fault("judge_log_order", f"{call_id}: judge_log was not the final key; no refund")
    return action


def output_text(response):
    if response.get("status") != "completed":
        raise InvalidResponse("Response is incomplete/capped/refused; no action substituted")
    if response.get("service_tier", "default") != "default":
        raise InvalidResponse("Unexpected processing tier; pricing needs reconciliation")
    pieces = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                raise InvalidResponse("Provider refusal; no gameplay pass substituted")
            if part.get("type") == "output_text":
                pieces.append(part["text"])
    if len(pieces) != 1 or not pieces[0].strip():
        raise InvalidResponse("Expected one nonempty structured response")
    return pieces[0]


def parse_action(response):
    try:
        action = json.loads(output_text(response))
        return validate_action(action)
    except (ValueError, TypeError, KeyError) as error:
        raise InvalidResponse(f"Malformed action: {error}") from error


def validate_receipt(config, raw, output_cap):
    """Return a saved receipt that honours the model, tier, input-count and usage contract."""
    count = raw.get("_local_counted_input_tokens")
    usage_value = raw.get("usage")
    usage = usage_value if isinstance(usage_value, dict) else {}
    details_value = usage.get("output_tokens_details", {})
    details = details_value if isinstance(details_value, dict) else {}
    ni, no = usage.get("input_tokens"), usage.get("output_tokens")
    nr = details.get("reasoning_tokens", 0)
    if (not isinstance(usage_value, dict) or not isinstance(details_value, dict)
            or type(count) is not int or not 0 <= count <= config.max_input_tokens
            or type(ni) is not int or not 0 <= ni <= count
            or type(no) is not int or not 0 <= no <= output_cap
            or type(nr) is not int or not 0 <= nr <= no
            or not isinstance(raw.get("model"), str)
            or not (raw["model"] == config.model or raw["model"].startswith(config.model + "-"))
            or raw.get("service_tier") != "default"):
        raise IntegrityError("Saved provider receipt violates model/tier/count/usage contract")
    return raw


class OpenAIProvider:
    name = "openai"

    def __init__(self, store, transport=None):
        self.store = store
        self.config = store.config
        if transport is None:
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ProviderFailure("Set OPENAI_API_KEY in your shell; do not put it in the repository")
            transport = HTTPTransport(key, self.config.timeout_seconds)
        self.transport = transport

    def _validate_receipt(self, raw, output_cap):
        return validate_receipt(self.config, raw, output_cap)

    def count_tokens(self, text, call_id):
        """Count `text` with the model's tokenizer through the input-count route; a few framing tokens are included."""
        try:
            counted = self.transport.post("/responses/input_tokens", {"model": self.config.model, "input": text}, call_id)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise ProviderFailure(f"{type(error).__name__}: could not count spoken tokens for {call_id}") from error
        count = counted.get("input_tokens")
        if type(count) is not int or count < 0:
            raise ProviderFailure(f"input-count route returned {count!r} for spoken text of {call_id}")
        return count

    def response(self, call_id, scope, actor, family, payload, session=None, message_cap=None):
        # Freeze exactly the request that is counted, sent and saved. No conversations,
        # previous_response_id, tools, or caller-specified providers can broaden access.
        payload = json.loads(wire_json(payload))
        forbidden = {"tools", "previous_response_id", "conversation", "background"} & payload.keys()
        if (forbidden or payload.get("model") != self.config.model or payload.get("store") is not False
                or payload.get("service_tier") != "default" or payload.get("truncation") != "disabled"
                or payload.get("prompt_cache_options") != {"ttl": "30m"}):
            raise ValueError("Request violates the frozen provider/security/pricing contract")
        old = self.store.call(call_id)
        if old:
            if old["request_json"] != canonical(payload):
                raise IntegrityError("Replay payload differs from saved request")
            if old["status"] != "completed":
                raise PendingCall("Existing request may have been billed. Stop for reconciliation; no blind retry.")
            return self._validate_receipt(json.loads(old["response_json"]), payload["max_output_tokens"])
        self.store.reserve(call_id, scope, actor, family, payload["max_output_tokens"], payload, session, message_cap)
        started = time.monotonic()
        try:
            count_body = {k: payload[k] for k in ("model", "instructions", "input", "reasoning", "text") if k in payload}
            counted = self.transport.post("/responses/input_tokens", count_body, call_id)
            count = counted.get("input_tokens")
            if type(count) is not int or not 0 <= count <= self.config.max_input_tokens:
                raise ContextOverflow("Exact input count exceeds frozen context budget; nothing silently truncated")
            raw = self.transport.post("/responses", payload, call_id)
            raw["_local_counted_input_tokens"] = count
            # Keep the complete receipt before checking shape or applying any action.
            self.store.finish(call_id, raw, time.monotonic() - started)
            return self._validate_receipt(raw, payload["max_output_tokens"])
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError,
                json.JSONDecodeError, ProviderFailure, IntegrityError) as error:
            self.store.fail(call_id, type(error).__name__)
            raise ProviderFailure(f"{type(error).__name__}: call {call_id} stopped; receipt/reservation preserved") from error

    transport_retry_safe = False  # a timed-out paid call may already be billed

    def act(self, request, view, allowance, family, attempt=1, notice=None, transport_try=1):
        """Return the actor's validated action; `allowance` is None only when replaying a saved call."""
        if transport_try != 1:
            raise ValueError("A paid call is never sent twice; transport tries are local-only")
        call_id = attempt_call_id(request["id"], attempt)
        old = self.store.call(call_id)
        # A saved receipt already consumed output allowance. Reconstructing its
        # pre-call remaining allowance would change the replay payload.
        payload = json.loads(old["request_json"]) if old else action_payload(
            self.config, attempt_turn(request, attempt, notice), view, allowance,
            self.store.daily_balance(family, view["day"]))
        response = self.response(call_id, "game", request["actor"], family, payload, request["session"],
                                 None if allowance is None else allowance.message_remaining)
        return settle_action(self.store, call_id, response, payload["max_output_tokens"],
                             lambda text: self.count_tokens(text, call_id))
