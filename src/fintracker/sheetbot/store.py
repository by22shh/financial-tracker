"""Small durable inbox and conversation state. Only one worker may consume it."""

import json
import sqlite3
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                prepared TEXT, reply TEXT, done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, pending TEXT
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def enqueue(self, payload: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events(id, payload) VALUES (?, ?)",
                (payload["update_id"], json.dumps(payload)),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('offset', ?)",
                (str(payload["update_id"] + 1),),
            )

    def offset(self) -> int | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key='offset'").fetchone()
        return int(row[0]) if row else None

    def next_event(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM events WHERE done=0 ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    def field(self, event_id: int, name: str) -> Any:
        assert name in {"prepared", "reply"}
        row = self.db.execute(
            "SELECT prepared, reply FROM events WHERE id=?", (event_id,)
        ).fetchone()
        index = 0 if name == "prepared" else 1
        return json.loads(row[index]) if row and row[index] else None

    def save(self, event_id: int, name: str, value: Any) -> None:
        assert name in {"prepared", "reply"}
        with self.db:
            self.db.execute(
                f"UPDATE events SET {name}=? WHERE id=?",  # noqa: S608
                (json.dumps(value, ensure_ascii=False), event_id),
            )

    def finish(self, event_id: int) -> None:
        with self.db:
            # Retain the technical receipt, discard message/audio contents.
            self.db.execute(
                "UPDATE events SET done=1,payload='{}',prepared=NULL,reply=NULL WHERE id=?",
                (event_id,),
            )

    def user(self, user_id: int) -> dict[str, Any]:
        # Old databases may still have sheet_id; it no longer controls the destination.
        row = self.db.execute("SELECT id,pending FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else {"id": user_id, "pending": None}

    def pending(self, user_id: int, text: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO users(id,pending) VALUES (?,?) ON CONFLICT(id) "
                "DO UPDATE SET pending=excluded.pending",
                (user_id, text),
            )
