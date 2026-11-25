"""The Claude path sends the shared prompt text in one block, charges each prompt at its rate card and keeps every limit."""
from copy import deepcopy
import json
import os
import tempfile
import unittest
from unittest import mock

from clock_alignment.allowance import decide_allowance
from clock_alignment.anthropic_provider import (API_KEY_ENV, REFUSAL_FAULT, UNSUPPORTED_SCHEMA_KEYS, AnthropicProvider,
                                                claude_schema)
from clock_alignment.cli import build_parser
from clock_alignment.config import NANODOLLARS, Config, ModelProfile, RateCard
from clock_alignment.engine import Engine
from clock_alignment.prompts import CLAUDE_REWORDING, CLAUDE_SYSTEM, SYSTEM
from clock_alignment.provider import (AllowanceCutoff, CapHit, InvalidResponse, ProviderFailure, TransportFailure,
                                      action_payload)
from clock_alignment.runner import play_turn
from clock_alignment.schema import empty_action, request_schema
from clock_alignment.store import BudgetLimit, Store

OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
COUNTED = 2_000
HAIKU_THRESHOLD = 100_000  # Haiku 5.5 prompts over this many tokens pay five times the base prices


def reply(text, stop="end_turn", fresh=900, read=0, written=0, output=200, category=None, model=OPUS):
    raw = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": model,
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


class ClaudeFixture:
    """One stored game at its first request; subclasses name the model and the input cap. Holds no tests."""
    model, max_input_tokens = OPUS, Config.max_input_tokens

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(model=self.model, effort="xhigh", max_input_tokens=self.max_input_tokens)
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

    def act(self, answers, allowance=None, transport_try=1, counted=COUNTED):
        transport = FakeTransport(answers, counted)
        provider = AnthropicProvider(self.store, transport=transport)
        result = provider.act(self.request, self.view, allowance or self.allowance(), self.family,
                              transport_try=transport_try)
        return result, transport


class ClaudeProviderTests(ClaudeFixture, unittest.TestCase):
    def test_the_request_sends_the_shared_text_as_one_uncached_block_at_xhigh_with_summarized_thinking(self):
        allowance = self.allowance()

        _, transport = self.act([reply(json.dumps(empty_action()))], allowance)

        (payload,) = transport.generations
        balance = self.store.daily_balance(self.family, self.view["day"])
        shared = action_payload(Config(), self.request, self.view, allowance, balance)["input"]
        self.assertEqual(payload["messages"], [{"role": "user", "content": [{"type": "text", "text": shared}]}])
        self.assertEqual(payload["system"], [{"type": "text", "text": CLAUDE_SYSTEM}])
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

    def test_a_nullable_choice_list_becomes_one_branch_per_type(self):
        # Witness: the API rejected {"type": ["string", "null"], "enum": ["P5446bc16", null]} with HTTP 400.
        node = {"type": ["string", "null"], "enum": ["P1", "P2", None], "description": "who reads it"}

        converted = claude_schema(node)

        self.assertEqual(converted, {"anyOf": [{"type": "string", "enum": ["P1", "P2"]}, {"type": "null"}],
                                     "description": "who reads it"})
        self.assertEqual(claude_schema({"type": ["boolean", "null"]}),
                         {"anyOf": [{"type": "boolean"}, {"type": "null"}]})
        self.assertEqual(claude_schema({"type": ["string", "null"], "enum": [None]}), {"anyOf": [{"type": "null"}]})

    def test_no_schema_sent_to_claude_lists_more_than_one_type_in_one_node(self):
        schema = claude_schema(request_schema(self.request["legal"], speaking=True))

        def types(node):
            if isinstance(node, dict):
                yield node.get("type")
                for value in node.values():
                    yield from types(value)
            elif isinstance(node, list):
                for value in node:
                    yield from types(value)

        self.assertFalse(any(isinstance(kind, list) for kind in types(schema)))

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


