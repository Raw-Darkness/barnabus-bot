"""SQLite: per-user moderation records, XP, posted highlights."""
import logging
import sqlite3
import time

from . import core

_db: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(core.config.get("DatabasePath", "barnabus.db"))
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute(
            """CREATE TABLE IF NOT EXISTS user_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT DEFAULT '',
                moderator_id INTEGER DEFAULT 0,
                ts REAL NOT NULL
            )"""
        )
        _db.execute("CREATE INDEX IF NOT EXISTS idx_user_records_user ON user_records(user_id)")
        _db.execute("CREATE TABLE IF NOT EXISTS highlights (message_id INTEGER PRIMARY KEY, ts REAL NOT NULL)")
        _db.execute(
            """CREATE TABLE IF NOT EXISTS user_xp (
                user_id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                last_award REAL NOT NULL DEFAULT 0
            )"""
        )
        _db.commit()
    return _db


def add_user_record(user_id: int, kind: str, detail: str = "", moderator_id: int = 0) -> None:
    try:
        db = get_db()
        db.execute(
            "INSERT INTO user_records (user_id, kind, detail, moderator_id, ts) VALUES (?, ?, ?, ?, ?)",
            (int(user_id), kind, (detail or "")[:500], int(moderator_id), time.time()),
        )
        db.commit()
    except Exception:
        logging.exception("Failed to add user record")


def get_user_records(user_id: int, limit: int = 25) -> list[tuple]:
    try:
        return get_db().execute(
            "SELECT kind, detail, moderator_id, ts FROM user_records WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (int(user_id), limit),
        ).fetchall()
    except Exception:
        logging.exception("Failed to read user records")
        return []


def summarize_user_record_counts(user_id: int) -> str:
    try:
        rows = get_db().execute(
            "SELECT kind, COUNT(*) FROM user_records WHERE user_id = ? GROUP BY kind ORDER BY COUNT(*) DESC",
            (int(user_id),),
        ).fetchall()
        return ", ".join(f"{n}× {kind.replace('_', ' ')}" for kind, n in rows)
    except Exception:
        logging.exception("Failed to summarize user records")
        return ""
