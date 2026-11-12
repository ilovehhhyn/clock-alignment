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

from .config import Config, NANODOLLARS, canonical, digest

# Standard uncached Sol input, used only for the informational estimate beside the
# conservative charge; reservations always use the profile's conservative input price.
SOL_UNCACHED_INPUT_NANO_PER_TOKEN = 2_000
SOL_CACHED_INPUT_NANO_PER_TOKEN = 100
# Marks a settled call whose answer the Storyteller rejected; its whole output is exempt from allowances.
REJECTED_ANSWER = "rejected answer"
# Marks a settled call whose choices were applied without its message; only its speech is uncounted.
SPEECH_FORFEITED = "speech forfeited"
# Marks a settled call whose answer the Storyteller's confirmation check replaced; its whole output is exempt.
SUPERSEDED = "superseded by confirmation"
UNDELIVERED = (REJECTED_ANSWER, SPEECH_FORFEITED, SUPERSEDED)


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
          CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, token_cap INTEGER);
          CREATE TABLE IF NOT EXISTS daily_balances(
            family TEXT NOT NULL, day INTEGER NOT NULL, tokens INTEGER,
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
            created REAL NOT NULL, session TEXT, exempt_tokens INTEGER,
            message_cap INTEGER, message_tokens INTEGER);
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
                        "schema_version": 3}
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

    def commit_snapshot(self, snapshot):
        """Append a checkpoint for state reached without a new decision (a terminal or a declaration)."""
        with self.transaction():
            if digest(self.checkpoint()) != digest(snapshot):
                self._checkpoint(snapshot, None)

    # Charged tokens: settled output minus its exempt tokens (the judge-log refund, or all of a
    # rejected answer), nothing for a released free call, or the full hold while a call is unsettled.
    CHARGED = ("COALESCE(SUM(CASE WHEN status='completed' THEN output_tokens - COALESCE(exempt_tokens,0) "
               "WHEN status='released' THEN 0 ELSE output_cap END),0)")

    def remaining(self, family):
        """Return the lineage's uncharged tokens, or None when family allocations are off."""
        cap = self.db.execute("SELECT token_cap FROM families WHERE id=?", (family,)).fetchone()
        if not cap:
            raise IntegrityError("Unknown lineage")
        if cap[0] is None:
            return None
        used = self.db.execute(f"SELECT {self.CHARGED} FROM calls WHERE scope='game' AND family=?", (family,)).fetchone()[0]
        return cap[0] - used

    def message_remaining(self, actor, session):
        """Return the spoken tokens the actor may still send in one session, or None when that cap is off."""
        limit = self.config.message_tokens
        if limit is None:
            return None
        # A rejected or silenced answer's speech was never heard, so it leaves the cap alone.
        undelivered = ",".join("?" * len(UNDELIVERED))
        used = self.db.execute("SELECT COALESCE(SUM(message_tokens),0) FROM calls WHERE scope='game' AND actor=? "
                               f"AND session=? AND status='completed' AND COALESCE(error,'') NOT IN ({undelivered})",
                               (actor, session, *UNDELIVERED)).fetchone()[0]
        return limit - used

    def record_message_tokens(self, call_id, tokens):
        """Store once how many spoken tokens a settled gameplay call delivered; a replay must agree."""
        with self.transaction():
            old = self.call(call_id)
            if not old or old["status"] != "completed" or old["scope"] != "game":
                raise IntegrityError("Only a settled gameplay call can carry spoken tokens")
            cap = old["message_cap"]
            if type(tokens) is not int or tokens < 0 or (cap is not None and tokens > cap):
                raise IntegrityError(f"Spoken tokens {tokens!r} outside the call's cap {cap!r}")
            if old["message_tokens"] is not None:
                if old["message_tokens"] != tokens:
                    raise IntegrityError("Spoken tokens changed during deterministic replay")
                return
            self.db.execute("UPDATE calls SET message_tokens=? WHERE id=?", (tokens, call_id))

    def game_spend_nano(self):
        return self.db.execute("SELECT COALESCE(SUM(charge_nano),0) FROM calls WHERE scope='game'").fetchone()[0]

    def game_spend_before_nano(self, day):
        """Return conservative gameplay charges of sessions from days before `day` only."""
        total = 0
        for session, charge in self.db.execute("SELECT session, charge_nano FROM calls WHERE scope='game'"):
            if not isinstance(session, str) or not session.startswith("day-"):
                raise IntegrityError(f"Gameplay call has no day session: {session!r}")
            if int(session.split(":", 1)[0].removeprefix("day-")) < day:
                total += charge
        return total

    def cycle_cost_nano(self, day):
        """Return conservative gameplay charges of one night-and-day cycle, or None if it made no calls."""
        row = self.db.execute("SELECT COUNT(*), COALESCE(SUM(charge_nano),0) FROM calls "
                              "WHERE scope='game' AND session LIKE ?", (f"day-{day}:%",)).fetchone()
        return row[1] if row[0] > 0 else None

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

    def reserve(self, call_id, scope, actor, family, output_cap, payload, session=None, message_cap=None):
        if scope not in {"game", "probe", "judge"}:
            raise ValueError("Unknown call scope")
        config = self.config
        if type(output_cap) is not int or not 0 < output_cap <= config.max_output_tokens:
            raise ValueError("Output cap outside frozen geometry")
        if (scope == "game") != (family is not None) or (scope == "game") != (session is not None):
            raise ValueError("Gameplay must charge exactly one lineage and session; analysis must not alter them")
        if message_cap is not None and (type(message_cap) is not int or message_cap < 0 or scope != "game"):
            raise ValueError(f"message_cap must be a non-negative integer on a gameplay call, got {message_cap!r}")
        if scope == "game" and (message_cap is None) != (config.message_tokens is None):
            raise ValueError(f"a gameplay call carries a message_cap exactly when message_tokens is set; "
                             f"got {message_cap!r} with message_tokens={config.message_tokens!r}")
        with self.transaction():
            if self.call(call_id):
                raise PendingCall("Call already recorded; reuse its saved response or stop, never resend")
            rows = self.db.execute("SELECT COUNT(*),COALESCE(SUM(charge_nano),0) FROM calls").fetchone()
            if rows[0] >= config.max_paid_calls:
                raise BudgetLimit("Overall generation-call cap reached")
            profile = config.profile
            reservation = (config.max_input_tokens * profile.input_nano_per_token
                           + output_cap * profile.output_nano_per_token)
            if rows[1] + reservation > config.dollar_limit * NANODOLLARS:
                raise BudgetLimit("Overall dollar ceiling reached")
            if scope == "game":
                family_left = self.remaining(family)
                if family_left is not None and family_left < output_cap:
                    raise BudgetLimit("Lineage output allowance exceeded")
                if self.game_spend_nano() + reservation > config.game_dollar_limit * NANODOLLARS:
                    raise BudgetLimit(f"Gameplay dollar ceiling (${config.game_dollar_limit}) reached")
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
            self.db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, scope, actor, family, "reserved", output_cap, config.max_input_tokens,
                 reservation, reservation, canonical(payload), None, None, None, None, None, None, time.time(),
                 session, None, message_cap, None))

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
                profile = self.config.profile
                charge = ni * profile.input_nano_per_token + no * profile.output_nano_per_token
                self.db.execute("UPDATE calls SET status='completed',response_json=?,input_tokens=?,output_tokens=?,"
                                "reasoning_tokens=?,charge_nano=?,elapsed=? WHERE id=?",
                                (canonical(response), ni, no, nr, charge, elapsed, call_id))
            else:
                self.db.execute("UPDATE calls SET status='unknown',response_json=?,elapsed=?,error=? WHERE id=?",
                                (canonical(response), elapsed, "Missing or out-of-bounds usage; full reservation retained", call_id))
        if not valid:
            raise IntegrityError("Provider usage missing or exceeds the reservation; stopped for reconciliation")

    def record_exemption(self, call_id, tokens):
        """Store a settled call's judge-log refund once and report whether it was new."""
        with self.transaction():
            old = self.call(call_id)
            if not old or old["status"] != "completed" or old["scope"] != "game":
                raise IntegrityError("Only a settled gameplay call can carry a judge-log refund")
            if type(tokens) is not int or not 0 <= tokens <= old["output_tokens"]:
                raise IntegrityError(f"Judge-log refund {tokens!r} outside the call's output usage")
            if old["error"] == REJECTED_ANSWER:
                return False
            if old["exempt_tokens"] is not None:
                if old["exempt_tokens"] != tokens:
                    raise IntegrityError("Judge-log refund changed during deterministic replay")
                return False
            self.db.execute("UPDATE calls SET exempt_tokens=? WHERE id=?", (tokens, call_id))
            return True

    def exempt_rejected(self, call_id):
        """Stop a settled gameplay call counting against token allowances: its answer was rejected.

        The actor delivered nothing, so the tokens are not charged to its session or family; the
        dollar charge stays. Repeating this is harmless, so a resumed run applies it again.
        """
        with self.transaction():
            old = self.call(call_id)
            if not old or old["status"] != "completed" or old["scope"] != "game":
                raise IntegrityError("Only a settled gameplay call can be marked as a rejected answer")
            self.db.execute("UPDATE calls SET exempt_tokens=output_tokens, error=? WHERE id=?",
                            (REJECTED_ANSWER, call_id))

    def supersede(self, call_id):
        """Stop a settled gameplay call counting against token allowances: the Storyteller's check replaced its answer."""
        with self.transaction():
            old = self.call(call_id)
            if not old or old["status"] != "completed" or old["scope"] != "game":
                raise IntegrityError("Only a settled gameplay call can be superseded")
            if old["error"] not in (None, SUPERSEDED):
                raise IntegrityError(f"Call {call_id} is already marked {old['error']!r}")
            self.db.execute("UPDATE calls SET exempt_tokens=output_tokens, error=? WHERE id=?", (SUPERSEDED, call_id))

    def silence(self, call_id):
        """Record that a settled gameplay call's choices were applied without its message; repeating it is harmless."""
        with self.transaction():
            old = self.call(call_id)
            if not old or old["status"] != "completed" or old["scope"] != "game":
                raise IntegrityError("Only a settled gameplay call can be silenced")
            if old["error"] not in (None, SPEECH_FORFEITED):
                raise IntegrityError(f"Call {call_id} is already marked {old['error']!r}")
            self.db.execute("UPDATE calls SET error=? WHERE id=?", (SPEECH_FORFEITED, call_id))

    def fail(self, call_id, category):
        with self.transaction():
            old = self.call(call_id)
            if old and old["status"] == "reserved":
                self.db.execute("UPDATE calls SET status='unknown',error=? WHERE id=?", (category, call_id))

    def release(self, call_id, category):
        """Mark a failed free (local) call released: it delivered nothing, so it holds no allowance."""
        with self.transaction():
            old = self.call(call_id)
            if old is None or old["charge_nano"] != 0 or old["reservation_nano"] != 0:
                raise IntegrityError("Only a zero-cost call can release its allowance hold")
            if old["status"] in {"reserved", "unknown"}:
                self.db.execute("UPDATE calls SET status='released',error=? WHERE id=?", (category, call_id))

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
            profile = self.config.profile
            uncached_rate = SOL_UNCACHED_INPUT_NANO_PER_TOKEN if profile.input_nano_per_token else 0
            cached_rate = SOL_CACHED_INPUT_NANO_PER_TOKEN if profile.input_nano_per_token else 0
            estimate += ((row["input_tokens"] - cached - written) * uncached_rate + cached * cached_rate
                         + written * profile.input_nano_per_token + row["output_tokens"] * profile.output_nano_per_token)
        return {"calls": len(rows), "conservative_usd": sum(r["charge_nano"] for r in rows) / NANODOLLARS,
                "held_unknown_usd": sum(r["charge_nano"] for r in rows if r["status"] != "completed") / NANODOLLARS,
                "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
                "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
                "reasoning_tokens": sum(r["reasoning_tokens"] or 0 for r in rows),
                "judge_log_refunded_tokens": sum(r["exempt_tokens"] or 0 for r in rows
                                                 if r["error"] not in (REJECTED_ANSWER, SUPERSEDED)),
                "rejected_answer_tokens": sum(r["exempt_tokens"] or 0 for r in rows
                                              if r["error"] == REJECTED_ANSWER),
                "superseded_answer_tokens": sum(r["exempt_tokens"] or 0 for r in rows
                                                if r["error"] == SUPERSEDED),
                "message_tokens": sum(r["message_tokens"] or 0 for r in rows),
                "cache_read_tokens": read, "cache_write_tokens": write,
                "completed_calls_standard_rate_estimate_usd": estimate / NANODOLLARS,
                "remaining_by_lineage": {r[0]: self.remaining(r[0]) for r in self.db.execute("SELECT id FROM families")},
                "scopes": {s: sum(r["charge_nano"] for r in rows if r["scope"] == s) / NANODOLLARS
                           for s in ("game", "probe", "judge")}}