class HaikuRateCardTests(ClaudeFixture, unittest.TestCase):
    """Haiku 5.5 bills each request at the card for its whole prompt's length, cache tokens included."""
    model, max_input_tokens = HAIKU, 300_000

    def charge(self, **usage):
        # The receipt check requires the billed prompt to fit the exact count taken before sending.
        self.act([reply(json.dumps(empty_action()), output=150, model=HAIKU, **usage)], counted=sum(usage.values()))
        return self.store.call(f"game:{self.request['id']}")["charge_nano"]

    def test_a_prompt_of_exactly_the_threshold_pays_the_base_card(self):
        self.assertEqual(self.charge(fresh=HAIKU_THRESHOLD), HAIKU_THRESHOLD * 100 + 150 * 500)

    def test_one_token_over_the_threshold_puts_every_token_on_the_long_card(self):
        self.assertEqual(self.charge(fresh=HAIKU_THRESHOLD + 1), (HAIKU_THRESHOLD + 1) * 500 + 150 * 2_500)

    def test_cache_reads_count_toward_the_threshold(self):
        # Witness: 60,000 fresh tokens alone are short; with 40,001 cache reads the prompt is 100,001 tokens.
        self.assertEqual(self.charge(fresh=60_000, read=40_001), 60_000 * 500 + 40_001 * 50 + 150 * 2_500)

    def test_the_hold_prices_the_whole_input_cap_at_the_long_cards_cache_write_price(self):
        self.store.reserve("game:hold", "game", self.actor, self.family, 1_024, {}, self.request["session"],
                           self.store.message_remaining(self.actor, self.request["session"]))

        self.assertEqual(self.store.call("game:hold")["reservation_nano"], 300_000 * 1_000 + 1_024 * 2_500)


class ClaudeRewordingTests(unittest.TestCase):
    def test_the_claude_prompt_differs_from_the_shared_one_only_in_the_reworded_sentences(self):
        restored = CLAUDE_SYSTEM
        for old, new in CLAUDE_REWORDING:
            restored = restored.replace(new, old)

        self.assertEqual(restored, SYSTEM)
        self.assertNotIn("all of your reasoning", CLAUDE_SYSTEM)
        self.assertIn("judge_log is the LAST field", CLAUDE_SYSTEM)

    def test_a_rewording_that_no_longer_matches_the_prompt_is_an_error(self):
        from clock_alignment.prompts import reworded

        with self.assertRaises(ValueError):
            reworded(SYSTEM, (("a sentence the prompt does not contain", "x"),))


class ClaudeConfigTests(unittest.TestCase):
    def test_the_claude_profile_runs_at_xhigh_and_another_effort_is_refused(self):
        self.assertEqual(Config(model=OPUS, effort="xhigh").profile.provider, "anthropic")

        with self.assertRaises(ValueError):
            Config(model=OPUS, effort="medium")

    def test_a_long_prompt_card_cheaper_than_the_base_card_is_refused(self):
        cheap = RateCard(input=50, output=2_500, cache_read=50, cache_write=1_000)

        with self.assertRaises(ValueError) as caught:
            ModelProfile(provider="anthropic", effort="xhigh", input_nano_per_token=100, output_nano_per_token=500,
                         long_prompt_tokens=HAIKU_THRESHOLD, long_prompt_rates=cheap)

        self.assertIn("long_prompt_rates must cost at least the base card", str(caught.exception))

    def test_a_threshold_without_its_card_is_refused(self):
        with self.assertRaises(ValueError):
            ModelProfile(provider="anthropic", effort="xhigh", input_nano_per_token=100, output_nano_per_token=500,
                         long_prompt_tokens=HAIKU_THRESHOLD)

    def test_the_claude_command_requires_a_known_model(self):
        base = ["claude", "--out", "run", "--allow-paid", "--dollar-limit", "5"]
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(base)
            with self.assertRaises(SystemExit):
                build_parser().parse_args([*base, "--model", "claude-sonnet-5-5"])

        self.assertEqual(build_parser().parse_args([*base, "--model", HAIKU]).model, HAIKU)


if __name__ == "__main__":
    unittest.main()
