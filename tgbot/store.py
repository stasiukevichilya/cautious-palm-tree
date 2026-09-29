"""SQLite storage. Every session query is scoped by the owner's Telegram id."""
import json
import time

import aiosqlite

from settings import ALLOWED_BACKENDS, DEFAULTS, backend_error

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('pending', 'allowed', 'blocked')),
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    system_prompt TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(tg_id, updated_at);
CREATE TABLE IF NOT EXISTS active_session (
    tg_id INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
"""


class NotFound(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = path
        self.db = None

    async def open(self, initial=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The database holds the bot token and chat history; SQLite gives -wal/-shm the same mode.
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA synchronous = NORMAL")
        # Environment values only seed an empty database; later edits come from the API.
        for key, value in (initial or {}).items():
            if value:
                await self.db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    # settings
    async def settings(self):
        rows = await self.db.execute_fetchall("SELECT key, value FROM settings")
        values = {**DEFAULTS, **{row["key"]: json.loads(row["value"]) for row in rows if row["key"] in DEFAULTS}}
        for kind in ALLOWED_BACKENDS:  # also drop anything disallowed that an older version stored
            values[kind] = {name: url for name, url in values[kind].items() if not backend_error(kind, name, url)}
        return values

    async def update_settings(self, values):
        await self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                                  [(key, json.dumps(value)) for key, value in values.items()])
        await self.db.commit()

    # users
    async def user(self, tg_id):
        rows = await self.db.execute_fetchall("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return dict(rows[0]) if rows else None

    async def register(self, tg_id, username, full_name, status="pending"):
        """Create the user if new; always refresh the display names. Returns (user, created)."""
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, ?)", (tg_id, username, full_name, status, time.time()))
        created = cursor.rowcount == 1
        await self.db.execute("UPDATE users SET username = ?, full_name = ? WHERE tg_id = ?",
                              (username, full_name, tg_id))
        await self.db.commit()
        return await self.user(tg_id), created

    async def set_status(self, tg_id, status):
        cursor = await self.db.execute("UPDATE users SET status = ? WHERE tg_id = ?", (status, tg_id))
        await self.db.commit()
        if cursor.rowcount != 1:
            raise NotFound(tg_id)
        return await self.user(tg_id)

    async def users(self):
        rows = await self.db.execute_fetchall(
            "SELECT u.*, (SELECT COUNT(*) FROM sessions s WHERE s.tg_id = u.tg_id) AS sessions "
            "FROM users u ORDER BY u.created_at")
        return [dict(row) for row in rows]

    # sessions
    async def create_session(self, tg_id, title=None):
        now = time.time()
        count, = (await self.db.execute_fetchall("SELECT COUNT(*) FROM sessions WHERE tg_id = ?", (tg_id,)))[0]
        cursor = await self.db.execute(
            "INSERT INTO sessions (tg_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (tg_id, (title or f"Сессия {count + 1}")[:64], now, now))
        await self.db.execute("INSERT OR REPLACE INTO active_session VALUES (?, ?)", (tg_id, cursor.lastrowid))
        await self.db.commit()
        return await self.session(tg_id, cursor.lastrowid)

    async def session(self, tg_id, session_id):
        rows = await self.db.execute_fetchall(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS messages "
            "FROM sessions s WHERE s.id = ? AND s.tg_id = ?", (session_id, tg_id))
        if not rows:
            raise NotFound(session_id)
        return dict(rows[0])

    async def sessions(self, tg_id):
        rows = await self.db.execute_fetchall(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS messages, "
            "s.id = (SELECT session_id FROM active_session a WHERE a.tg_id = s.tg_id) AS active "
            "FROM sessions s WHERE s.tg_id = ? ORDER BY s.updated_at DESC", (tg_id,))
        return [dict(row) for row in rows]

    async def active_session(self, tg_id, create=False):
        rows = await self.db.execute_fetchall("SELECT session_id FROM active_session WHERE tg_id = ?", (tg_id,))
        if rows:
            return await self.session(tg_id, rows[0]["session_id"])
        return await self.create_session(tg_id) if create else None

    async def switch_session(self, tg_id, session_id):
        session = await self.session(tg_id, session_id)
        await self.db.execute("INSERT OR REPLACE INTO active_session VALUES (?, ?)", (tg_id, session_id))
        await self.db.commit()
        return session

    async def update_session(self, tg_id, session_id, **fields):
        await self.session(tg_id, session_id)
        assert fields.keys() <= {"title", "system_prompt"}
        for key, value in fields.items():
            await self.db.execute(f"UPDATE sessions SET {key} = ? WHERE id = ? AND tg_id = ?",
                                  (value, session_id, tg_id))
        await self.db.commit()
        return await self.session(tg_id, session_id)

    async def delete_session(self, tg_id, session_id):
        """Delete one of the user's sessions; the most recent remaining one becomes active."""
        await self.session(tg_id, session_id)
        await self.db.execute("DELETE FROM sessions WHERE id = ? AND tg_id = ?", (session_id, tg_id))
        rows = await self.db.execute_fetchall(
            "SELECT id FROM sessions WHERE tg_id = ? ORDER BY updated_at DESC LIMIT 1", (tg_id,))
        if rows and not await self.db.execute_fetchall("SELECT 1 FROM active_session WHERE tg_id = ?", (tg_id,)):
            await self.db.execute("INSERT INTO active_session VALUES (?, ?)", (tg_id, rows[0]["id"]))
        await self.db.commit()
        return await self.active_session(tg_id)

    async def clear_session(self, tg_id, session_id):
        await self.session(tg_id, session_id)
        await self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self.db.commit()

    # messages
    async def add_message(self, tg_id, session_id, role, content):
        await self.session(tg_id, session_id)
        now = time.time()
        await self.db.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                              (session_id, role, content, now))
        await self.db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        await self.db.commit()

    async def history(self, tg_id, session_id, max_messages, max_chars):
        """Newest messages that fit both limits, oldest first; never starts with an assistant turn."""
        await self.session(tg_id, session_id)
        rows = await self.db.execute_fetchall(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, max_messages))
        result, total = [], 0
        for row in rows:
            total += len(row["content"])
            if total > max_chars and result:
                break
            result.append({"role": row["role"], "content": row["content"]})
        result.reverse()
        while result and result[0]["role"] == "assistant":
            result.pop(0)
        return result
