"""Injected transport tests: validate spending/replay without real API calls."""
from copy import deepcopy
import json
import socket
import tempfile
import unittest

from clock_alignment.allowance import decide_allowance
from clock_alignment.config import Config
from clock_alignment.engine import Engine
from clock_alignment.provider import InvalidResponse, OpenAIProvider, ProviderFailure, action_payload
from clock_alignment.schema import empty_action
from clock_alignment.store import IntegrityError, PendingCall, Store


class Transport:
    def __init__(self, receipt=None, count=128, fail=None):
        self.receipt, self.count, self.fail = receipt, count, fail
        self.calls = []

    def post(self, path, payload, call_id):
        self.calls.append((path, deepcopy(payload), call_id))
        if path == "/responses/input_tokens":
            return {"input_tokens": self.count}
        if self.fail is not None:
            raise self.fail
        if self.receipt is None:
            raise AssertionError("Unexpected generation network call")
        return deepcopy(self.receipt)


def valid_receipt(config=None, action=None):
    config = config or Config()
    return {"id": "response-fixture", "model": config.model, "status": "completed",
            "service_tier": "default", "usage": {"input_tokens": 128, "output_tokens": 256,
                "output_tokens_details": {"reasoning_tokens": 128}},
            "output": [{"type": "message", "content": [{"type": "output_text",
                "text": json.dumps(empty_action() if action is None else action)}]}]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config()
        self.engine = Engine(self.config.to_dict(), seed=91)
        self.store = Store(self.temp.name)
        self.store.initialize(self.config, 91, self.engine, "openai")
        self.request = self.engine.next_request()
        self.actor = self.request["actor"]
        self.family = self.engine.family(self.actor)
        self.view = self.engine.view(self.actor)
        self.store.save_intent(self.request, self.view, self.family)
        self.key = "game:" + self.request["id"]

    def tearDown(self):
        self.store.close()

    def allowance(self, store):
        return decide_allowance(self.config, store.remaining(self.family),
                                store.message_remaining(self.actor, self.request["session"]))

    def act(self, transport):
        return OpenAIProvider(self.store, transport=transport).act(
            self.request, self.view, self.allowance(self.store), self.family)

    def test_crash_after_receipt_reuses_saved_response_and_commits_once(self):
        action = empty_action()
        action["private_note"] = "Recorded before simulated process death."
        transport = Transport(valid_receipt(action=action))
        observed = self.act(transport)
        self.assertEqual(observed, action)
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNone(self.store.decision(self.request["id"]))
        before = self.store.accounting()
        # Even an in-memory action application is lost if its atomic decision /
        # checkpoint transaction never committed before the process died.
        self.engine.apply(self.request["id"], observed)
        self.store.close()
        self.store = Store(self.temp.name)
        recovered = Engine.restore(self.store.checkpoint())
        recovered_request = recovered.next_request()
        self.assertEqual(recovered_request, self.request)
        self.assertEqual(recovered.view(self.actor), self.view)
        self.store.save_intent(recovered_request, recovered.view(self.actor), self.family)
        blocked_network = Transport()
        replayed = self.act(blocked_network)
        self.assertEqual(replayed, action)
        self.assertEqual(blocked_network.calls, [])
        self.assertEqual(self.store.accounting(), before)
        recovered.apply(self.request["id"], replayed)
        self.store.commit_action(self.request["id"], replayed, recovered.snapshot(), self.key)
        with self.assertRaisesRegex(IntegrityError, "already committed"):
            self.store.commit_action(self.request["id"], replayed, recovered.snapshot(), self.key)
        self.assertTrue(self.store.verify())

    def test_wrong_model_count_or_tier_stay_rejected_on_replay(self):
        cases = [{"model": "a-different-model"}, {"service_tier": "priority"},
                 {"usage": {"input_tokens": 129, "output_tokens": 256,
                            "output_tokens_details": {"reasoning_tokens": 128}}}]
        for change in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                try:
                    store.initialize(self.config, 91, self.engine, "openai")
                    raw = {**valid_receipt(), **change}
                    wire = Transport(raw)
                    provider = OpenAIProvider(store, transport=wire)
                    with self.assertRaises(ProviderFailure):
                        provider.act(self.request, self.view, self.allowance(store), self.family)
                    self.assertEqual(store.call(self.key)["status"], "completed")
                    before = store.accounting()
                    replay_wire = Transport()
                    with self.assertRaises(IntegrityError):
                        OpenAIProvider(store, transport=replay_wire).act(
                            self.request, self.view, self.allowance(store), self.family)
                    self.assertEqual(replay_wire.calls, [])
                    self.assertEqual(store.accounting(), before)
                    self.assertIsNone(store.decision(self.request["id"]))
                finally:
                    store.close()

    def test_capped_refused_or_malformed_response_keeps_paid_receipt(self):
        variants = []
        incomplete = valid_receipt()
        incomplete["status"] = "incomplete"
        variants.append(incomplete)
        refusal = valid_receipt()
        refusal["output"][0]["content"] = [{"type": "refusal", "refusal": "declined"}]
        variants.append(refusal)
        malformed = valid_receipt()
        malformed["output"][0]["content"][0]["text"] = "{"
        variants.append(malformed)
        variants.append(valid_receipt(action={"say": "unknown or missing fields"}))
        for index, receipt in enumerate(variants):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                try:
                    store.initialize(self.config, 91, self.engine, "openai")
                    wire = Transport(receipt)
                    with self.assertRaises(InvalidResponse):
                        OpenAIProvider(store, transport=wire).act(
                            self.request, self.view, self.allowance(store), self.family)
                    self.assertEqual(store.call(self.key)["status"], "completed")
                    saved = json.loads(store.call(self.key)["response_json"])
                    self.assertEqual(saved["output"], receipt["output"])
                    self.assertEqual(store.remaining(self.family), self.config.family_tokens - 256)
                    self.assertGreater(store.accounting()["conservative_usd"], 0)
                    self.assertIsNone(store.decision(self.request["id"]))
                    replay_wire = Transport()
                    with self.assertRaises(InvalidResponse):
                        OpenAIProvider(store, transport=replay_wire).act(
                            self.request, self.view, self.allowance(store), self.family)
                    self.assertEqual(replay_wire.calls, [])
                finally:
                    store.close()

    def test_context_overflow_counts_but_never_generates_or_truncates(self):
        wire = Transport(count=self.config.max_input_tokens + 1)
        with self.assertRaises(ProviderFailure):
            self.act(wire)
        self.assertEqual([p for p, _, _ in wire.calls], ["/responses/input_tokens"])
        self.assertEqual(self.store.call(self.key)["status"], "unknown")
        self.assertEqual(self.store.remaining(self.family), self.config.family_tokens - self.config.max_output_tokens)
        self.assertIsNone(self.store.decision(self.request["id"]))

    def test_timeout_preserves_reservation_and_never_blindly_resends(self):
        wire = Transport(fail=socket.timeout("simulated ambiguous billing"))
        with self.assertRaises(ProviderFailure):
            self.act(wire)
        before = self.store.accounting()
        self.assertGreater(before["held_unknown_usd"], 0)
        self.assertEqual(self.store.call(self.key)["status"], "unknown")
        replay_wire = Transport()
        with self.assertRaises(PendingCall):
            self.act(replay_wire)
        self.assertEqual(replay_wire.calls, [])
        self.assertEqual(self.store.accounting(), before)

    def test_count_and_generation_use_identical_authorized_inputs(self):
        wire = Transport(valid_receipt())
        self.act(wire)
        count_payload, generation_payload = wire.calls[0][1], wire.calls[1][1]
        for key in ("model", "instructions", "input", "reasoning", "text"):
            self.assertEqual(count_payload[key], generation_payload[key])
        self.assertFalse(generation_payload["store"])
        self.assertEqual(generation_payload["truncation"], "disabled")
        self.assertFalse({"tools", "previous_response_id", "conversation", "background"} & generation_payload.keys())
        decoded = json.loads(generation_payload["input"])
        self.assertEqual(decoded["authorized_observation"], self.view)
        self.assertNotIn("truth", decoded)

    def test_forbidden_provider_expansions_are_rejected_before_spend(self):
        provider_wire = Transport()
        provider = OpenAIProvider(self.store, transport=provider_wire)
        base = action_payload(self.config, self.request, self.view, self.allowance(self.store), self.config.family_tokens)
        additions = ({"tools": []}, {"previous_response_id": "another-player-response"},
                     {"conversation": "shared-room"}, {"background": True},
                     {"model": "a-different-model"}, {"service_tier": "priority"},
                     {"store": True}, {"truncation": "auto"})
        for addition in additions:
            with self.subTest(addition=addition), self.assertRaises(ValueError):
                provider.response("forbidden", "game", self.actor, self.family, {**base, **addition})
        self.assertEqual(provider_wire.calls, [])
        self.assertEqual(self.store.accounting()["calls"], 0)


if __name__ == "__main__":
    unittest.main()
