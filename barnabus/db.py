"""SQLite: per-user moderation records, XP, posted highlights.

Message excerpts in records are encrypted at rest (Fernet, key in record.key
next to the database) and blanked after RecordExcerptDays (default 30). The
record itself — kind, time, moderator — is kept so history counts stay right.
"""
import logging
import os
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

from . import core

_db: sqlite3.Connection | None = None
_fernet: Fernet | None = None
_ENC_PREFIX = "enc:"
KEY_PATH = "record.key"


def _key() -> Fernet:
    """Load the encryption key, creating it on first run (0600)."""
    global _fernet
    if _fernet is None:
        path = core.config.get("RecordKeyPath", KEY_PATH)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(Fernet.generate_key())
            os.chmod(path, 0o600)
            logging.info("Generated record encryption key at %s — back it up; without it stored excerpts are unreadable", path)
        with open(path, "rb") as f:
            _fernet = Fernet(f.read().strip())
    return _fernet


def _enc(text: str) -> str:
    if not text:
        return ""
    return _ENC_PREFIX + _key().encrypt(text.encode("utf-8")).decode("ascii")


def _dec(text: str) -> str:
    if not text or not text.startswith(_ENC_PREFIX):
        return text or ""
    try:
        return _key().decrypt(text[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return "[unreadable: wrong key]"


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
        _migrate_plaintext(_db)
    return _db


def _migrate_plaintext(db: sqlite3.Connection) -> None:
    """Encrypt any detail text written before encryption existed."""
    rows = db.execute("SELECT id, detail FROM user_records WHERE detail != '' AND detail NOT LIKE ?", (_ENC_PREFIX + "%",)).fetchall()
    for rid, detail in rows:
        db.execute("UPDATE user_records SET detail = ? WHERE id = ?", (_enc(detail), rid))
    if rows:
        db.commit()
        logging.info("Encrypted %d existing record excerpt(s)", len(rows))


def add_user_record(user_id: int, kind: str, detail: str = "", moderator_id: int = 0) -> None:
    try:
        db = get_db()
        db.execute(
            "INSERT INTO user_records (user_id, kind, detail, moderator_id, ts) VALUES (?, ?, ?, ?, ?)",
            (int(user_id), kind, _enc((detail or "")[:500]), int(moderator_id), time.time()),
        )
        db.commit()
    except Exception:
        logging.exception("Failed to add user record")


def get_user_records(user_id: int, limit: int = 25) -> list[tuple]:
    try:
        rows = get_db().execute(
            "SELECT kind, detail, moderator_id, ts FROM user_records WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (int(user_id), limit),
        ).fetchall()
        return [(k, _dec(d), m, t) for k, d, m, t in rows]
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


def delete_user_data(user_id: int) -> tuple[int, int]:
    """Erase everything held about a member: records, notes and XP. Returns (records, xp rows)."""
    db = get_db()
    r = db.execute("DELETE FROM user_records WHERE user_id = ?", (int(user_id),)).rowcount
    x = db.execute("DELETE FROM user_xp WHERE user_id = ?", (int(user_id),)).rowcount
    db.commit()
    return r, x


def expire_excerpts() -> int:
    """Blank message excerpts older than RecordExcerptDays. Moderator notes are not
    message content and are kept; the record's kind and time are kept for all."""
    days = int(core.config.get("RecordExcerptDays", 30))
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    db = get_db()
    n = db.execute("UPDATE user_records SET detail = '' WHERE ts < ? AND detail != '' AND kind != 'note'", (cutoff,)).rowcount
    db.commit()
    if n:
        logging.info("Expired %d record excerpt(s) older than %d days", n, days)
    return n
