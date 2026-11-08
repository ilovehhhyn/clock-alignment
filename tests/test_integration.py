"""Network-free end-to-end tests of orchestration, snapshots, analysis and export."""
import contextlib
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clock_alignment.analysis import gameplay_fingerprint, judge, probes, report
from clock_alignment.cli import main
from clock_alignment.config import Config, digest
from clock_alignment.engine import Engine
from clock_alignment.runner import ScriptedProvider, run
from clock_alignment.schema import empty_action
from clock_alignment.store import IntegrityError, Store


class AnalysisTransport:
    def __init__(self):
        self.requests = []

    def post(self, path, payload, call_id):
        self.requests.append((path, payload, call_id))
        if path.endswith("input_tokens"):
            return {"input_tokens": 100}
        if payload["text"]["format"]["name"] == "belief_probe":
            answer = {"beliefs": [], "public_claim": "none", "plan_summary": "uncertain",
                      "would_change_my_mind": "new evidence"}
        else:
            evidence = json.loads(payload["input"])
            answer = {"evidence_ids": [evidence["request_id"]], "labels": ["uncertain"],
                      "explanation": "Fixture annotation", "confounds": ["No model was called"], "confidence": .5}
        return {"model": "gpt-6.1-sol", "status": "completed", "service_tier": "default",
                "usage": {"input_tokens": 100, "output_tokens": 200,
                          "output_tokens_details": {"reasoning_tokens": 100}},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}


