"""Persistent state: conversations, schedules and stored report runs.

Deliberately sqlite3 from the standard library. This runs on a Photon VM that
should not need a package index to remember what you asked five minutes ago,
and the whole dataset is a few thousand rows of text.

A connection is opened per operation rather than shared. SQLite connections are
not safe to pass between threads by default, and FastAPI will happily run
handlers on different ones; a per-call connection removes the question. WAL is
enabled so the scheduler writing a run cannot block the UI reading one.

Times are stored as ISO-8601 UTC strings. Not local time: a schedule that
silently shifts by an hour twice a year is a bug that takes months to notice.
"""
import contextlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db")
DB_PATH = os.getenv("STATE_DB", DEFAULT_DB)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS schedules (
    id          TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    model       TEXT,
    scope       TEXT NOT NULL DEFAULT 'all',
    kind        TEXT NOT NULL,
    hour        INTEGER NOT NULL DEFAULT 0,
    minute      INTEGER NOT NULL DEFAULT 0,
    weekday     INTEGER,
    enabled     INTEGER NOT NULL DEFAULT 1,
    next_run    TEXT NOT NULL,
    last_run    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    schedule_id  TEXT,
    question     TEXT NOT NULL,
    answer       TEXT,
    model        TEXT,
    scope        TEXT,
    status       TEXT NOT NULL,
    error        TEXT,
    tools_called TEXT,
    usage        TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, seq);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


_INITIALISED = set()


def connect(path: str = None) -> sqlite3.Connection:
    """Open a connection, creating the schema the first time a path is used.

    Self-initialising on purpose. Relying on a startup hook means any path that
    reaches the database another way fails with "no such table", which reads
    like data loss rather than a missing migration.
    """
    target = path or DB_PATH
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if target not in _INITIALISED:
        conn.executescript(SCHEMA)
        conn.commit()
        _INITIALISED.add(target)
    return conn


@contextlib.contextmanager
def session(path: str = None):
    """A connection that commits on success and always closes.

    ``with sqlite3.connect(...)`` commits but does not close, which leaks a file
    descriptor per call. That is invisible in a test run and fatal in a service
    that stays up for weeks.
    """
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: str = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA)


# --- conversations -----------------------------------------------------------

def create_conversation(title: str = "", path: str = None) -> str:
    cid = new_id()
    now = utcnow()
    with session(path) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)", (cid, title[:120], now, now))
    return cid


def add_message(conversation_id: str, role: str, content: str,
                path: str = None) -> int:
    """Append a message, returning its sequence number.

    The sequence is allocated inside the transaction that writes the row, so two
    requests racing on the same conversation cannot both claim the same seq and
    silently drop one of the messages.
    """
    with session(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (conversation_id, content[:120] if role == "user" else "",
             utcnow(), utcnow()))
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM messages"
            " WHERE conversation_id = ?", (conversation_id,)).fetchone()
        seq = row["next"]
        conn.execute(
            "INSERT INTO messages (conversation_id, seq, role, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (conversation_id, seq, role, content, utcnow()))
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                     (utcnow(), conversation_id))
        # Give an untitled conversation its first question as a title.
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND title = ''",
            (content[:120], conversation_id))
    return seq


