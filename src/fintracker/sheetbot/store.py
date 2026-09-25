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
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL,
                recorded_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
                data TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER NOT NULL, description TEXT NOT NULL, category_id TEXT NOT NULL,
                PRIMARY KEY(user_id, description)
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

    def record(self, record_id: int, user_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT data FROM records WHERE id=? AND user_id=?", (record_id, user_id)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_record(
        self, record_id: int, user_id: int, data: dict[str, Any], timestamp: int
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(id) "
                "DO UPDATE SET data=excluded.data",
                (record_id, user_id, json.dumps(data, ensure_ascii=False), timestamp),
            )

    def recent(self, user_id: int, timestamp: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM records WHERE user_id=? AND recorded_at>=? "
            "AND recorded_at<=? ORDER BY id DESC LIMIT 30",
            (user_id, timestamp - 300, timestamp),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def prompt(self, prompt_id: int, user_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT kind,data FROM prompts WHERE id=? AND user_id=? AND active=1",
            (prompt_id, user_id),
        ).fetchone()
        return {"kind": row[0], "data": json.loads(row[1])} if row else None

    def save_prompt(self, prompt_id: int, user_id: int, kind: str, data: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO prompts VALUES (?,?,?,?,1)",
                (prompt_id, user_id, kind, json.dumps(data, ensure_ascii=False)),
            )

    def close_prompt(self, prompt_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE prompts SET active=0 WHERE id=?", (prompt_id,))

    def learn(self, user_id: int, description: str, category_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO preferences VALUES (?,?,?) ON CONFLICT(user_id,description) "
                "DO UPDATE SET category_id=excluded.category_id",
                (user_id, description.casefold().strip()[:300], category_id),
            )

    def preferences(self, user_id: int) -> list[dict[str, str]]:
        rows = self.db.execute(
            "SELECT description,category_id FROM preferences WHERE user_id=? "
            "ORDER BY rowid DESC LIMIT 100",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
