import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.plot_run import export_csv, read_calls


class PlotDataTests(unittest.TestCase):
    def test_read_only_export_preserves_unknowns_and_orders_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "experiment.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE calls(id TEXT,scope TEXT,status TEXT,input_tokens INTEGER,
                    output_tokens INTEGER,elapsed REAL,charge_nano INTEGER,created REAL);
            """)
            connection.execute("INSERT INTO meta VALUES (?,?)", ("provider", json.dumps("scripted")))
            connection.executemany("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)", [
                ("later", "probe", "unknown", None, None, None, 150_000_000, 2),
                ("earlier", "game", "completed", 120, 80, 1.5, 1_000_000, 1)])
            connection.commit()
            connection.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            data = read_calls(root)
            destination = export_csv(data, root / "diagnostics" / "runtime.csv")
            self.assertEqual(data["provider"], "scripted")
            self.assertEqual([r["call_id"] for r in data["calls"]], ["earlier", "later"])
            self.assertAlmostEqual(data["calls"][-1]["cumulative_conservative_usd"], .151)
            with destination.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["output_tokens"], "")
            self.assertEqual(rows[-1]["elapsed_seconds"], "")
            self.assertEqual(rows[-1]["status"], "unknown")
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(sqlite3.OperationalError):
                read_calls(temp)
            self.assertFalse((Path(temp) / "experiment.sqlite").exists())

    def test_empty_ledger_exports_header(self):
        with tempfile.TemporaryDirectory() as temp:
            target = export_csv({"provider": "scripted", "calls": []}, Path(temp) / "empty.csv")
            with target.open(newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertIn("cumulative_conservative_usd", reader.fieldnames)
                self.assertEqual(list(reader), [])


if __name__ == "__main__":
    unittest.main()