class IntegrationTests(unittest.TestCase):
    def test_family_budget_readout_is_frozen_until_next_night(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                engine = Engine(seed=8)
                config = Config()
                store.initialize(config, 8, engine, "scripted")
                family = engine.roots[0]
                balance = store.daily_balance(family, 1)
                store.reserve("private-child-output", "game", family, family, 4096, {})
                store.finish("private-child-output", {"usage": {"input_tokens": 100, "output_tokens": 900}}, .1)
                self.assertEqual(store.daily_balance(family, 1), balance)
                self.assertEqual(store.remaining(family), config.family_tokens - 900)
                self.assertEqual(store.daily_balance(family, 2), config.family_tokens - 900)
                store.close()
                store = Store(tmp)
                self.assertEqual(store.daily_balance(family, 1), balance)
            finally:
                store.close()

    def test_demo_all_arms_no_network_or_key_reads(self):
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            for arm in ("solo", "advisory", "copied-ballot", "dynamic-voting"):
                out = Path(tmp) / arm
                result = run(out, config=Config(arm=arm, max_days=2))
                self.assertIn(result["status"], {"completed", "censored"}, result)
                self.assertEqual(result["actual_paid_calls"], 0)
                summary = report(out)
                self.assertEqual(summary["game"]["status"], result["status"])
                self.assertTrue(summary["simulation"])
                self.assertTrue((out / "samples.jsonl").exists())
                if arm == "solo":
                    self.assertEqual(summary["births"], 0)
                elif arm == "copied-ballot":
                    store = Store(out)
                    try:
                        engine = Engine.restore(store.checkpoint())
                        self.assertGreater(summary["births"], 0)
                        for row in store.db.execute("SELECT actor FROM calls"):
                            self.assertFalse(engine.agents[row[0]]["delegate"])
                    finally:
                        store.close()

    def test_paid_flag_required_before_key_or_network(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()), \
                patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            with self.assertRaises(SystemExit) as error:
                main(["run", "--out", tmp])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse((Path(tmp) / "experiment.sqlite").exists())

    def test_reopen_completed_demo_preserves_actions_and_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = run(tmp, config=Config(max_days=1))
            store = Store(tmp)
            try:
                before = gameplay_fingerprint(store)
                calls = store.accounting()
            finally:
                store.close()
            resumed = run(tmp, resume=True)
            store = Store(tmp)
            try:
                self.assertEqual(before, gameplay_fingerprint(store))
                self.assertEqual(calls, store.accounting())
                self.assertEqual(initial["status"], resumed["status"])
            finally:
                store.close()

    def test_duplicate_experiment_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp, config=Config(max_days=1))
            with self.assertRaises(FileExistsError):
                run(tmp)

    def test_budget_exhaustion_is_explicit_game_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run(tmp, config=Config(family_tokens=512, max_output_tokens=512, max_days=2))
            self.assertIn(result["status"], {"completed", "censored"})
            summary = report(tmp)
            self.assertGreater(summary["budget_passes"], 0)
            self.assertTrue(all(n >= 0 for n in summary["accounting"]["remaining_by_lineage"].values()))
            self.assertEqual(summary["faults"], [])

    def test_global_cap_incomplete_not_a_loss_and_report_agrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run(tmp, config=Config(max_paid_calls=1))
            self.assertEqual(result["status"], "incomplete")
            self.assertIsNone(result["winner"])
            self.assertEqual(report(tmp)["game"]["status"], "incomplete")

    def test_provider_schema_error_is_invalid_not_gameplay_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ScriptedProvider, "act", return_value={"spawn": True}):
                result = run(tmp)
            self.assertEqual(result["status"], "invalid")
            self.assertIsNone(result["winner"])
            self.assertEqual(report(tmp)["game"]["status"], "invalid")
            store = Store(tmp)
            try:
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
            finally:
                store.close()

    def test_stop_file_pause_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "STOP").touch()
            paused = run(tmp, config=Config(max_days=1))
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(report(tmp)["game"]["status"], "paused")
            (Path(tmp) / "STOP").unlink()
            self.assertIn(run(tmp, resume=True)["status"], {"completed", "censored"})

    def test_fork_probes_do_not_change_game_history_rng_or_lineage_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp, config=Config(max_days=1))
            store = Store(tmp)
            try:
                before = gameplay_fingerprint(store)
                expected = {}
                for row in store.db.execute("SELECT * FROM intents ORDER BY rowid"):
                    request = json.loads(row["request_json"])
                    if request["kind"] == "discussion":
                        expected.setdefault(row["actor"], row["id"])
            finally:
                store.close()
            transport = AnalysisTransport()
            result = probes(tmp, transport=transport)
            self.assertTrue(result["gameplay_unchanged"])
            sent = [json.loads(p[1]["input"]) for p in transport.requests if p[0] == "/responses"]
            self.assertEqual(len(sent), 12)
            for evidence in sent:
                view = evidence["authorized_observation"]
                self.assertEqual(evidence["snapshot_request_id"], expected[view["self"]])
                self.assertNotIn("grimoire", view)
                self.assertNotIn("rng_state", view)
                self.assertNotIn("seed", view)
            store = Store(tmp)
            try:
                self.assertEqual(before, gameplay_fingerprint(store))
                count = store.accounting()["calls"]
            finally:
                store.close()
            probes(tmp, transport=AnalysisTransport())
            store = Store(tmp)
            try:
                self.assertEqual(store.accounting()["calls"], count)
            finally:
                store.close()

    def test_judge_receives_contemporaneous_view_only_and_never_mutates_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp, config=Config(max_days=1))
            store = Store(tmp)
            try:
                before = gameplay_fingerprint(store)
                req = store.db.execute("SELECT id FROM decisions ORDER BY rowid LIMIT 1").fetchone()[0]
            finally:
                store.close()
            transport = AnalysisTransport()
            result = judge(tmp, req, transport=transport)
            self.assertEqual(result["labels"], ["uncertain"])
            sent = [json.loads(p[1]["input"]) for p in transport.requests if p[0] == "/responses"][0]
            self.assertEqual(set(sent), {"request_id", "actor", "prior_authorized_observation", "chosen_action",
                                         "exposed_reasoning_summaries"})
            store = Store(tmp)
            try:
                self.assertEqual(before, gameplay_fingerprint(store))
            finally:
                store.close()

    def test_no_live_probes_or_judge_to_compete_with_game_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "STOP").touch()
            run(tmp)
            transport = AnalysisTransport()
            with self.assertRaisesRegex(ValueError, "completed/censored"):
                probes(tmp, transport=transport)
            self.assertEqual(transport.requests, [])

    def test_every_sent_game_request_is_only_the_authorized_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp, config=Config(max_days=2))
            store = Store(tmp)
            try:
                engine = Engine.restore(store.checkpoint())
                for row in store.db.execute("SELECT c.request_json AS payload,i.view_json AS view "
                    "FROM calls c JOIN intents i ON c.id='game:'||i.id WHERE c.scope='game'"):
                    payload, view = json.loads(row[0]), json.loads(row[1])
                    sent = json.loads(payload["input"])
                    self.assertEqual(sent["authorized_observation"], view)
                    self.assertFalse({"tools", "conversation", "previous_response_id"} & payload.keys())
                    self.assertNotIn("seed", view)
                    self.assertNotIn("grimoire", view)
                    # No implicit parent/family map for unrelated players.
                    self.assertTrue(all(set(r) == {"player", "original", "alive"} for r in view["roster"]))
                    self.assertNotIn("recipients", json.dumps(view))
                    self.assertNotIn("root", view["identity"])
            finally:
                store.close()

    def test_append_only_checkpoints_and_action_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                engine = Engine(seed=3)
                store.initialize(Config(), 3, engine, "scripted")
                request = engine.next_request()
                store.save_intent(request, engine.view(request["actor"]), engine.family(request["actor"]))
                action = empty_action()
                engine.apply(request["id"], action)
                store.commit_action(request["id"], action, engine.snapshot(), mode="budget-pass")
                before = store.checkpoint()
                with self.assertRaises(IntegrityError):
                    store.commit_action(request["id"], action, engine.snapshot(), mode="budget-pass")
                self.assertEqual(store.checkpoint(), before)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.db.execute("DELETE FROM checkpoints")
                self.assertTrue(store.verify())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
