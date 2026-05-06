"""SQLite-backed conversation history for @daton_claude_bot.

The previous in-memory `conversations: dict[int, list]` was wiped on every
Railway redeploy, so every git push reset the assistant's context. This
module stores history on the Railway volume mounted at /data so it survives
restarts.

Schema kept deliberately tiny:
    conversations(chat_id INTEGER, role TEXT, content TEXT, ts TEXT, seq INTEGER PRIMARY KEY AUTOINCREMENT)

Public API mirrors what bot.py needed from a defaultdict(list):
    db_load(chat_id)              -> list of {role, content} dicts (chronological)
    db_append(chat_id, role, content)
    db_replace_last(chat_id, role, content)
    db_truncate(chat_id, max_history)
    db_clear(chat_id)             -> for /reset

Content is stored as TEXT. Anthropic supports list-of-blocks content
(e.g. for images), so we JSON-encode any non-string content on write and
JSON-decode on read.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("CONVERSATIONS_DB_PATH", "/data/conversations.db")

# SQLite is fine with multiple readers + one writer. We serialize writes
# behind a lock to avoid "database is locked" under concurrent_updates=True.
_write_lock = threading.Lock()


def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def init_db():
    _ensure_dir()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                seq      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id  INTEGER NOT NULL,
                role     TEXT    NOT NULL,
                content  TEXT    NOT NULL,
                ts       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_chat_seq ON conversations(chat_id, seq)"
        )
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    try:
        yield conn
    finally:
        conn.close()


def _encode(content):
    if isinstance(content, str):
        return content
    return "__JSON__" + json.dumps(content, ensure_ascii=False)


def _decode(content_text):
    if content_text.startswith("__JSON__"):
        try:
            return json.loads(content_text[len("__JSON__"):])
        except Exception:
            return content_text
    return content_text


def db_load(chat_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE chat_id = ? ORDER BY seq ASC",
            (chat_id,),
        ).fetchall()
    return [{"role": r, "content": _decode(c)} for r, c in rows]


def db_append(chat_id: int, role: str, content) -> None:
    enc = _encode(content)
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, enc),
        )


def db_replace_last(chat_id: int, role: str, content) -> None:
    """Replace the most recent row for this chat_id (used after silent
    intermediate replies, e.g. hubspot_search rewrite)."""
    enc = _encode(content)
    with _write_lock, _connect() as conn:
        row = conn.execute(
            "SELECT seq FROM conversations WHERE chat_id = ? ORDER BY seq DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
                (chat_id, role, enc),
            )
        else:
            conn.execute(
                "UPDATE conversations SET role = ?, content = ? WHERE seq = ?",
                (role, enc, row[0]),
            )


def db_truncate(chat_id: int, max_history: int) -> None:
    """Keep only the last `max_history` messages for this chat."""
    if max_history <= 0:
        return
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            DELETE FROM conversations
             WHERE chat_id = ?
               AND seq NOT IN (
                   SELECT seq FROM conversations
                    WHERE chat_id = ?
                    ORDER BY seq DESC LIMIT ?
               )
            """,
            (chat_id, chat_id, max_history),
        )


def db_clear(chat_id: int) -> None:
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
