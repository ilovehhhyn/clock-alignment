"""The vLLM path sends the OpenAI path's exact prompt text and keeps every limit and receipt rule."""
from copy import deepcopy
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

from clock_alignment.allowance import MIN_SPOKEN_TOKENS, CallAllowance, decide_allowance, fit_message, fit_text
from clock_alignment.config import Config
from clock_alignment.engine import Engine
from clock_alignment.local_provider import API_KEY_ENV, LocalProvider, parse_endpoint
from clock_alignment.prompts import SYSTEM
from clock_alignment.provider import AllowanceCutoff, ProviderFailure, action_payload, attempt_call_id, try_call_id
from clock_alignment.runner import play_turn
from clock_alignment.schema import empty_action, request_schema
from clock_alignment.store import Store

QWEN = "Qwen/Qwen3-4B-Instruct-2507"
HYBRID = "Qwen/Qwen3-8B"


def completion(text, finish="stop", completion_tokens=200):
    return {"id": "chatcmpl-fixture", "model": QWEN, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": completion_tokens, "total_tokens": 1100}}


class LocalTransport:
    """Record every request; answer /tokenize with a fixed count and chats from a list."""

    def __init__(self, answers, count=900):
        self.answers, self.count, self.calls = list(answers), count, []

    def post(self, path, payload, call_id):
        self.calls.append((path, deepcopy(payload), call_id))
        if path == "/tokenize":
            count = len(payload["prompt"].split()) if "prompt" in payload else self.count
            return {"count": count, "max_model_len": 52_096, "tokens": []}
        if not self.answers:
            raise AssertionError(f"unexpected chat call {call_id}")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return deepcopy(answer)


class LocalProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(model=QWEN, effort="none", max_days=2)
        self.engine = Engine(self.config.to_dict(), seed=91)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.store.initialize(self.config, 91, self.engine, "vllm")
        self.request = self.engine.next_request()
        self.actor = self.request["actor"]
        self.family = self.engine.family(self.actor)
        self.view = self.engine.view(self.actor)
        self.store.save_intent(self.request, self.view, self.family)

    def allowance(self):
        return decide_allowance(self.config, self.store.remaining(self.family),
                                self.store.message_remaining(self.actor, self.request["session"]))

    def test_chat_request_carries_the_openai_prompt_text_schema_and_sampling(self):
        allowance = self.allowance()
        transport = LocalTransport([completion(json.dumps(empty_action()))])

        LocalProvider(self.store, transport=transport).act(self.request, self.view, allowance, self.family)

        (count_path, count_body, _), (chat_path, chat, _) = transport.calls
        balance = self.store.daily_balance(self.family, self.view["day"])
        openai_input = action_payload(Config(), self.request, self.view, allowance, balance)["input"]
        self.assertEqual((count_path, chat_path), ("/tokenize", "/v1/chat/completions"))
        self.assertEqual(chat["messages"], [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": openai_input}])
        self.assertEqual(count_body["messages"], chat["messages"])
        self.assertTrue(count_body["add_generation_prompt"])
        self.assertEqual(chat["response_format"]["json_schema"]["schema"], request_schema(self.request["legal"]))
        self.assertEqual(chat["max_tokens"], allowance.output_cap)
        self.assertEqual({k: chat[k] for k in ("temperature", "top_p", "top_k", "min_p")},
                         dict(self.config.profile.sampling))

    def test_a_hybrid_thinking_model_is_asked_with_thinking_off_in_the_count_and_the_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(model=HYBRID, effort="none", max_days=2)
            engine = Engine(config.to_dict(), seed=91)
            store = Store(directory)
            self.addCleanup(store.close)
            store.initialize(config, 91, engine, "vllm")
            request = engine.next_request()
            actor, family = request["actor"], engine.family(request["actor"])
            allowance = decide_allowance(config, store.remaining(family), store.message_remaining(actor, request["session"]))
            reply = completion(json.dumps(empty_action()))
            reply["model"] = HYBRID
            transport = LocalTransport([reply])

            LocalProvider(store, transport=transport).act(request, engine.view(actor), allowance, family)

            (_, count_body, _), (_, chat, _) = transport.calls
            self.assertEqual(count_body["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(chat["chat_template_kwargs"], {"enable_thinking": False})

    def test_the_instruct_model_sends_no_chat_template_switches(self):
        transport = LocalTransport([completion(json.dumps(empty_action()))])

        LocalProvider(self.store, transport=transport).act(self.request, self.view, self.allowance(), self.family)

        for path, body, _ in transport.calls:
            with self.subTest(path=path):
                self.assertNotIn("chat_template_kwargs", body)

    def test_length_finish_at_the_family_remainder_is_a_declared_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(model=QWEN, effort="none", max_days=2, family_tokens=1024)
            engine = Engine(config.to_dict(), seed=91)
            store = Store(directory)
            self.addCleanup(store.close)
            store.initialize(config, 91, engine, "vllm")
            request = engine.next_request()
            actor, family = request["actor"], engine.family(request["actor"])
            allowance = decide_allowance(config, store.remaining(family), store.message_remaining(actor, request["session"]))
            transport = LocalTransport([completion('{"whisper_to": null', finish="length", completion_tokens=1024)])

            with self.assertRaises(AllowanceCutoff):
                LocalProvider(store, transport=transport).act(request, engine.view(actor), allowance, family)

            self.assertEqual((allowance.output_cap, store.remaining(family)), (1024, 0))

    def test_input_over_the_context_limit_never_reaches_generation(self):
        transport = LocalTransport([], count=self.config.max_input_tokens + 1)

        with self.assertRaises(ProviderFailure):
            LocalProvider(self.store, transport=transport).act(self.request, self.view, self.allowance(), self.family)

        self.assertEqual([path for path, _, _ in transport.calls], ["/tokenize"])
        self.assertEqual(self.store.call(attempt_call_id(self.request["id"], 1))["status"], "released")
        self.assertEqual(self.store.remaining(self.family), self.config.family_tokens)

    def test_a_dropped_local_call_is_resent_within_the_same_player_attempt(self):
        transport = LocalTransport([socket.timeout("server busy"), completion(json.dumps(empty_action()))])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=transport), self.request)

        decision = self.store.db.execute("SELECT call_id, mode FROM decisions").fetchone()
        self.assertEqual(tuple(decision), (try_call_id(attempt_call_id(self.request["id"], 1), 2), "model"))
        faults = [r[0] for r in self.store.db.execute("SELECT category FROM faults")]
        self.assertEqual(faults, ["transport_retry"])

    def test_endpoint_must_be_plain_http_on_the_loopback_address(self):
        self.assertEqual(parse_endpoint("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
        for url in ("https://127.0.0.1:8000", "http://localhost:8000", "http://0.0.0.0:8000",
                    "http://10.0.0.5:8000", "http://127.0.0.1", "http://127.0.0.1:8000/v1",
                    "http://user:pw@127.0.0.1:8000"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "127.0.0.1"):
                parse_endpoint(url)

    def test_missing_api_key_is_rejected_by_name_before_any_request(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderFailure, API_KEY_ENV):
                LocalProvider(self.store, endpoint="http://127.0.0.1:8000")

    def test_local_model_requires_a_day_horizon_and_no_reasoning_effort(self):
        for kwargs in ({"model": QWEN, "effort": "none"}, {"model": QWEN, "effort": "medium", "max_days": 2},
                       {"model": "gpt-6.1-sol", "effort": "none"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)


def words(text):
    """The fake tokenizer: one token per whitespace-separated word."""
    return len(text.split())


class CountingTransport(LocalTransport):
    """Answer /tokenize for a prompt string with its word count, so message cuts are checkable by hand."""

    def post(self, path, payload, call_id):
        if path == "/tokenize" and "prompt" in payload:
            self.calls.append((path, deepcopy(payload), call_id))
            return {"count": words(payload["prompt"]), "max_model_len": 52_096, "tokens": []}
        return super().post(path, payload, call_id)


def discussion_request(engine):
    """Apply neutral actions until the first public discussion request of day 1."""
    while (request := engine.next_request())["kind"] != "discussion":
        engine.apply(request["id"], engine.pass_action(request))
    return request


class SpokenTokenCapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(model=QWEN, effort="none", max_days=2, message_tokens=8)
        self.engine = Engine(self.config.to_dict(), seed=91)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.store.initialize(self.config, 91, self.engine, "vllm")
        self.request = discussion_request(self.engine)
        self.actor = self.request["actor"]

    def spoken(self, text):
        action = empty_action()
        action.update(say=text, audience="public")
        return completion(json.dumps(action))

    def test_a_statement_over_the_spoken_cap_keeps_its_first_words_and_is_marked(self):
        text = "one two three four five six seven eight nine ten eleven twelve"
        transport = CountingTransport([self.spoken(text)])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=transport), self.request)

        decision = json.loads(self.store.db.execute("SELECT action_json FROM decisions").fetchone()[0])
        self.assertEqual(decision["say"], "one two three four five six [cut off]")
        self.assertEqual(self.store.message_remaining(self.actor, self.request["session"]), 0)
        self.assertEqual(self.store.call(attempt_call_id(self.request["id"], 1))["message_tokens"], 8)

    def test_a_statement_within_the_spoken_cap_is_delivered_whole_and_charged_exactly(self):
        transport = CountingTransport([self.spoken("five words fit the cap")])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=transport), self.request)

        decision = json.loads(self.store.db.execute("SELECT action_json FROM decisions").fetchone()[0])
        self.assertEqual(decision["say"], "five words fit the cap")
        self.assertEqual(self.store.message_remaining(self.actor, self.request["session"]), 3)

    def test_with_too_few_spoken_tokens_left_the_schema_forbids_speaking_but_not_choosing(self):
        # Witness: 1 token left cannot carry a word plus the mark; MIN_SPOKEN_TOKENS closes the fields.
        family = self.engine.family(self.actor)
        self.store.reserve("earlier", "game", self.actor, family, 256, {}, self.request["session"], 8)
        self.store.finish("earlier", {"usage": {"input_tokens": 10, "output_tokens": 20}}, 0.1)
        self.store.record_message_tokens("earlier", 7)
        transport = CountingTransport([completion(json.dumps(empty_action()))])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=transport), self.request)

        chat = next(payload for path, payload, _ in transport.calls if path == "/v1/chat/completions")
        properties = chat["response_format"]["json_schema"]["schema"]["properties"]
        self.assertEqual(properties["say"], {"type": "string", "enum": [""]})
        self.assertEqual(properties["audience"], {"type": "null"})
        self.assertEqual(properties["ready_to_vote"], {"type": "boolean"})
        self.assertEqual(json.loads(chat["messages"][1]["content"])["message_tokens_remaining"], 1)

    def test_exactly_the_minimum_spoken_tokens_still_allows_a_message(self):
        self.assertTrue(CallAllowance(output_cap=512, limit="call", message_remaining=MIN_SPOKEN_TOKENS).may_speak)
        self.assertFalse(CallAllowance(output_cap=512, limit="call", message_remaining=MIN_SPOKEN_TOKENS - 1).may_speak)

    def test_thinking_is_limited_only_by_the_family_allocation(self):
        transport = CountingTransport([completion(json.dumps(empty_action()))])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=transport), self.request)

        chat = next(payload for path, payload, _ in transport.calls if path == "/v1/chat/completions")
        self.assertEqual(chat["max_tokens"], self.config.max_output_tokens)


class FitMessageTests(unittest.TestCase):
    def test_messages_are_cut_word_by_word_to_the_cap(self):
        for text, cap, kept, tokens in (("a b c", None, "a b c", 3),
                                        ("a b c", 3, "a b c", 3),
                                        ("a b c d", 3, "a [cut off]", 3),
                                        ("a b c d e f", 4, "a b [cut off]", 4),
                                        ("a b", 1, "", 0),
                                        ("", 0, "", 0)):
            with self.subTest(text=text, cap=cap):
                self.assertEqual(fit_text(text, cap, words, limit=1000), (kept, tokens))

    def test_an_emptied_whisper_drops_its_recipient_and_audience(self):
        action = empty_action()
        action.update(whisper_to="P1", audience="unknown", whisper="far too long")

        fitted, tokens = fit_message(action, 1, words)

        self.assertEqual((fitted["whisper"], fitted["whisper_to"], fitted["audience"], tokens), ("", None, None, 0))


if __name__ == "__main__":
    unittest.main()
