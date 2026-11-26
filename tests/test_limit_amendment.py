"""A paused game's output limits can only rise, every player is told, and a refused amendment changes nothing."""
import json
import tempfile
import unittest
from unittest import mock

from clock_alignment.amendment import CLAUDE_MAX_OUTPUT_TOKENS, amend_limits, require_raised_limits
from clock_alignment.config import Config
from clock_alignment.engine import Engine
from clock_alignment.runner import ScriptedProvider, play_turn, run
from clock_alignment.store import IntegrityError, Store

OLD_ANSWER, OLD_FAMILY = 4_096, 32_768
NEW_ANSWER, NEW_FAMILY = 8_192, 65_536
TURNS_BEFORE_PAUSE = 30


class PausedGame:
    """A scripted game paused between requests on day 1, with today's balances already shown. Holds no tests."""
    created_on_older_code = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config(max_days=2, max_output_tokens=OLD_ANSWER, family_tokens=OLD_FAMILY)
        store = Store(self.temp.name)
        engine = Engine(config.to_dict(), seed=7)
        if self.created_on_older_code:
            with mock.patch("clock_alignment.store.code_fingerprint", return_value="fingerprint-of-older-code"):
                store.initialize(config, 7, engine, "scripted")
        else:
            store.initialize(config, 7, engine, "scripted")
        provider = ScriptedProvider(store)
        for _ in range(TURNS_BEFORE_PAUSE):
            play_turn(engine, store, provider, engine.next_request())
        self.day, self.roots = engine.day, list(engine.roots)
        self.balances = {root: store.daily_balance(root, self.day) for root in self.roots}
        self.remaining = {root: store.remaining(root) for root in self.roots}
        self.checkpoints = store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        store.close()

    def reopen(self):
        store = Store(self.temp.name)
        self.addCleanup(store.close)
        return store


class LimitAmendmentTests(PausedGame, unittest.TestCase):
    def test_an_amendment_raises_both_limits_for_every_family_by_the_same_delta_and_keeps_the_log_append_only(self):
        record = amend_limits(self.temp.name, max_output_tokens=NEW_ANSWER, family_tokens=NEW_FAMILY)

        store = self.reopen()
        delta = NEW_FAMILY - OLD_FAMILY
        self.assertEqual((store.config.max_output_tokens, store.config.family_tokens), (NEW_ANSWER, NEW_FAMILY))
        self.assertEqual({root: store.remaining(root) for root in self.roots},
                         {root: tokens + delta for root, tokens in self.remaining.items()})
        # Balances already shown today keep their night-start value; the announcement states the rise.
        self.assertEqual({root: store.daily_balance(root, self.day) for root in self.roots}, self.balances)
        self.assertEqual(store.amendments(), [record])
        self.assertEqual(record["old"], {"max_output_tokens": OLD_ANSWER, "family_tokens": OLD_FAMILY})
        self.assertTrue(store.verify())
        self.assertEqual(store.meta("config")["family_tokens"], OLD_FAMILY)  # the frozen original stays readable

    def test_every_active_player_sees_the_announcement_and_the_new_rule_text(self):
        amend_limits(self.temp.name, max_output_tokens=NEW_ANSWER, family_tokens=NEW_FAMILY)

        engine = Engine.restore(self.reopen().checkpoint())
        actors = [name for name, agent in engine.agents.items() if agent["active"]]
        for actor in actors:
            view = engine.view(actor)
            texts = [event["data"]["text"] for event in view["events"] if event["kind"] == "storyteller_announcement"]
            self.assertEqual(len(texts), 1, actor)
            self.assertIn("up to 8,192 tokens (was 4,096)", texts[0])
            self.assertIn("is 65,536 tokens for the whole game (was 32,768)", texts[0])
            self.assertIn(f"past the {NEW_ANSWER}-token maximum", json.dumps(view["rules"]))
        self.assertGreater(len(actors), 0)

    def test_the_game_resumes_on_the_amended_code_and_finishes(self):
        amend_limits(self.temp.name, max_output_tokens=NEW_ANSWER, family_tokens=NEW_FAMILY)

        result = run(self.temp.name, provider="scripted", resume=True)

        self.assertIn(result["status"], {"completed", "censored"})
        self.assertEqual(result["config"]["max_output_tokens"], NEW_ANSWER)
        later_views = [json.loads(row[0]) for row in self.reopen().db.execute(
            "SELECT view_json FROM intents ORDER BY rowid")][TURNS_BEFORE_PAUSE:]
        self.assertTrue(later_views)
        self.assertTrue(all(any(event["kind"] == "storyteller_announcement" for event in view["events"])
                            for view in later_views))

    def test_lowering_a_limit_is_refused_and_changes_nothing(self):
        with self.assertRaises(ValueError) as caught:
            amend_limits(self.temp.name, max_output_tokens=NEW_ANSWER, family_tokens=OLD_FAMILY - 1)

        self.assertIn("limits may only rise", str(caught.exception))
        self.assert_unchanged()

    def test_an_amendment_with_a_call_in_flight_is_refused_and_changes_nothing(self):
        store = Store(self.temp.name)
        engine = Engine.restore(store.checkpoint())
        request = engine.next_request()
        actor = request["actor"]
        store.reserve("game:in-flight", "game", actor, engine.family(actor), 1_024, {}, request["session"],
                      store.message_remaining(actor, request["session"]))
        store.close()

        with self.assertRaises(IntegrityError) as caught:
            amend_limits(self.temp.name, max_output_tokens=NEW_ANSWER, family_tokens=NEW_FAMILY)

        self.assertIn("a call is still in flight", str(caught.exception))
        self.assert_unchanged()

    def assert_unchanged(self):
        store = self.reopen()
        self.assertEqual((store.config.max_output_tokens, store.config.family_tokens), (OLD_ANSWER, OLD_FAMILY))
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], self.checkpoints)
        self.assertEqual(store.amendments(), [])


