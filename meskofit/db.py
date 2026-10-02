"""SQLite storage. One short-lived connection per call keeps threads independent (WAL mode)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Each entry upgrades the schema by one version (PRAGMA user_version). Identical to the Go app,
# so a meskofit.db made by either implementation opens in the other.
MIGRATIONS = [
    """CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE body_log (
        date TEXT PRIMARY KEY,
        weight_kg REAL, waist_cm REAL, neck_cm REAL, hip_cm REAL, chest_cm REAL, arm_cm REAL, thigh_cm REAL,
        body_fat_pct REAL, note TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE foods (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        barcode TEXT,
        name TEXT NOT NULL,
        brand TEXT,
        data TEXT NOT NULL,
        favorite INTEGER NOT NULL DEFAULT 0,
        custom INTEGER NOT NULL DEFAULT 0,
        fetched_at TEXT NOT NULL,
        UNIQUE(source, source_id)
    );
    CREATE INDEX foods_barcode ON foods(barcode);

    CREATE TABLE food_log (
        id INTEGER PRIMARY KEY,
        date TEXT NOT NULL,
        meal TEXT NOT NULL,
        food_id INTEGER,
        name TEXT NOT NULL,
        brand TEXT,
        amount REAL NOT NULL,
        unit TEXT NOT NULL,
        unit_label TEXT,
        grams REAL,
        nutrients TEXT NOT NULL,
        source TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX food_log_date ON food_log(date);

    CREATE TABLE water_log (date TEXT PRIMARY KEY, ml REAL NOT NULL);

    CREATE TABLE fasts (
        id INTEGER PRIMARY KEY,
        start_at TEXT NOT NULL,
        end_at TEXT,
        target_hours REAL NOT NULL
    );

    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        day_id TEXT NOT NULL,
        day_name TEXT NOT NULL,
        level TEXT NOT NULL,
        date TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        data TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    );
    CREATE INDEX sessions_date ON sessions(date);

    CREATE TABLE sets (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        plan_ex_id TEXT NOT NULL,
        ex_key TEXT NOT NULL,
        set_no INTEGER NOT NULL,
        weight_kg REAL,
        reps INTEGER,
        secs INTEGER,
        done INTEGER NOT NULL DEFAULT 1,
        logged_at TEXT NOT NULL,
        UNIQUE(session_id, plan_ex_id, set_no)
    );
    CREATE INDEX sets_ex ON sets(ex_key);

    CREATE TABLE activities (
        id INTEGER PRIMARY KEY,
        date TEXT NOT NULL,
        kind TEXT NOT NULL,
        minutes REAL NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX activities_date ON activities(date);

    CREATE TABLE photos (
        id INTEGER PRIMARY KEY,
        date TEXT NOT NULL,
        file TEXT NOT NULL,
        thumb TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    );""",
]


def now() -> str:
    """UTC timestamp, RFC 3339 with a Z suffix and whole seconds, like the Go app writes."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nz(s: str | None) -> str | None:
    """Empty or blank strings are stored as NULL."""
    return s if s is not None and s.strip() != "" else None


class Database:
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "meskofit.db"
        self._write_lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _migrate(self) -> None:
        c = self._connect()
        try:
            v = c.execute("PRAGMA user_version").fetchone()[0]
            for i in range(v, len(MIGRATIONS)):
                c.execute("BEGIN")
                try:
                    for stmt in _split(MIGRATIONS[i]):
                        c.execute(stmt)
                    c.execute(f"PRAGMA user_version = {i + 1}")
                    c.execute("COMMIT")
                except Exception as e:
                    c.execute("ROLLBACK")
                    raise RuntimeError(f"migration {i + 1}: {e}") from e
        finally:
            c.close()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """A connection for reads or single statements (autocommit)."""
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction; commits on success, rolls back on error. Writers are serialized."""
        with self._write_lock:
            c = self._connect()
            try:
                c.execute("BEGIN IMMEDIATE")
                try:
                    yield c
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
                else:
                    c.execute("COMMIT")
            finally:
                c.close()

    # ── convenience ──
    def query(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute(sql, args).fetchone()

    def exec(self, sql: str, *args: Any) -> int:
        """Run one write statement; returns lastrowid."""
        with self.tx() as c:
            return c.execute(sql, args).lastrowid or 0

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key = ?", key)
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self.exec(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    def backup_to(self, dest: Path | str) -> None:
        """A consistent single-file copy of the database (VACUUM INTO)."""
        with self.conn() as c:
            c.execute("VACUUM INTO ?", (str(dest),))


def _split(script: str) -> list[str]:
    """Split a migration script on semicolons (none of ours appear inside string literals)."""
    return [s.strip() for s in script.split(";") if s.strip()]