def history(conversation_id: str, limit_turns: int = 6, path: str = None) -> list:
    """The last N user/assistant exchanges, oldest first.

    Only prose is replayed. Tool calls and their results are deliberately not
    stored or replayed: a single estate question can return 12k tokens of JSON,
    and three of those would push the real question out of the context window.
    The model gets what was asked and what it concluded, which is what a
    follow-up like "and which of those are powered on?" actually needs.
    """
    with session(path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ?"
            " ORDER BY seq DESC LIMIT ?",
            (conversation_id, max(limit_turns, 0) * 2)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def list_conversations(limit: int = 50, path: str = None) -> list:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT c.id, c.title, c.created_at, c.updated_at,"
            "       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS messages"
            " FROM conversations c ORDER BY c.updated_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete_conversation(conversation_id: str, path: str = None) -> None:
    with session(path) as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?",
                     (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


# --- schedules ---------------------------------------------------------------

def create_schedule(question: str, kind: str, hour: int, minute: int,
                    weekday=None, model=None, scope: str = "all",
                    next_run: str = "", path: str = None) -> str:
    sid = new_id()
    with session(path) as conn:
        conn.execute(
            "INSERT INTO schedules (id, question, model, scope, kind, hour, minute,"
            " weekday, enabled, next_run, last_run, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?)",
            (sid, question, model, scope, kind, hour, minute, weekday,
             next_run, utcnow()))
    return sid


def list_schedules(path: str = None) -> list:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT * FROM schedules ORDER BY next_run").fetchall()
    return [dict(r) for r in rows]


def get_schedule(schedule_id: str, path: str = None):
    with session(path) as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?",
                           (schedule_id,)).fetchone()
    return dict(row) if row else None


def due_schedules(now_iso: str, path: str = None) -> list:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?"
            " ORDER BY next_run", (now_iso,)).fetchall()
    return [dict(r) for r in rows]


def mark_schedule_ran(schedule_id: str, next_run: str, path: str = None) -> None:
    with session(path) as conn:
        conn.execute(
            "UPDATE schedules SET last_run = ?, next_run = ? WHERE id = ?",
            (utcnow(), next_run, schedule_id))


def set_schedule_enabled(schedule_id: str, enabled: bool, path: str = None) -> None:
    with session(path) as conn:
        conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?",
                     (1 if enabled else 0, schedule_id))


def delete_schedule(schedule_id: str, path: str = None) -> None:
    with session(path) as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# --- runs --------------------------------------------------------------------

def start_run(question: str, schedule_id=None, model=None, scope: str = "all",
              path: str = None) -> str:
    rid = new_id()
    with session(path) as conn:
        conn.execute(
            "INSERT INTO runs (id, schedule_id, question, model, scope, status,"
            " started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (rid, schedule_id, question, model, scope, utcnow()))
    return rid


def finish_run(run_id: str, answer: str = None, error: str = None,
               tools_called=None, usage=None, path: str = None) -> None:
    with session(path) as conn:
        conn.execute(
            "UPDATE runs SET status = ?, answer = ?, error = ?, tools_called = ?,"
            " usage = ?, finished_at = ? WHERE id = ?",
            ("error" if error else "ok", answer, error,
             json.dumps(tools_called or []), json.dumps(usage or {}),
             utcnow(), run_id))


def list_runs(limit: int = 50, schedule_id=None, path: str = None) -> list:
    query = ("SELECT id, schedule_id, question, model, scope, status, error,"
             " started_at, finished_at, tools_called FROM runs")
    args = []
    if schedule_id:
        query += " WHERE schedule_id = ?"
        args.append(schedule_id)
    query += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    with session(path) as conn:
        rows = conn.execute(query, args).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["tools_called"] = json.loads(item.get("tools_called") or "[]")
        out.append(item)
    return out


def get_run(run_id: str, path: str = None):
    with session(path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["tools_called"] = json.loads(item.get("tools_called") or "[]")
    item["usage"] = json.loads(item.get("usage") or "{}")
    return item


def previous_answer(schedule_id: str, path: str = None):
    """The last successful answer for a schedule.

    Used to tell a recurring job what it said last time, so a daily report can
    describe what changed rather than restating the same 52 VMs every morning.
    """
    with session(path) as conn:
        row = conn.execute(
            "SELECT answer, started_at FROM runs WHERE schedule_id = ?"
            " AND status = 'ok' AND answer IS NOT NULL"
            " ORDER BY started_at DESC LIMIT 1", (schedule_id,)).fetchone()
    return dict(row) if row else None