class OlderCodeAmendmentTests(PausedGame, unittest.TestCase):
    """The live case: the run was created on older code, so only a recorded amendment may resume it."""
    created_on_older_code = True

    def test_a_run_created_on_older_code_is_still_refused_without_an_amendment(self):
        with self.assertRaises(IntegrityError) as caught:
            self.reopen().verify()

        self.assertIn("Code changed since this experiment was created", str(caught.exception))

    def test_an_amendment_records_both_fingerprints_and_lets_the_run_resume(self):
        record = amend_limits(self.temp.name, max_output_tokens=NEW_ANSWER, family_tokens=NEW_FAMILY)

        self.assertEqual(record["old_code_fingerprint"], "fingerprint-of-older-code")
        self.assertTrue(self.reopen().verify())
        self.assertIn(run(self.temp.name, provider="scripted", resume=True)["status"], {"completed", "censored"})


class AmendmentRuleTests(unittest.TestCase):
    def test_a_claude_run_cannot_be_raised_past_the_api_maximum(self):
        haiku = Config(model="claude-haiku-5-5", effort="xhigh", max_input_tokens=300_000,
                       max_output_tokens=65_536, family_tokens=1_048_576)

        with self.assertRaises(ValueError) as caught:
            require_raised_limits(haiku, max_output_tokens=CLAUDE_MAX_OUTPUT_TOKENS + 1, family_tokens=4_194_304)

        self.assertIn("exceeds the Messages API maximum", str(caught.exception))
        require_raised_limits(haiku, max_output_tokens=CLAUDE_MAX_OUTPUT_TOKENS, family_tokens=4_194_304)

    def test_an_amendment_that_changes_nothing_is_refused(self):
        config = Config(max_output_tokens=OLD_ANSWER, family_tokens=OLD_FAMILY)

        with self.assertRaises(ValueError):
            require_raised_limits(config, max_output_tokens=OLD_ANSWER, family_tokens=OLD_FAMILY)


if __name__ == "__main__":
    unittest.main()
