"""Single-host durable coordinator. Models receive views, never database access.

SQLite's IMMEDIATE transactions arbitrate budgets even across processes. The runner
also holds a process lock: a second orchestrator must not advance the same game.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from .config import (Config, INPUT_NANO_PER_TOKEN, NANODOLLARS,
                     OUTPUT_NANO_PER_TOKEN, canonical, digest)


class BudgetLimit(RuntimeError):
    pass


class PendingCall(RuntimeError):
    pass


class IntegrityError(RuntimeError):
    pass


def code_fingerprint():
    base = Path(__file__).parent
    return digest({str(p.relative_to(base)): p.read_text() for p in sorted(base.rglob("*.py"))})


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "experiment.sqlite"
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, token_cap INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS daily_balances(
            family TEXT NOT NULL, day INTEGER NOT NULL, tokens INTEGER NOT NULL,
            PRIMARY KEY(family,day));
          CREATE TABLE IF NOT EXISTS checkpoints(
            seq INTEGER PRIMARY KEY, parent_hash TEXT NOT NULL, hash TEXT NOT NULL,
            state_json TEXT NOT NULL, request_id TEXT UNIQUE, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS intents(
            id TEXT PRIMARY KEY, actor TEXT NOT NULL, family TEXT NOT NULL,
            request_json TEXT NOT NULL, view_json TEXT NOT NULL, view_hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS calls(
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, actor TEXT NOT NULL, family TEXT,
            status TEXT NOT NULL, output_cap INTEGER NOT NULL, input_cap INTEGER NOT NULL,
            reservation_nano INTEGER NOT NULL, charge_nano INTEGER NOT NULL,
            request_json TEXT NOT NULL, response_json TEXT, output_tokens INTEGER,
            input_tokens INTEGER, reasoning_tokens INTEGER, elapsed REAL, error TEXT,
            created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS decisions(
            id TEXT PRIMARY KEY REFERENCES intents(id), action_json TEXT NOT NULL,
            call_id TEXT REFERENCES calls(id), mode TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS faults(
            seq INTEGER PRIMARY KEY, category TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS annotations(
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_id TEXT NOT NULL,
            result_json TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS outcomes(
            seq INTEGER PRIMARY KEY, result_json TEXT NOT NULL, created REAL NOT NULL);
        """)
        for table in ("meta", "families", "daily_balances", "checkpoints", "intents", "decisions", "faults", "annotations", "outcomes"):
            for operation in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} "
                                f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable log'); END")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS immutable_calls_delete BEFORE DELETE ON calls "
                        "BEGIN SELECT RAISE(ABORT, 'immutable call ledger'); END")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    @contextmanager
    def runner_lock(self):
        path = self.directory / "runner.lock"
        with path.open("a+") as handle:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another runner owns this experiment") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def initialized(self):
        return self.db.execute("SELECT 1 FROM meta WHERE key='config'").fetchone() is not None

    def initialize(self, config, seed, engine, provider):
        with self.transaction():
            if self.initialized():
                raise FileExistsError("Experiment already exists; resume it explicitly")
            metadata = {"config": config.to_dict(), "seed": seed, "provider": provider,
                        "code_fingerprint": code_fingerprint(), "created": time.time(),
                        "schema_version": 1}
            for key, value in metadata.items():
                self.db.execute("INSERT INTO meta VALUES (?,?)", (key, canonical(value)))
            for root in engine.roots:
                self.db.execute("INSERT INTO families VALUES (?,?)", (root, config.family_tokens))
            self._checkpoint(engine.snapshot(), None)

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise IntegrityError(f"Missing experiment metadata: {key}")
        return json.loads(row[0])

    @property
    def config(self):
        return Config(**self.meta("config"))

    def verify(self, check_code=True):
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise IntegrityError("SQLite integrity check failed")
        if check_code and self.meta("code_fingerprint") != code_fingerprint():
            raise IntegrityError("Code changed since this experiment was created; do not mix protocols on resume")
        parent = "genesis"
        for row in self.db.execute("SELECT * FROM checkpoints ORDER BY seq"):
            expected = digest({"parent": parent, "state": json.loads(row["state_json"]),
                               "request_id": row["request_id"]})
            if row["parent_hash"] != parent or row["hash"] != expected:
                raise IntegrityError(f"Checkpoint chain altered at {row['seq']}")
            parent = row["hash"]
        for row in self.db.execute("SELECT * FROM intents"):
            if digest(json.loads(row["view_json"])) != row["view_hash"]:
                raise IntegrityError(f"Agent view changed: {row['id']}")
        return True

    def _checkpoint(self, snapshot, request_id):
        previous = self.db.execute("SELECT seq,hash FROM checkpoints ORDER BY seq DESC LIMIT 1").fetchone()
        seq, parent = (previous[0] + 1, previous[1]) if previous else (0, "genesis")
        h = digest({"parent": parent, "state": snapshot, "request_id": request_id})
        self.db.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
                        (seq, parent, h, canonical(snapshot), request_id, time.time()))

    def checkpoint(self):
        row = self.db.execute("SELECT state_json FROM checkpoints ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            raise IntegrityError("No checkpoint")
        return json.loads(row[0])

    def save_intent(self, request, view, family):
        with self.transaction():
            values = (request["id"], request["actor"], family, canonical(request), canonical(view), digest(view))
            old = self.db.execute("SELECT * FROM intents WHERE id=?", (request["id"],)).fetchone()
            if old:
                if tuple(old) != values:
                    raise IntegrityError("Request/view changed during deterministic replay")
            else:
                self.db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?)", values)

    def decision(self, request_id):
        row = self.db.execute("SELECT action_json FROM decisions WHERE id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def commit_action(self, request_id, action, snapshot, call_id=None, mode="model"):
        with self.transaction():
            if self.decision(request_id) is not None:
                raise IntegrityError("Action already committed")
            self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?)",
                            (request_id, canonical(action), call_id, mode, time.time()))
            self._checkpoint(snapshot, request_id)

    def commit_terminal(self, snapshot):
        with self.transaction():
            if digest(self.checkpoint()) != digest(snapshot):
                self._checkpoint(snapshot, None)

    def remaining(self, family):
        cap = self.db.execute("SELECT token_cap FROM families WHERE id=?", (family,)).fetchone()
        if not cap:
            raise IntegrityError("Unknown lineage")
        used = self.db.execute("SELECT COALESCE(SUM(CASE WHEN status='completed' THEN output_tokens "
                               "ELSE output_cap END),0) FROM calls WHERE scope='game' AND family=?", (family,)).fetchone()[0]
        return cap[0] - used

    def daily_balance(self, family, day):
        # A live shared balance could become a covert daytime parent-child
        # communication channel through chosen output lengths. Show only the
        # balance fixed at the beginning of this night, through the next day.
        with self.transaction():
            row = self.db.execute("SELECT tokens FROM daily_balances WHERE family=? AND day=?", (family, day)).fetchone()
            if row:
                return row[0]
            tokens = self.remaining(family)
            self.db.execute("INSERT INTO daily_balances VALUES (?,?,?)", (family, day, tokens))
            return tokens

    def call(self, call_id):
        row = self.db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return dict(row) if row else None

    def reserve(self, call_id, scope, actor, family, output_cap, payload):
        if scope not in {"game", "probe", "judge"}:
            raise ValueError("Unknown call scope")
        config = self.config
        if type(output_cap) is not int or not 0 < output_cap <= config.max_output_tokens:
            raise ValueError("Output cap outside frozen geometry")
        if (scope == "game") != (family is not None):
            raise ValueError("Gameplay must charge exactly one lineage; analysis must not alter it")
        with self.transaction():
            if self.call(call_id):
                raise PendingCall("Call already recorded; reuse its saved response or stop, never resend")
            rows = self.db.execute("SELECT COUNT(*),COALESCE(SUM(charge_nano),0) FROM calls").fetchone()
            if rows[0] >= config.max_paid_calls:
                raise BudgetLimit("Overall generation-call cap reached")
            reservation = config.max_input_tokens * INPUT_NANO_PER_TOKEN + output_cap * OUTPUT_NANO_PER_TOKEN
            if rows[1] + reservation > config.dollar_limit * NANODOLLARS:
                raise BudgetLimit("Overall dollar ceiling reached")
            if scope == "game":
                if self.remaining(family) < output_cap:
                    raise BudgetLimit("Lineage output allowance exceeded")
            else:
                analysis = self.db.execute("SELECT COALESCE(SUM(charge_nano),0), "
                    "COALESCE(SUM(CASE WHEN status='completed' THEN output_tokens ELSE output_cap END),0) "
                    "FROM calls WHERE scope!='game'").fetchone()
                if analysis[0] + reservation > config.analysis_dollar_limit * NANODOLLARS:
                    raise BudgetLimit("Analysis dollar ceiling reached")
                if analysis[1] + output_cap > config.analysis_output_tokens:
                    raise BudgetLimit("Analysis generated-token ceiling reached")
                if scope == "probe" and self.db.execute("SELECT COUNT(*) FROM calls WHERE scope='probe'").fetchone()[0] >= config.max_probes:
                    raise BudgetLimit("Probe count ceiling reached")
            self.db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, scope, actor, family, "reserved", output_cap, config.max_input_tokens,
                 reservation, reservation, canonical(payload), None, None, None, None, None, None, time.time()))

    def finish(self, call_id, response, elapsed):
        """Receipt first. Even malformed/capped output remains paid and available to audit."""
        with self.transaction():
            old = self.call(call_id)
            if not old or old["status"] != "reserved":
                raise IntegrityError("Only a reserved request can be settled")
            usage_value = response.get("usage")
            usage = usage_value if isinstance(usage_value, dict) else {}
            details_value = usage.get("output_tokens_details", {})
            details = details_value if isinstance(details_value, dict) else {}
            ni, no = usage.get("input_tokens"), usage.get("output_tokens")
            nr = details.get("reasoning_tokens", 0)
            valid = (isinstance(usage_value, dict) and isinstance(details_value, dict)
                     and type(ni) is int and type(no) is int and type(nr) is int
                     and 0 <= ni <= old["input_cap"] and 0 <= no <= old["output_cap"] and 0 <= nr <= no)
            if valid:
                charge = ni * INPUT_NANO_PER_TOKEN + no * OUTPUT_NANO_PER_TOKEN
                self.db.execute("UPDATE calls SET status='completed',response_json=?,input_tokens=?,output_tokens=?,"
                                "reasoning_tokens=?,charge_nano=?,elapsed=? WHERE id=?",
                                (canonical(response), ni, no, nr, charge, elapsed, call_id))
            else:
                self.db.execute("UPDATE calls SET status='unknown',response_json=?,elapsed=?,error=? WHERE id=?",
                                (canonical(response), elapsed, "Missing or out-of-bounds usage; full reservation retained", call_id))
        if not valid:
            raise IntegrityError("Provider usage missing or exceeds the reservation; stopped for reconciliation")

    def fail(self, call_id, category):
        with self.transaction():
            old = self.call(call_id)
            if old and old["status"] == "reserved":
                self.db.execute("UPDATE calls SET status='unknown',error=? WHERE id=?", (category, call_id))

    def fault(self, category, detail):
        with self.transaction():
            self.db.execute("INSERT INTO faults(category,detail,created) VALUES (?,?,?)", (category, detail, time.time()))

    def annotate(self, key, scope, source_id, result):
        with self.transaction():
            self.db.execute("INSERT INTO annotations VALUES (?,?,?,?,?)", (key, scope, source_id, canonical(result), time.time()))

    def outcome(self, result):
        with self.transaction():
            self.db.execute("INSERT INTO outcomes(result_json,created) VALUES (?,?)", (canonical(result), time.time()))

    def latest_outcome(self):
        row = self.db.execute("SELECT result_json FROM outcomes ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def accounting(self):
        rows = [dict(r) for r in self.db.execute("SELECT * FROM calls ORDER BY created,id")]
        read, write, estimate = 0, 0, 0
        for row in rows:
            if row["status"] != "completed":
                continue
            usage = json.loads(row["response_json"])["usage"]
            details = usage.get("input_tokens_details") or {}
            if not isinstance(details, dict):
                details = {}
            cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
            if (type(cached) is not int or type(written) is not int
                    or min(cached, written) < 0 or cached + written > row["input_tokens"]):
                # Raw receipt remains available; never invent plausible cache usage.
                cached, written = 0, row["input_tokens"]
            read += cached
            write += written
            estimate += ((row["input_tokens"] - cached - written) * 2_000 + cached * 100
                         + written * INPUT_NANO_PER_TOKEN + row["output_tokens"] * OUTPUT_NANO_PER_TOKEN)
        return {"calls": len(rows), "conservative_usd": sum(r["charge_nano"] for r in rows) / NANODOLLARS,
                "held_unknown_usd": sum(r["charge_nano"] for r in rows if r["status"] != "completed") / NANODOLLARS,
                "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
                "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
                "reasoning_tokens": sum(r["reasoning_tokens"] or 0 for r in rows),
                "cache_read_tokens": read, "cache_write_tokens": write,
                "completed_calls_standard_rate_estimate_usd": estimate / NANODOLLARS,
                "remaining_by_lineage": {r[0]: self.remaining(r[0]) for r in self.db.execute("SELECT id FROM families")},
                "scopes": {s: sum(r["charge_nano"] for r in rows if r["scope"] == s) / NANODOLLARS
                           for s in ("game", "probe", "judge")}}
