"""Versioned, local SQLite facts and tool-only shortcuts; no scripts or cloud sync."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import commands, policy
from .paths import user_data_dir


def normalize_command(text: str) -> str:
    return " ".join(commands.normalize(text)[0].rstrip(".!?").casefold().split())


@dataclass(frozen=True)
class Shortcut:
    command: str
    steps: list[dict]
    confidence: float = 1.0


class MemoryStore:
    def __init__(self, path: Path | None = None, *, retention_days: int = 30, max_entries: int = 500):
        self.path = path or Path(os.environ.get("BRODI_MEMORY_PATH", user_data_dir() / "memory.sqlite3"))
        self.retention_days = retention_days
        self.max_entries = max_entries
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("Memory was created by a newer Brodi version")
            db.executescript(
                "CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL, expires REAL NOT NULL); CREATE TABLE IF NOT EXISTS shortcuts (command TEXT PRIMARY KEY, steps TEXT NOT NULL, updated REAL NOT NULL, expires REAL NOT NULL); PRAGMA user_version=1;"
            )

        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=0.3)
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _expires(self) -> float:
        return time.time() + max(1, self.retention_days) * 86400

    def _capacity(self, db, table: str, key_name: str, key: str) -> bool:
        # No silent eviction: reject new entries when full. Existing entries can be updated.
        return (
            bool(db.execute(f"SELECT 1 FROM {table} WHERE {key_name}=?", (key,)).fetchone())
            or db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] < self.max_entries
        )

    def get_fact(self, key: str) -> str | None:
        with self._db() as db:
            row = db.execute(
                "SELECT value FROM facts WHERE key=? AND expires>?", (key.casefold(), time.time())
            ).fetchone()
        return row[0] if row else None

    def set_fact(self, key: str, value: str, *, confirmed: bool = False) -> None:
        key = key.strip().casefold()
        policy.memory_gate(
            "set_fact", key=key, value=value, confirmed=confirmed, replacing=self.get_fact(key) not in (None, value)
        )
        with self._db() as db:
            if not self._capacity(db, "facts", "key", key):
                raise ValueError("Memory is full. Forget an entry before adding more.")
            db.execute("INSERT OR REPLACE INTO facts VALUES (?,?,?,?)", (key, value, time.time(), self._expires()))

    def store_shortcut(self, command: str, steps: list[dict], *, confirmed: bool = False) -> bool:
        policy.memory_gate("store_shortcut", key=command, value=json.dumps(steps))
        if not steps or any(
            set(step) - {"tool", "args", "success"}
            or not isinstance(step.get("tool"), str)
            or not isinstance(step.get("args", {}), dict)
            for step in steps
        ):
            raise ValueError("Shortcuts must contain tool names and arguments, never executable code")
        key = normalize_command(command)
        with self._db() as db:
            previous = db.execute("SELECT steps,expires FROM shortcuts WHERE command=?", (key,)).fetchone()
            if previous and previous[1] > time.time() and json.loads(previous[0]) != steps:
                policy.memory_gate("store_shortcut", key=command, replacing=True, confirmed=confirmed)
            if not self._capacity(db, "shortcuts", "command", key):
                return False
            db.execute(
                "INSERT OR REPLACE INTO shortcuts VALUES (?,?,?,?)",
                (key, json.dumps(steps), time.time(), self._expires()),
            )
        return True

    def match_shortcut(self, command: str) -> Shortcut | None:
        key = normalize_command(command)
        with self._db() as db:
            rows = db.execute("SELECT command,steps FROM shortcuts WHERE expires>?", (time.time(),)).fetchall()
        for stored, steps in rows:
            if stored == key:
                return Shortcut(stored, json.loads(steps))
        # Only tolerate punctuation/spacing differences, never changed verbs, numbers, or arguments.
        compact = re.sub(r"[^\w]", "", key)
        candidates = [(stored, steps) for stored, steps in rows if re.sub(r"[^\w]", "", stored) == compact]
        if len(candidates) == 1:
            steps = json.loads(candidates[0][1])
            if all(step.get("tool") == "open_app" for step in steps):
                return Shortcut(candidates[0][0], steps, 0.99)
        return None

    def list(self) -> dict[str, list[dict]]:
        with self._db() as db:
            facts = [
                {"key": k, "value": v, "expired": e <= time.time()}
                for k, v, e in db.execute("SELECT key,value,expires FROM facts ORDER BY key")
            ]
            shortcuts = [
                {"command": c, "steps": json.loads(s), "expired": e <= time.time()}
                for c, s, e in db.execute("SELECT command,steps,expires FROM shortcuts ORDER BY command")
            ]
        return {"facts": facts, "shortcuts": shortcuts}

    def delete(self, key: str | None = None, *, confirmed: bool = False) -> int:
        policy.memory_gate("delete", key=key or "all", confirmed=confirmed)
        with self._db() as db:
            if key is None:
                count = db.execute("DELETE FROM facts").rowcount + db.execute("DELETE FROM shortcuts").rowcount
            else:
                count = (
                    db.execute("DELETE FROM facts WHERE key=?", (key.casefold(),)).rowcount
                    + db.execute("DELETE FROM shortcuts WHERE command=?", (normalize_command(key),)).rowcount
                )
        return count
