"""Serve gameplay from a loopback vLLM server with the same prompt text, receipts and limits.

The system prompt, observation text, action schema and allowances are the OpenAI path's,
byte for byte. Only the wire format differs: chat completions plus exact /tokenize counts.
vLLM replies are normalized into the OpenAI receipt shape before any check or settlement.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import canonical, digest
from .prompts import SYSTEM
from .provider import (ContextOverflow, InvalidResponse, NoRedirect, ProviderFailure, TransportFailure,
                       attempt_call_id, attempt_turn, observation_text, settle_action, try_call_id,
                       validate_receipt, wire_json)
from .schema import request_schema
from .store import IntegrityError, PendingCall

API_KEY_ENV = "LOCAL_LLM_API_KEY"
# The server must share the job's node; any other host would carry private views off it.
LOOPBACK_HOST = "127.0.0.1"
ROUTES = ("/tokenize", "/v1/chat/completions")
MAX_RESPONSE_BYTES = 16_000_000
FINISH_REASONS = ("stop", "length")
TRANSPORT_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, OSError, json.JSONDecodeError, ProviderFailure)


def parse_endpoint(url):
    """Return `http://127.0.0.1:<port>` for a loopback vLLM server, or fail."""
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != LOOPBACK_HOST or parts.port is None
            or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username):
        raise ValueError(f"local endpoint must be http://{LOOPBACK_HOST}:<port>, got {url!r}")
    return f"http://{LOOPBACK_HOST}:{parts.port}"


class LocalTransport:
    def __init__(self, endpoint, key, timeout):
        self.endpoint, self.key, self.timeout = parse_endpoint(endpoint), key, timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, path, payload, call_id):
        if path not in ROUTES:
            raise ValueError(f"Unsupported local route {path!r}")
        request = urllib.request.Request(self.endpoint + path, data=wire_json(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                     "X-Request-Id": digest({"call": call_id, "path": path})})
        with self.opener.open(request, timeout=self.timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderFailure(f"Local response exceeds {MAX_RESPONSE_BYTES} bytes")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ProviderFailure("Local response must be an object")
        return result


def request_seed(call_id):
    """Derive a per-call sampling seed so a rerun of the same request draws the same sample."""
    return int(digest({"seed": call_id})[:8], 16)


def template_switches(config):
    """Return the profile's chat-template switches as a payload fragment, empty when it has none."""
    switches = dict(config.profile.chat_template_kwargs)
    return {"chat_template_kwargs": switches} if switches else {}


def local_action_payload(config, request, view, allowance, displayed_balance, call_id):
    return {"model": config.model,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": observation_text(request, view, allowance, displayed_balance)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "game_action", "schema": request_schema(request["legal"], speaking=allowance.may_speak),
                "strict": True}},
            "max_tokens": allowance.output_cap, "n": 1, "stream": False,
            "seed": request_seed(call_id), **dict(config.profile.sampling), **template_switches(config)}


def normalize_receipt(raw, model, counted):
    """Map one vLLM chat completion onto the OpenAI receipt shape; malformed usage stays unsettled."""
    choices = raw.get("choices")
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    finish = choice.get("finish_reason")
    text = message.get("content")
    receipt = {"id": raw.get("id"), "model": raw.get("model"), "service_tier": "default",
               "status": "completed" if finish == "stop" else "incomplete",
               "usage": {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                         "output_tokens_details": {"reasoning_tokens": 0}},
               "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
               "provider_response": raw, "_local_counted_input_tokens": counted, "_requested_model": model}
    if finish == "length":
        receipt["incomplete_details"] = {"reason": "max_output_tokens"}
    elif finish not in FINISH_REASONS or not isinstance(text, str):
        receipt["incomplete_details"] = {"reason": f"unexpected finish {finish!r}"}
    return receipt


def require_payload_contract(config, payload):
    switches = template_switches(config)
    expected_keys = {"model", "messages", "response_format", "max_tokens", "n", "stream", "seed",
                     *dict(config.profile.sampling), *switches}
    sampling_ok = all(payload.get(key) == value for key, value in config.profile.sampling)
    switches_ok = all(payload.get(key) == value for key, value in switches.items())
    if (set(payload) != expected_keys or payload["model"] != config.model or payload["stream"] is not False
            or payload["n"] != 1 or not sampling_ok or not switches_ok
            or payload["messages"][0] != {"role": "system", "content": SYSTEM}
            or payload["response_format"]["json_schema"]["strict"] is not True):
        raise ValueError("Request violates the frozen local provider contract")


