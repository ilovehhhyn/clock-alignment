"""The probe's pure rules: Slurm times parse exactly, and a link still loading its model is not a stall."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("probe_game", Path(__file__).resolve().parent.parent / "scripts" / "probe_game.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

QUEUE = """     JOBID       NAME  ST     TIME TIME_LIM NODELIST(REASON)
  17250001   ca-pilot   R    12:34    59:00 della-l09g6
  17250002   ca-pilot  PD     0:00    59:00 (Dependency)
  17250009   ca-pilot   R    40:00    59:00 della-l09g7
17249990       ca-pilot    COMPLETED   00:45:09      0:0
"""


class SlurmSecondsTests(unittest.TestCase):
    def test_squeue_and_sacct_forms_parse_exactly(self):
        self.assertEqual(probe.slurm_seconds("0:25"), 25)
        self.assertEqual(probe.slurm_seconds("12:34"), 12 * 60 + 34)
        self.assertEqual(probe.slurm_seconds("1:02:03"), 3600 + 2 * 60 + 3)
        self.assertEqual(probe.slurm_seconds("1-00:00:10"), 24 * 3600 + 10)

    def test_anything_else_is_an_error(self):
        for text in ("", "25", "a:b", "UNLIMITED", "1-2", "x-00:00:10"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                probe.slurm_seconds(text)


class ChainJobsTests(unittest.TestCase):
    def test_only_the_chain_file_ids_are_read_with_their_elapsed_time(self):
        with tempfile.TemporaryDirectory() as local:
            (Path(local) / "chain.txt").write_text("17250001\n17250002\n17249990\n")
            jobs = probe.chain_jobs(QUEUE, Path(local))
        self.assertEqual(jobs, [("17250001", "R", 12 * 60 + 34), ("17250002", "PD", 0),
                                ("17249990", "COMPLETED", 45 * 60 + 9)])


class UnreachableTests(unittest.TestCase):
    # Invariant: a failed login-node command stops the probe before any sync or report, so stale data is never shown as fresh.
    def test_the_ssh_failure_text_marks_the_login_node_unreachable(self):
        self.assertTrue(probe.unreachable("[ssh exit 255] Connection closed by UNKNOWN port 65535"))
        self.assertFalse(probe.unreachable(QUEUE))
        self.assertFalse(probe.unreachable("no ledger yet\n"))


class StallTests(unittest.TestCase):
    # Invariant: a stall needs a link past its load phase, no decision for STALL_SECONDS, and an unchanged count.
    # Witness: the straddle a 20-minute cadence produces (link paused, next link started 5 minutes ago).
    def test_a_link_that_just_started_is_not_stalled_however_long_the_ledger_was_idle(self):
        self.assertFalse(probe.stalled(running_seconds=5 * 60, idle_seconds=19 * 60, requests_unchanged=True))

    def test_a_long_running_link_with_no_decision_and_an_unchanged_count_is_stalled(self):
        self.assertTrue(probe.stalled(running_seconds=30 * 60, idle_seconds=probe.STALL_SECONDS + 1, requests_unchanged=True))

    def test_new_decisions_or_a_recent_decision_mean_no_stall(self):
        self.assertFalse(probe.stalled(running_seconds=30 * 60, idle_seconds=probe.STALL_SECONDS + 1, requests_unchanged=False))
        self.assertFalse(probe.stalled(running_seconds=30 * 60, idle_seconds=probe.STALL_SECONDS - 1, requests_unchanged=True))


if __name__ == "__main__":
    unittest.main()
