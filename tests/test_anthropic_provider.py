"""The Claude path sends the shared prompt text in cacheable blocks, charges cache-aware prices and keeps every limit."""
from copy import deepcopy
import json
import os
import tempfile
import unittest
from unittest import mock

from clock_alignment.allowance import decide_allowance
from clock_alignment.anthropic_provider import (API_KEY_ENV, CACHE_CONTROL, CHUNK_CHARS, REFUSAL_FAULT,
                                                UNSUPPORTED_SCHEMA_KEYS, AnthropicProvider, claude_schema,
                                                joined_text, observation_blocks, stable_prefix_length)
from clock_alignment.config import NANODOLLARS, Config
from clock_alignment.engine import Engine
from clock_alignment.prompts import SYSTEM
from clock_alignment.provider import (AllowanceCutoff, CapHit, InvalidResponse, ProviderFailure, TransportFailure,
                                      action_payload)
from clock_alignment.runner import play_turn
from clock_alignment.schema import empty_action, request_schema
from clock_alignment.store import BudgetLimit, Store

OPUS = "claude-opus-5-5"
COUNTED = 2_000


def reply(text, stop="end_turn", fresh=900, read=0, written=0, output=200, category=None):
    raw = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": OPUS,
           "content": [{"type": "thinking", "thinking": "a summary", "signature": "sig"}, {"type": "text", "text": text}],
           "stop_reason": stop, "usage": {"input_tokens": fresh, "cache_read_input_tokens": read,
                                          "cache_creation_input_tokens": written, "output_tokens": output}}
    if stop == "refusal":
        raw["stop_details"] = {"type": "refusal", "category": category, "explanation": "declined"}
    return raw


class FakeTransport:
    """Record counts and generations; answer generations from a list, raising any exception found there."""

    def __init__(self, answers, counted=COUNTED):
        self.answers, self.counted, self.counts, self.generations = list(answers), counted, [], []

    def count(self, body):
        self.counts.append(deepcopy(body))
        if "system" not in body:  # spoken text: one token per word
            return len(body["messages"][0]["content"].split())
        return self.counted

    def generate(self, payload):
        self.generations.append(deepcopy(payload))
        if not self.answers:
            raise AssertionError("unexpected generation")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return deepcopy(answer)


class ClaudeProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(model=OPUS, effort="xhigh")
        self.engine = Engine(self.config.to_dict(), seed=91)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.store.initialize(self.config, 91, self.engine, "anthropic")
        self.request = self.engine.next_request()
        self.actor = self.request["actor"]
        self.family = self.engine.family(self.actor)
        self.view = self.engine.view(self.actor)
        self.store.save_intent(self.request, self.view, self.family)

    def allowance(self):
        return decide_allowance(self.config, self.store.remaining(self.family),
                                self.store.message_remaining(self.actor, self.request["session"]))

    def act(self, answers, allowance=None, transport_try=1):
        transport = FakeTransport(answers)
        provider = AnthropicProvider(self.store, transport=transport)
        result = provider.act(self.request, self.view, allowance or self.allowance(), self.family,
                              transport_try=transport_try)
        return result, transport

    def test_the_request_joins_to_the_shared_text_and_runs_at_xhigh_with_summarized_thinking(self):
        allowance = self.allowance()

        _, transport = self.act([reply(json.dumps(empty_action()))], allowance)

        (payload,) = transport.generations
        balance = self.store.daily_balance(self.family, self.view["day"])
        shared = action_payload(Config(), self.request, self.view, allowance, balance)["input"]
        self.assertEqual(joined_text(payload), shared)
        self.assertEqual(payload["system"], [{"type": "text", "text": SYSTEM, "cache_control": CACHE_CONTROL}])
        self.assertEqual(payload["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(payload["output_config"]["effort"], "xhigh")
        self.assertEqual(payload["max_tokens"], allowance.output_cap)
        self.assertEqual(transport.counts[0], {key: payload[key] for key in
                                               ("model", "system", "messages", "thinking", "output_config")})

    def test_the_schema_drops_only_the_keywords_claude_rejects(self):
        schema = request_schema(self.request["legal"], speaking=True)

        stripped = claude_schema(schema)

        text = json.dumps(stripped)
        self.assertTrue(all(f'"{key}"' not in text for key in UNSUPPORTED_SCHEMA_KEYS))
        self.assertEqual(stripped["required"], schema["required"])
        self.assertEqual(set(stripped["properties"]), set(schema["properties"]))
        self.assertIn('"maxLength"', json.dumps(schema))  # the witness: the shared schema does carry them

    def test_cache_reads_and_writes_are_charged_at_their_own_prices(self):
        self.act([reply(json.dumps(empty_action()), fresh=300, read=1_200, written=400, output=150)])

        row = self.store.call(f"game:{self.request['id']}")
        profile = self.config.profile
        expected = (300 * profile.input_nano_per_token + 1_200 * profile.cache_read_nano_per_token
                    + 400 * profile.cache_write_nano_per_token + 150 * profile.output_nano_per_token)
        self.assertEqual(row["charge_nano"], expected)
        self.assertEqual(row["input_tokens"], 1_900)

    def test_the_reservation_covers_a_prompt_written_wholly_to_the_cache(self):
        provider_output_cap = 1_024
        self.store.reserve("game:hold", "game", self.actor, self.family, provider_output_cap, {}, self.request["session"],
                           self.store.message_remaining(self.actor, self.request["session"]))

        row = self.store.call("game:hold")

        profile = self.config.profile
        self.assertEqual(row["reservation_nano"], self.config.max_input_tokens * profile.cache_write_nano_per_token
                         + provider_output_cap * profile.output_nano_per_token)

    def test_a_refusal_is_recorded_and_returned_for_a_private_reprompt(self):
        with self.assertRaises(InvalidResponse) as caught:
            self.act([reply("", stop="refusal", category="general_harms")])

        self.assertIn("refusal (general_harms)", str(caught.exception))
        faults = [row["category"] for row in self.store.db.execute("SELECT category FROM faults")]
        self.assertEqual(faults, [REFUSAL_FAULT])
        self.assertEqual(self.store.call(f"game:{self.request['id']}")["status"], "completed")

    def test_running_out_at_the_family_remainder_is_a_pass_and_at_the_call_cap_a_charged_retry(self):
        small = self.allowance().__class__(**{**self.allowance().__dict__, "output_cap": 600})
        with self.assertRaises(AllowanceCutoff):
            self.act([reply("{", stop="max_tokens", output=600)], small)

        with self.assertRaises(CapHit):
            self.act([reply("{", stop="max_tokens", output=self.config.max_output_tokens)], transport_try=2)

    def test_a_failed_try_keeps_its_dollar_hold_and_charges_no_tokens(self):
        remaining_before = self.store.remaining(self.family)

        with self.assertRaises(TransportFailure):
            self.act([TransportFailure("HTTP 529 after 6 retries")])

        row = self.store.call(f"game:{self.request['id']}")
        self.assertEqual(row["status"], "released")
        self.assertEqual(row["charge_nano"], row["reservation_nano"])
        self.assertGreater(row["charge_nano"], 0)
        self.assertEqual(self.store.remaining(self.family), remaining_before)

    def test_an_interrupted_try_is_abandoned_once_and_repeating_it_is_harmless(self):
        call_id = f"game:{self.request['id']}"
        self.store.reserve(call_id, "game", self.actor, self.family, 1_024, {}, self.request["session"],
                           self.store.message_remaining(self.actor, self.request["session"]))
        provider = AnthropicProvider(self.store, transport=FakeTransport([]))

        provider.release_interrupted(call_id)
        provider.release_interrupted(call_id)

        self.assertEqual(self.store.call(call_id)["status"], "released")
        self.assertEqual(self.store.call(call_id)["error"], "interrupted")

    def test_a_settled_call_cannot_be_abandoned(self):
        self.act([reply(json.dumps(empty_action()))])

        with self.assertRaises(Exception) as caught:
            self.store.abandon(f"game:{self.request['id']}", "interrupted")

        self.assertIn("Only an unsettled gameplay call can be abandoned", str(caught.exception))

    def test_a_prompt_over_the_input_cap_never_reaches_generation(self):
        transport = FakeTransport([], counted=self.config.max_input_tokens + 1)
        provider = AnthropicProvider(self.store, transport=transport)

        with self.assertRaises(ProviderFailure):
            provider.act(self.request, self.view, self.allowance(), self.family)

        self.assertEqual(transport.generations, [])

    def test_a_hard_api_error_stops_the_run_instead_of_retrying(self):
        with self.assertRaises(ProviderFailure) as caught:
            self.act([ProviderFailure("HTTP 400: schema rejected")])

        self.assertNotIsInstance(caught.exception, TransportFailure)
        self.assertEqual(self.store.call(f"game:{self.request['id']}")["status"], "unknown")

    def test_the_dollar_ceiling_stops_a_call_before_it_is_sent(self):
        # Invariant: one call's hold (150,000 input tokens at the $8/M cache-write price) exceeds the $1 gameplay cap.
        config = Config(model=OPUS, effort="xhigh", max_input_tokens=150_000,
                        dollar_limit=2, game_dollar_limit=1, analysis_dollar_limit=1)
        with tempfile.TemporaryDirectory() as root:
            engine = Engine(config.to_dict(), seed=91)
            store = Store(root)
            self.addCleanup(store.close)
            store.initialize(config, 91, engine, "anthropic")
            request = engine.next_request()
            actor = request["actor"]
            store.save_intent(request, engine.view(actor), engine.family(actor))
            transport = FakeTransport([reply(json.dumps(empty_action()))])
            allowance = decide_allowance(config, store.remaining(engine.family(actor)),
                                         store.message_remaining(actor, request["session"]))

            with self.assertRaises(BudgetLimit):
                AnthropicProvider(store, transport=transport).act(request, engine.view(actor), allowance,
                                                                  engine.family(actor))

            self.assertEqual(transport.generations, [])
            self.assertGreater(config.max_input_tokens * config.profile.cache_write_nano_per_token,
                               config.game_dollar_limit * NANODOLLARS)

    def test_missing_key_is_rejected_by_name_before_any_request(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderFailure) as caught:
                AnthropicProvider(self.store)

        self.assertIn(API_KEY_ENV, str(caught.exception))

    def test_play_turn_commits_claude_answers_through_the_runner(self):
        provider = AnthropicProvider(self.store, transport=FakeTransport([reply(json.dumps(empty_action()))] * 6))
        request = self.request

        for _ in range(3):
            play_turn(self.engine, self.store, provider, request)
            request = self.engine.next_request()
            self.store.save_intent(request, self.engine.view(request["actor"]), self.engine.family(request["actor"]))

        modes = [row[0] for row in self.store.db.execute("SELECT mode FROM decisions")]
        self.assertEqual(len(modes), 3)


class CacheSplitTests(unittest.TestCase):
    def view(self, events):
        return {"day": 3, "events": events, "identity": {"team": "red"}, "rules": {"text": "x" * 50}}

    def text(self, view):
        from clock_alignment.config import canonical
        return canonical({"authorized_observation": view, "turn": {"id": "t"}})

    def test_blocks_inside_the_stable_prefix_repeat_byte_for_byte_in_the_next_call_that_day(self):
        first_events = [{"kind": "say", "text": "a" * 900, "seq": index} for index in range(12)]
        first_view = self.view(first_events)
        second_view = self.view(first_events + [{"kind": "say", "text": "b" * 50, "seq": 12}])
        first, second = self.text(first_view), self.text(second_view)

        first_blocks = observation_blocks(first, stable_prefix_length(first, first_view))
        second_blocks = observation_blocks(second, stable_prefix_length(second, second_view))

        marked = [index for index, block in enumerate(first_blocks) if "cache_control" in block]
        self.assertEqual(len(marked), 1)
        cached = first_blocks[:marked[0] + 1]
        self.assertEqual([block["text"] for block in cached], [block["text"] for block in second_blocks[:len(cached)]])
        self.assertEqual("".join(block["text"] for block in first_blocks), first)
        self.assertLessEqual(len(second_blocks) - len(cached), 20)

    def test_a_short_prefix_gets_no_chunk_breakpoint(self):
        view = self.view([])
        text = self.text(view)

        blocks = observation_blocks(text, stable_prefix_length(text, view))

        self.assertLess(stable_prefix_length(text, view), CHUNK_CHARS)
        self.assertEqual(blocks, [{"type": "text", "text": text}])

    def test_text_that_does_not_open_with_day_and_events_is_rejected(self):
        view = self.view([{"kind": "say", "seq": 0}])

        with self.assertRaises(ValueError):
            stable_prefix_length('{"other":1}', view)


class ClaudeConfigTests(unittest.TestCase):
    def test_the_claude_profile_runs_at_xhigh_and_another_effort_is_refused(self):
        self.assertEqual(Config(model=OPUS, effort="xhigh").profile.provider, "anthropic")

        with self.assertRaises(ValueError):
            Config(model=OPUS, effort="medium")


if __name__ == "__main__":
    unittest.main()
