"""Field order survives the wire, and free local calls resume cleanly after a dead server or a kill."""
from copy import deepcopy
import json
import socket
import tempfile
import unittest

from clock_alignment.briefing import LABELS
from clock_alignment.config import Config
from clock_alignment.engine import Engine, neutral_action
from clock_alignment.local_provider import LocalProvider
from clock_alignment.provider import OpenAIProvider, ProviderFailure, attempt_call_id, wire_json
from clock_alignment.runner import declare_final_day_if_due, play_turn
from clock_alignment.schema import PROPERTIES, empty_action
from clock_alignment.store import IntegrityError, Store

QWEN = "Qwen/Qwen3-4B-Instruct-2507"
SERVED_CONTEXT = 48_000 + 4_096


def chat(text, model=QWEN):
    return {"id": "chatcmpl", "model": model, "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1100}}


class Wire:
    """Answer /tokenize with a count and chat calls from a list; record every post."""

    def __init__(self, answers, count=900):
        self.answers, self.count, self.posts = list(answers), count, []

    def post(self, path, payload, call_id):
        self.posts.append((path, deepcopy(payload), call_id))
        if path in ("/tokenize", "/responses/input_tokens"):
            if "prompt" in payload or isinstance(payload.get("input"), str):
                spoken = len((payload.get("prompt") or payload["input"]).split())
                return {"count": spoken, "input_tokens": spoken, "max_model_len": SERVED_CONTEXT}
            return {"count": self.count, "input_tokens": self.count, "max_model_len": SERVED_CONTEXT}
        if not self.answers:
            raise AssertionError(f"unexpected generation call {call_id}")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return deepcopy(answer)


class SpokenCountDies(Wire):
    """Answer everything, except that counting a spoken text fails."""

    def post(self, path, payload, call_id):
        if path == "/tokenize" and "prompt" in payload:
            raise ConnectionResetError("server gone while counting speech")
        return super().post(path, payload, call_id)


class SmallContext(Wire):
    """A server started with a context too small for the frozen request geometry."""

    def post(self, path, payload, call_id):
        reply = super().post(path, payload, call_id)
        if path == "/tokenize":
            reply["max_model_len"] = SERVED_CONTEXT - 1
        return reply


def generations(wire):
    return [post for post in wire.posts if post[0] in ("/v1/chat/completions", "/responses")]


class LocalResumeTests(unittest.TestCase):
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

    def mode(self):
        return self.store.db.execute("SELECT mode FROM decisions WHERE id=?", (self.request["id"],)).fetchone()[0]

    def test_schema_fields_reach_the_server_in_schema_order_with_judge_log_last(self):
        wire = Wire([chat(json.dumps(empty_action()))])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=wire), self.request)

        sent = generations(wire)[0][1]["response_format"]["json_schema"]["schema"]["properties"]
        self.assertEqual(list(sent), list(PROPERTIES))
        self.assertEqual(list(json.loads(wire_json(sent))), list(PROPERTIES))

    def test_the_briefing_reaches_the_server_in_reading_order(self):
        wire = Wire([chat(json.dumps(empty_action()))])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=wire), self.request)

        turn = json.loads(generations(wire)[0][1]["messages"][1]["content"])["turn"]
        self.assertEqual([line.split(":")[0] for line in turn["briefing"]], list(LABELS))

    def test_a_game_resumes_after_the_server_died_during_a_turn(self):
        dead = Wire([ConnectionResetError("server gone")] * 10)
        with self.assertRaises(ProviderFailure):
            play_turn(self.engine, self.store, LocalProvider(self.store, transport=dead), self.request)

        healthy = Wire([chat(json.dumps(empty_action()))])
        play_turn(self.engine, self.store, LocalProvider(self.store, transport=healthy), self.request)

        self.assertEqual(self.mode(), "model")
        self.assertEqual(len(generations(healthy)), 1)

    def test_a_call_killed_mid_flight_holds_no_allowance_after_resume(self):
        call_id = attempt_call_id(self.request["id"], 1)
        self.store.reserve(call_id, "game", self.actor, self.engine.family(self.actor), self.config.max_output_tokens,
                           {"interrupted": True}, self.request["session"], 512)

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=Wire([chat(json.dumps(empty_action()))])),
                  self.request)

        self.assertEqual(self.mode(), "model")
        self.assertEqual(self.store.call(call_id)["status"], "released")
        self.assertEqual(self.store.remaining(self.engine.family(self.actor)), self.config.family_tokens - 200)

    def test_a_dropped_connection_does_not_use_up_one_of_the_players_attempts(self):
        malformed = chat("{")
        wire = Wire([ConnectionResetError("blip"), malformed, malformed, chat(json.dumps(empty_action()))])

        play_turn(self.engine, self.store, LocalProvider(self.store, transport=wire), self.request)

        self.assertEqual(self.mode(), "model")
        notices = [json.loads(p[1]["messages"][1]["content"])["turn"].get("storyteller_notice", "")
                   for p in generations(wire)]
        self.assertFalse(any("server" in notice for notice in notices))

    def test_a_rejected_answer_stays_free_when_the_run_resumes_after_it(self):
        illegal = empty_action()
        illegal.update(whisper_to="P-not-a-player", whisper="hello", judge_log="because " * 20)
        dying = Wire([chat(json.dumps(illegal))] + [ConnectionResetError("server gone")] * 3)
        with self.assertRaises(ProviderFailure):
            play_turn(self.engine, self.store, LocalProvider(self.store, transport=dying), self.request)

        healthy = Wire([chat(json.dumps(empty_action()))])
        play_turn(self.engine, self.store, LocalProvider(self.store, transport=healthy), self.request)

        accepted_tokens = 200  # the fixture reply's completion_tokens; its empty judge_log refunds nothing
        self.assertEqual(self.mode(), "model")
        self.assertEqual(self.store.remaining(self.engine.family(self.actor)),
                         self.config.family_tokens - accepted_tokens)

    def test_a_spoken_count_failure_after_a_settled_receipt_stops_the_run_and_resumes_on_that_receipt(self):
        # Invariant: a completed call is never released or re-sent, so its tokens are charged exactly once.
        public = self.reach("discussion")
        statement = empty_action()
        statement.update(say="Good morning, everyone.", audience="public")
        dying = SpokenCountDies([chat(json.dumps(statement))])
        with self.assertRaisesRegex(ProviderFailure, "could not count the spoken tokens of settled call"):
            play_turn(self.engine, self.store, LocalProvider(self.store, transport=dying), public)
        first = attempt_call_id(public["id"], 1)
        self.assertEqual(self.store.call(first)["status"], "completed")
        self.assertEqual(len(generations(dying)), 1)

        healthy = Wire([])
        play_turn(self.engine, self.store, LocalProvider(self.store, transport=healthy), public)

        self.assertEqual(generations(healthy), [])
        self.assertEqual(self.store.call(first)["message_tokens"], 3)
        self.assertEqual(self.store.remaining(self.engine.family(public["actor"])), self.config.family_tokens - 200)
        self.assertEqual(self.store.db.execute("SELECT mode FROM decisions WHERE id=?", (public["id"],)).fetchone()[0],
                         "model")

    def test_a_server_with_too_small_a_context_is_refused_before_any_generation(self):
        wire = SmallContext([chat(json.dumps(empty_action()))])

        with self.assertRaisesRegex(ProviderFailure, "max-model-len 52096 or more"):
            play_turn(self.engine, self.store, LocalProvider(self.store, transport=wire), self.request)

        self.assertEqual([p[0] for p in wire.posts], ["/tokenize"])
        self.assertEqual(self.store.call(attempt_call_id(self.request["id"], 1))["status"], "released")

    def reach(self, kind):
        while (request := self.engine.next_request())["kind"] != kind:
            self.engine.apply(request["id"], self.engine.pass_action(request))
        return request

    def test_context_overflow_is_not_retried(self):
        wire = Wire([], count=self.config.max_input_tokens + 1)

        with self.assertRaises(ProviderFailure):
            play_turn(self.engine, self.store, LocalProvider(self.store, transport=wire), self.request)

        self.assertEqual([p[0] for p in wire.posts], ["/tokenize"])

    def test_a_receipt_from_the_wrong_model_stops_the_run_the_same_way_on_replay(self):
        wire = Wire([chat(json.dumps(empty_action()), model="another-model")])
        with self.assertRaises(IntegrityError):
            play_turn(self.engine, self.store, LocalProvider(self.store, transport=wire), self.request)

        with self.assertRaises(IntegrityError):
            play_turn(self.engine, self.store, LocalProvider(self.store, transport=Wire([])), self.request)


class PaidWireAndHorizonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_paid_schema_reaches_the_api_in_schema_order(self):
        config = Config()
        engine = Engine(config.to_dict(), seed=91)
        store = Store(self.temp.name)
        self.addCleanup(store.close)
        store.initialize(config, 91, engine, "openai")
        receipt = {"id": "r", "model": config.model, "status": "completed", "service_tier": "default",
                   "usage": {"input_tokens": 100, "output_tokens": 200, "output_tokens_details": {"reasoning_tokens": 50}},
                   "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(empty_action())}]}]}
        wire = Wire([receipt], count=100)

        play_turn(engine, store, OpenAIProvider(store, transport=wire), engine.next_request())

        self.assertEqual(list(generations(wire)[0][1]["text"]["format"]["schema"]["properties"]), list(PROPERTIES))

    def test_final_day_projection_ignores_charges_already_made_tonight(self):
        config = Config(game_dollar_limit=1)
        engine = Engine(config.to_dict(), seed=4)
        store = Store(self.temp.name)
        self.addCleanup(store.close)
        store.initialize(config, 4, engine, "openai")
        for index, session in enumerate(["day-1:night"] + ["day-2:night"] * 6):
            call_id, actor = f"call-{index}", engine.roots[index]
            store.reserve(call_id, "game", actor, actor, 1024, {}, session, 512)
            store.finish(call_id, {"usage": {"input_tokens": 48_000, "output_tokens": 1000}}, .1)
        while engine.day == 1:
            request = engine.next_request()
            action = neutral_action()
            if request["kind"] == "nominate":
                action["nominate"] = request["legal"]["nominate"][0]
            engine.apply(request["id"], action)

        declare_final_day_if_due(engine, store)

        self.assertIsNone(engine.final_day)

    def test_only_a_zero_cost_call_can_release_its_hold(self):
        config = Config()
        engine = Engine(config.to_dict(), seed=4)
        store = Store(self.temp.name)
        self.addCleanup(store.close)
        store.initialize(config, 4, engine, "openai")
        store.reserve("paid", "game", engine.roots[0], engine.roots[0], 512, {}, "day-1:night", 512)
        store.finish("paid", {"usage": {"input_tokens": 0, "output_tokens": 0}}, .1)

        with self.assertRaises(IntegrityError):
            store.release("paid", "test")


if __name__ == "__main__":
    unittest.main()
