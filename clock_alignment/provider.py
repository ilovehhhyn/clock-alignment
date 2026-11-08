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

from .config import canonical, digest
from .prompts import SYSTEM
from .schema import ACTION_SCHEMA, validate_action
from .store import IntegrityError, PendingCall


class ProviderFailure(RuntimeError):
    pass


class ContextOverflow(ProviderFailure):
    pass


class InvalidResponse(ProviderFailure):
    pass


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
            data=canonical(payload).encode(), method="POST",
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


def action_payload(config, request, view, remaining, displayed_balance=None):
    return {"model": config.model, "instructions": SYSTEM,
            "input": canonical({"authorized_observation": view, "turn": request,
                                "lineage_tokens_at_start_of_night": remaining if displayed_balance is None else displayed_balance}),
            "reasoning": {"effort": config.effort, "summary": "auto"},
            "text": {"format": {"type": "json_schema", "name": "game_action", "strict": True,
                                "schema": ACTION_SCHEMA}},
            "max_output_tokens": min(config.max_output_tokens, remaining),
            "store": False, "service_tier": "default", "truncation": "disabled",
            "prompt_cache_options": {"ttl": "30m"}}


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
        count = raw.get("_local_counted_input_tokens")
        usage_value = raw.get("usage")
        usage = usage_value if isinstance(usage_value, dict) else {}
        details_value = usage.get("output_tokens_details", {})
        details = details_value if isinstance(details_value, dict) else {}
        ni, no = usage.get("input_tokens"), usage.get("output_tokens")
        nr = details.get("reasoning_tokens", 0)
        if (not isinstance(usage_value, dict) or not isinstance(details_value, dict)
                or type(count) is not int or not 0 <= count <= self.config.max_input_tokens
                or type(ni) is not int or not 0 <= ni <= count
                or type(no) is not int or not 0 <= no <= output_cap
                or type(nr) is not int or not 0 <= nr <= no
                or not isinstance(raw.get("model"), str)
                or not (raw["model"] == self.config.model or raw["model"].startswith(self.config.model + "-"))
                or raw.get("service_tier") != "default"):
            raise IntegrityError("Saved provider receipt violates model/tier/count/usage contract")
        return raw

    def response(self, call_id, scope, actor, family, payload):
        # Freeze exactly the request that is counted, sent and saved. No conversations,
        # previous_response_id, tools, or caller-specified providers can broaden access.
        payload = json.loads(canonical(payload))
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
        self.store.reserve(call_id, scope, actor, family, payload["max_output_tokens"], payload)
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

    def act(self, request, view, remaining, family):
        old = self.store.call("game:" + request["id"])
        # A saved receipt already consumed output allowance. Reconstructing its
        # pre-call remaining allowance would change the replay payload.
        payload = json.loads(old["request_json"]) if old else action_payload(
            self.config, request, view, remaining, self.store.daily_balance(family, view["day"]))
        response = self.response("game:" + request["id"], "game", request["actor"], family, payload)
        return parse_action(response)