class LocalProvider:
    name = "vllm"
    transport_retry_safe = True  # a local call costs no money, so a failed attempt can be retried

    def __init__(self, store, endpoint=None, transport=None):
        self.store = store
        self.config = store.config
        if self.config.profile.provider != "vllm":
            raise ValueError(f"Model {self.config.model!r} is not served by vLLM")
        if transport is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise ProviderFailure(f"Set {API_KEY_ENV} to the vLLM server's API key; never pass it as an argument")
            if endpoint is None:
                raise ValueError("Local runs require --endpoint http://127.0.0.1:<port>")
            transport = LocalTransport(endpoint, key, self.config.timeout_seconds)
        self.transport = transport

    def count_tokens(self, text, call_id):
        """Count `text` exactly with the served model's tokenizer, without special tokens.

        The call's receipt is already settled, so a failure here stops the run instead of
        releasing the call; the resumed run counts the saved receipt again.
        """
        try:
            counted = self.transport.post("/tokenize", {"model": self.config.model, "prompt": text,
                                                        "add_special_tokens": False}, call_id).get("count")
        except TRANSPORT_ERRORS as error:
            raise ProviderFailure(f"{type(error).__name__}: could not count the spoken tokens of settled call "
                                  f"{call_id}; resume the run to count its saved receipt") from error
        if type(counted) is not int or counted < 0:
            raise ProviderFailure(f"/tokenize returned {counted!r} for spoken text of {call_id}")
        return counted

    def require_served_context(self, reply):
        """Fail once, before any generation, if the server's context cannot hold a full request."""
        needed = self.config.max_input_tokens + self.config.max_output_tokens
        served = reply.get("max_model_len")
        if type(served) is not int or served < needed:
            raise ProviderFailure(f"the server's max_model_len {served!r} is below max_input_tokens + "
                                  f"max_output_tokens = {needed}; start vLLM with --max-model-len {needed} or more")

    def response(self, call_id, payload, actor, family, session, message_cap):
        payload = json.loads(wire_json(payload))
        require_payload_contract(self.config, payload)
        old = self.store.call(call_id)
        if old:
            if old["request_json"] != canonical(payload):
                raise IntegrityError("Replay payload differs from saved request")
            if old["status"] != "completed":
                raise PendingCall("A failed local try must be released and skipped, never replayed")
            return validate_receipt(self.config, json.loads(old["response_json"]), payload["max_tokens"])
        self.store.reserve(call_id, "game", actor, family, payload["max_tokens"], payload, session, message_cap)
        started = time.monotonic()
        # The count must see the same rendered prompt as the generation, switches included.
        reply = self._post(call_id, "/tokenize", {"model": payload["model"], "messages": payload["messages"],
                                                  "add_generation_prompt": True, **template_switches(self.config)})
        try:
            self.require_served_context(reply)
        except ProviderFailure:
            self.store.release(call_id, "served context too small")
            raise
        counted = reply.get("count")
        if type(counted) is not int or not 0 <= counted <= self.config.max_input_tokens:
            self.store.release(call_id, "ContextOverflow")
            raise ContextOverflow(f"Exact input count {counted!r} exceeds {self.config.max_input_tokens}; nothing truncated")
        receipt = normalize_receipt(self._post(call_id, "/v1/chat/completions", payload), payload["model"], counted)
        try:
            self.store.finish(call_id, receipt, time.monotonic() - started)
        except IntegrityError as error:
            self.store.release(call_id, "malformed usage")
            raise TransportFailure(f"call {call_id}: the server's usage report was unusable") from error
        # A settled receipt that breaks the contract stops the run identically on replay.
        return validate_receipt(self.config, receipt, payload["max_tokens"])

    def _post(self, call_id, path, body):
        try:
            return self.transport.post(path, body, call_id)
        except TRANSPORT_ERRORS as error:
            # Local calls cost nothing and the actor received nothing, so the hold is released.
            self.store.release(call_id, type(error).__name__)
            raise TransportFailure(f"{type(error).__name__}: call {call_id} got no usable answer") from error

    def act(self, request, view, allowance, family, attempt=1, notice=None, transport_try=1):
        """Return the actor's validated action; `allowance` is None only when replaying a saved call."""
        attempt_id = attempt_call_id(request["id"], attempt)
        call_id = try_call_id(attempt_id, transport_try)
        old = self.store.call(call_id)
        # The seed follows the attempt, so a resent try asks for the same sample.
        payload = json.loads(old["request_json"]) if old else local_action_payload(
            self.config, attempt_turn(request, attempt, notice), view, allowance,
            self.store.daily_balance(family, view["day"]), attempt_id)
        response = self.response(call_id, payload, request["actor"], family, request["session"],
                                 None if allowance is None else allowance.message_remaining)
        if response["status"] == "incomplete" and response["incomplete_details"]["reason"] != "max_output_tokens":
            raise InvalidResponse(f"Local response ended with {response['incomplete_details']['reason']}")
        return settle_action(self.store, call_id, response, payload["max_tokens"],
                             lambda text: self.count_tokens(text, call_id))
