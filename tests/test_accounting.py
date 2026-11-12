"""Durable budget and concurrency fault tests; no network or model calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import threading
import unittest

from clock_alignment.config import Config, INPUT_NANO_PER_TOKEN, OUTPUT_NANO_PER_TOKEN
from clock_alignment.engine import Engine
from clock_alignment.store import BudgetLimit, IntegrityError, PendingCall, Store


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def initialized(self, config=None):
        config = config or Config()
        engine = Engine(config.to_dict(), seed=71)
        store = Store(self.temp.name)
        store.initialize(config, 71, engine, "openai")
        return store, engine

    def test_concurrent_family_reservations_cannot_overspend(self):
        config = Config(family_tokens=512, max_output_tokens=512)
        store, engine = self.initialized(config)
        family = engine.roots[0]
        store.close()
        barrier = threading.Barrier(4)

        def reserve(index):
            connection = Store(self.temp.name)
            try:
                barrier.wait(timeout=10)
                try:
                    connection.reserve(f"race-{index}", "game", family, family, 512, {}, "day-1:night", 512)
                except BudgetLimit:
                    return False
                return True
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            admitted = list(pool.map(reserve, range(4)))
        self.assertEqual(sum(admitted), 1)
        check = Store(self.temp.name)
        try:
            self.assertEqual(check.remaining(family), 0)
            self.assertEqual(check.accounting()["calls"], 1)
        finally:
            check.close()

    def test_concurrent_global_reservations_cannot_cross_dollar_cap(self):
        config = Config(dollar_limit=2, game_dollar_limit=1, analysis_dollar_limit=1,
                        max_output_tokens=512, family_tokens=8192)
        store, engine = self.initialized(config)
        roots = engine.roots
        store.close()
        barrier = threading.Barrier(8)

        def reserve(index):
            connection = Store(self.temp.name)
            try:
                barrier.wait(timeout=10)
                try:
                    connection.reserve(f"global-{index}", "game", roots[index], roots[index], 512, {}, "day-1:night", 512)
                except BudgetLimit:
                    return False
                return True
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            admitted = list(pool.map(reserve, range(8)))
        reservation = config.max_input_tokens * INPUT_NANO_PER_TOKEN + 512 * OUTPUT_NANO_PER_TOKEN
        self.assertEqual(sum(admitted), 1_000_000_000 // reservation)
        self.assertLess(sum(admitted), 8)
        check = Store(self.temp.name)
        try:
            self.assertLessEqual(check.accounting()["conservative_usd"], 1)
            self.assertEqual(check.accounting()["calls"], sum(admitted))
        finally:
            check.close()

    def test_unknown_billing_retains_full_dollars_and_output_after_reopen(self):
        store, engine = self.initialized()
        family = engine.roots[0]
        store.reserve("unknown", "game", family, family, 1024, {}, "day-1:night", 512)
        reservation = store.call("unknown")["reservation_nano"]
        store.fail("unknown", "socket.timeout")
        store.close()
        reopened = Store(self.temp.name)
        try:
            self.assertEqual(reopened.call("unknown")["status"], "unknown")
            self.assertEqual(reopened.call("unknown")["charge_nano"], reservation)
            self.assertEqual(reopened.remaining(family), Config().family_tokens - 1024)
            self.assertEqual(reopened.accounting()["held_unknown_usd"], reservation / 1_000_000_000)
            with self.assertRaises(PendingCall):
                reopened.reserve("unknown", "game", family, family, 1024, {}, "day-1:night", 512)
        finally:
            reopened.close()

    def test_reasoning_is_part_of_output_not_charged_twice(self):
        store, engine = self.initialized()
        try:
            family = engine.roots[0]
            store.reserve("settled", "game", family, family, 1024, {}, "day-1:night", 512)
            receipt = {"usage": {"input_tokens": 100, "output_tokens": 192,
                                 "output_tokens_details": {"reasoning_tokens": 128}}}
            store.finish("settled", receipt, .1)
            self.assertEqual(store.remaining(family), Config().family_tokens - 192)
            self.assertEqual(store.call("settled")["charge_nano"],
                             100 * INPUT_NANO_PER_TOKEN + 192 * OUTPUT_NANO_PER_TOKEN)
            self.assertEqual(store.accounting()["reasoning_tokens"], 128)
            with self.assertRaises(IntegrityError):
                store.finish("settled", receipt, .1)
        finally:
            store.close()

    def test_missing_or_out_of_bounds_usage_keeps_reservation(self):
        for receipt in ({}, {"usage": None}, {"usage": "invalid"},
                        {"usage": {"input_tokens": 10, "output_tokens": 10,
                                   "output_tokens_details": None}},
                        {"usage": {"input_tokens": 49_000, "output_tokens": 1}},
                        {"usage": {"input_tokens": 10, "output_tokens": 1025}},
                        {"usage": {"input_tokens": 10, "output_tokens": 10,
                                   "output_tokens_details": {"reasoning_tokens": 11}}}):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as directory:
                config, engine = Config(), Engine(seed=2)
                store = Store(directory)
                try:
                    store.initialize(config, 2, engine, "openai")
                    root = engine.roots[0]
                    store.reserve("bad-usage", "game", root, root, 1024, {}, "day-1:night", 512)
                    before = store.accounting()
                    with self.assertRaises(IntegrityError):
                        store.finish("bad-usage", receipt, .1)
                    self.assertEqual(store.call("bad-usage")["status"], "unknown")
                    self.assertEqual(store.accounting()["conservative_usd"], before["conservative_usd"])
                    self.assertEqual(store.remaining(root), config.family_tokens - 1024)
                    self.assertEqual(json.loads(store.call("bad-usage")["response_json"]), receipt)
                finally:
                    store.close()

    def test_analysis_has_own_ceiling_and_does_not_consume_family_allowance(self):
        store, engine = self.initialized(Config(analysis_output_tokens=1024))
        try:
            root = engine.roots[0]
            remaining = store.remaining(root)
            store.reserve("probe-one", "probe", root, None, 1024, {})
            self.assertEqual(store.remaining(root), remaining)
            with self.assertRaises(BudgetLimit):
                store.reserve("judge-too-much", "judge", "observer", None, 512, {})
            self.assertEqual(store.accounting()["calls"], 1)
            self.assertGreater(store.accounting()["conservative_usd"], 0)
        finally:
            store.close()

    def test_generation_call_limit_counts_unknown_attempts(self):
        store, engine = self.initialized(Config(max_paid_calls=1))
        try:
            root = engine.roots[0]
            store.reserve("first", "game", root, root, 512, {}, "day-1:night", 512)
            store.fail("first", "timeout")
            with self.assertRaises(BudgetLimit):
                store.reserve("second", "game", root, root, 512, {}, "day-1:night", 512)
        finally:
            store.close()

    def test_runner_lock_and_immutable_intents(self):
        store, engine = self.initialized()
        other = Store(self.temp.name)
        try:
            with store.runner_lock():
                with self.assertRaisesRegex(RuntimeError, "Another runner"):
                    with other.runner_lock():
                        self.fail("second runner unexpectedly acquired lock")
            with other.runner_lock():
                pass
            request = engine.next_request()
            view = engine.view(request["actor"])
            root = engine.family(request["actor"])
            store.save_intent(request, view, root)
            store.save_intent(request, view, root)
            with self.assertRaisesRegex(IntegrityError, "changed"):
                store.save_intent(request, {**view, "injected": "new state"}, root)
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute("DELETE FROM intents")
        finally:
            other.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
