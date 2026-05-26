"""MindReader AI — Database persistence layer.

Supports PostgreSQL (production) and SQLite (local development).
Set DATABASE_URL env var for PostgreSQL; falls back to SQLite automatically.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "")

# --- Connection helpers ---

if DATABASE_URL:
    # PostgreSQL mode
    import psycopg2
    import psycopg2.extras

    def _connect():
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn

    def _rows_to_dicts(cursor) -> list[dict]:
        if cursor.description is None:
            return []
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def _row_to_dict(cursor) -> dict | None:
        if cursor.description is None:
            return None
        row = cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    _PH = "%s"  # PostgreSQL placeholder
    _AUTOINCREMENT = "SERIAL"
    _REAL_TYPE = "DOUBLE PRECISION"
    _UPSERT_CONFLICT = """
        INSERT INTO sessions (id, user_id, domain, model, first_message, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(id) DO UPDATE SET
            domain = EXCLUDED.domain,
            model = EXCLUDED.model,
            first_message = CASE
                WHEN sessions.first_message = '' THEN EXCLUDED.first_message
                ELSE sessions.first_message
            END,
            updated_at = EXCLUDED.updated_at
    """
    print("Database: PostgreSQL")

else:
    # SQLite mode (local development)
    DB_PATH = Path(__file__).parent / "mindreader.db"

    def _connect():
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _rows_to_dicts(cursor) -> list[dict]:
        return [dict(r) for r in cursor.fetchall()]

    def _row_to_dict(cursor) -> dict | None:
        row = cursor.fetchone()
        return dict(row) if row else None

    _PH = "?"  # SQLite placeholder
    _AUTOINCREMENT = "INTEGER"
    _REAL_TYPE = "REAL"
    _UPSERT_CONFLICT = """
        INSERT INTO sessions (id, user_id, domain, model, first_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            domain = excluded.domain,
            model = excluded.model,
            first_message = CASE
                WHEN sessions.first_message = '' THEN excluded.first_message
                ELSE sessions.first_message
            END,
            updated_at = excluded.updated_at
    """
    print("Database: SQLite")


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT 'general',
            model TEXT NOT NULL DEFAULT 'hybrid',
            first_message TEXT DEFAULT '',
            created_at {_REAL_TYPE} NOT NULL,
            updated_at {_REAL_TYPE} NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS messages (
            id {_AUTOINCREMENT} PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            turn_number INTEGER DEFAULT 0,
            created_at {_REAL_TYPE} NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS prompts (
            id {_AUTOINCREMENT} PRIMARY KEY,
            session_id TEXT NOT NULL,
            final_prompt TEXT NOT NULL,
            summary TEXT DEFAULT '',
            domain TEXT NOT NULL DEFAULT 'general',
            preview_text TEXT DEFAULT '',
            char_count INTEGER DEFAULT 0,
            section_count INTEGER DEFAULT 0,
            is_public INTEGER DEFAULT 0,
            created_at {_REAL_TYPE} NOT NULL,
            deleted_at {_REAL_TYPE},
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # --- Migrations: add columns to existing tables ---
    try:
        cur.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
        print("Migration: added user_id column to sessions")
    except Exception:
        conn.rollback()  # column already exists, ignore

    try:
        cur.execute("ALTER TABLE prompts ADD COLUMN rating INTEGER DEFAULT 0")
        conn.commit()
        print("Migration: added rating column to prompts")
    except Exception:
        conn.rollback()  # column already exists

    try:
        cur.execute("ALTER TABLE prompts ADD COLUMN is_public INTEGER DEFAULT 0")
        conn.commit()
        print("Migration: added is_public column to prompts")
    except Exception:
        conn.rollback()  # column already exists

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS analytics (
            id {_AUTOINCREMENT} PRIMARY KEY,
            event TEXT NOT NULL,
            path TEXT DEFAULT '',
            referrer TEXT DEFAULT '',
            user_id TEXT DEFAULT '',
            ip_hash TEXT DEFAULT '',
            created_at {_REAL_TYPE} NOT NULL
        )
    """)

    # Create indexes (IF NOT EXISTS works in both SQLite and PostgreSQL 9.5+)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, turn_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_created ON prompts(created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_domain ON prompts(domain)")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS subscribers (
            id {_AUTOINCREMENT} PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            created_at {_REAL_TYPE} NOT NULL
        )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event ON analytics(event, created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_created ON analytics(created_at DESC)")

    conn.commit()
    cur.close()
    conn.close()


# --- Sessions ---

def save_session(session_id: str, domain: str, model: str, first_message: str = "", user_id: str = "") -> None:
    conn = _connect()
    now = time.time()
    conn.cursor().execute(
        _UPSERT_CONFLICT,
        (session_id, user_id, domain, model, first_message[:200], now, now),
    )
    conn.commit()
    conn.close()


def update_session(session_id: str) -> None:
    conn = _connect()
    conn.cursor().execute(
        f"UPDATE sessions SET updated_at = {_PH} WHERE id = {_PH}",
        (time.time(), session_id),
    )
    conn.commit()
    conn.close()


def get_session(session_id: str) -> dict | None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM sessions WHERE id = {_PH}", (session_id,))
    result = _row_to_dict(cur)
    cur.close()
    conn.close()
    return result


def get_sessions(limit: int = 30, offset: int = 0, user_id: str = "") -> list[dict]:
    conn = _connect()
    cur = conn.cursor()
    if user_id:
        cur.execute(
            f"""
            SELECT
                s.id, s.domain, s.model, s.first_message, s.created_at, s.updated_at,
                COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS turns,
                COUNT(DISTINCT p.id) AS prompt_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            LEFT JOIN prompts p ON p.session_id = s.id AND p.deleted_at IS NULL
            WHERE s.user_id = {_PH}
            GROUP BY s.id, s.domain, s.model, s.first_message, s.created_at, s.updated_at
            ORDER BY s.updated_at DESC
            LIMIT {_PH} OFFSET {_PH}
            """,
            (user_id, limit, offset),
        )
    else:
        cur.execute(
            f"""
            SELECT
                s.id, s.domain, s.model, s.first_message, s.created_at, s.updated_at,
                COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS turns,
                COUNT(DISTINCT p.id) AS prompt_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            LEFT JOIN prompts p ON p.session_id = s.id AND p.deleted_at IS NULL
            GROUP BY s.id, s.domain, s.model, s.first_message, s.created_at, s.updated_at
            ORDER BY s.updated_at DESC
            LIMIT {_PH} OFFSET {_PH}
            """,
            (limit, offset),
        )
    result = _rows_to_dicts(cur)
    cur.close()
    conn.close()
    return result


# --- Messages ---

def save_message(session_id: str, role: str, content: str, turn_number: int = 0) -> None:
    conn = _connect()
    conn.cursor().execute(
        f"INSERT INTO messages (session_id, role, content, turn_number, created_at) "
        f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
        (session_id, role, content, turn_number, time.time()),
    )
    conn.commit()
    conn.close()


def get_session_messages(session_id: str) -> list[dict]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT role, content, turn_number FROM messages WHERE session_id = {_PH} ORDER BY id",
        (session_id,),
    )
    result = _rows_to_dicts(cur)
    cur.close()
    conn.close()
    return result


# --- Prompts ---

def save_prompt(session_id: str, final_prompt: str, summary: str, domain: str, preview_text: str) -> int:
    """Save a generated prompt. Returns the prompt ID."""
    sections = [l for l in final_prompt.split("\n") if l.startswith("## ")]
    conn = _connect()
    cur = conn.cursor()
    if DATABASE_URL:
        # PostgreSQL: use RETURNING
        cur.execute(
            "INSERT INTO prompts (session_id, final_prompt, summary, domain, preview_text, "
            "char_count, section_count, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (session_id, final_prompt, summary, domain, preview_text[:100],
             len(final_prompt), len(sections), time.time()),
        )
        prompt_id = cur.fetchone()[0]
    else:
        # SQLite: use lastrowid
        cur.execute(
            "INSERT INTO prompts (session_id, final_prompt, summary, domain, preview_text, "
            "char_count, section_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, final_prompt, summary, domain, preview_text[:100],
             len(final_prompt), len(sections), time.time()),
        )
        prompt_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return prompt_id


def get_prompts(limit: int = 20, offset: int = 0, domain: str | None = None) -> list[dict]:
    conn = _connect()
    cur = conn.cursor()
    if domain:
        cur.execute(
            f"SELECT id, session_id, summary, domain, preview_text, char_count, section_count, is_public, created_at "
            f"FROM prompts WHERE deleted_at IS NULL AND domain = {_PH} ORDER BY created_at DESC LIMIT {_PH} OFFSET {_PH}",
            (domain, limit, offset),
        )
    else:
        cur.execute(
            f"SELECT id, session_id, summary, domain, preview_text, char_count, section_count, is_public, created_at "
            f"FROM prompts WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT {_PH} OFFSET {_PH}",
            (limit, offset),
        )
    result = _rows_to_dicts(cur)
    cur.close()
    conn.close()
    return result


def get_gallery_prompts(limit: int = 30, offset: int = 0, domain: str | None = None) -> list[dict]:
    """Get prompts for the public gallery — ordered by rating then recency."""
    conn = _connect()
    cur = conn.cursor()
    if domain:
        cur.execute(
            f"""SELECT id, session_id, summary, domain, preview_text, char_count, section_count,
                       rating, is_public, created_at
            FROM prompts
            WHERE deleted_at IS NULL AND is_public = 1 AND domain = {_PH} AND char_count >= 500
            ORDER BY rating DESC, created_at DESC
            LIMIT {_PH} OFFSET {_PH}""",
            (domain, limit, offset),
        )
    else:
        cur.execute(
            f"""SELECT id, session_id, summary, domain, preview_text, char_count, section_count,
                       rating, is_public, created_at
            FROM prompts
            WHERE deleted_at IS NULL AND is_public = 1 AND char_count >= 500
            ORDER BY rating DESC, created_at DESC
            LIMIT {_PH} OFFSET {_PH}""",
            (limit, offset),
        )
    result = _rows_to_dicts(cur)
    cur.close()
    conn.close()
    return result


def get_prompt(prompt_id: int) -> dict | None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM prompts WHERE id = {_PH} AND deleted_at IS NULL", (prompt_id,)
    )
    result = _row_to_dict(cur)
    cur.close()
    conn.close()
    return result


def delete_prompt(prompt_id: int) -> bool:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE prompts SET deleted_at = {_PH} WHERE id = {_PH} AND deleted_at IS NULL",
        (time.time(), prompt_id),
    )
    conn.commit()
    affected = cur.rowcount
    cur.close()
    conn.close()
    return affected > 0


def rate_prompt(prompt_id: int, rating: int) -> bool:
    """Rate a prompt: 1 = thumbs up, -1 = thumbs down, 0 = clear."""
    rating = max(-1, min(1, rating))
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE prompts SET rating = {_PH} WHERE id = {_PH} AND deleted_at IS NULL",
        (rating, prompt_id),
    )
    conn.commit()
    affected = cur.rowcount
    cur.close()
    conn.close()
    return affected > 0


def set_prompt_public(prompt_id: int, is_public: bool) -> bool:
    """Publish or unpublish a prompt from the public gallery."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE prompts SET is_public = {_PH} WHERE id = {_PH} AND deleted_at IS NULL",
        (1 if is_public else 0, prompt_id),
    )
    conn.commit()
    affected = cur.rowcount
    cur.close()
    conn.close()
    return affected > 0


# --- Subscribers ---

def add_subscriber(email: str) -> bool:
    """Add an email subscriber. Returns True if new, False if already exists."""
    conn = _connect()
    try:
        conn.cursor().execute(
            f"INSERT INTO subscribers (email, created_at) VALUES ({_PH}, {_PH})",
            (email.lower().strip(), time.time()),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        conn.rollback()
        conn.close()
        return False


def get_subscriber_count() -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM subscribers")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


# --- Analytics ---

def track_event(event: str, path: str = "", referrer: str = "", user_id: str = "", ip_hash: str = "") -> None:
    conn = _connect()
    conn.cursor().execute(
        f"INSERT INTO analytics (event, path, referrer, user_id, ip_hash, created_at) "
        f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
        (event, path[:500], referrer[:500], user_id[:100], ip_hash[:64], time.time()),
    )
    conn.commit()
    conn.close()


def get_analytics(days: int = 7) -> dict:
    """Get analytics summary for the last N days."""
    conn = _connect()
    cur = conn.cursor()
    cutoff = time.time() - (days * 86400)

    # Total page views
    cur.execute(f"SELECT COUNT(*) FROM analytics WHERE event = 'pageview' AND created_at >= {_PH}", (cutoff,))
    pageviews = cur.fetchone()[0]

    # Unique visitors (by user_id)
    cur.execute(f"SELECT COUNT(DISTINCT user_id) FROM analytics WHERE event = 'pageview' AND created_at >= {_PH} AND user_id != ''", (cutoff,))
    unique_visitors = cur.fetchone()[0]

    # Prompts generated
    cur.execute(f"SELECT COUNT(*) FROM analytics WHERE event = 'prompt_generated' AND created_at >= {_PH}", (cutoff,))
    prompts_generated = cur.fetchone()[0]

    # Page views by path
    cur.execute(
        f"SELECT path, COUNT(*) as cnt FROM analytics WHERE event = 'pageview' AND created_at >= {_PH} GROUP BY path ORDER BY cnt DESC LIMIT 10",
        (cutoff,),
    )
    top_pages = _rows_to_dicts(cur)

    # Daily breakdown
    cur.execute(
        f"""SELECT
            CAST((created_at - {_PH}) / 86400 AS INTEGER) as day_offset,
            COUNT(*) as views,
            COUNT(DISTINCT user_id) as visitors
        FROM analytics
        WHERE event = 'pageview' AND created_at >= {_PH}
        GROUP BY day_offset
        ORDER BY day_offset""",
        (cutoff, cutoff),
    )
    daily = _rows_to_dicts(cur)

    # Top referrers
    cur.execute(
        f"SELECT referrer, COUNT(*) as cnt FROM analytics WHERE event = 'pageview' AND created_at >= {_PH} AND referrer != '' GROUP BY referrer ORDER BY cnt DESC LIMIT 10",
        (cutoff,),
    )
    top_referrers = _rows_to_dicts(cur)

    # Events breakdown
    cur.execute(
        f"SELECT event, COUNT(*) as cnt FROM analytics WHERE created_at >= {_PH} GROUP BY event ORDER BY cnt DESC",
        (cutoff,),
    )
    events = _rows_to_dicts(cur)

    cur.close()
    conn.close()
    return {
        "days": days,
        "pageviews": pageviews,
        "unique_visitors": unique_visitors,
        "prompts_generated": prompts_generated,
        "top_pages": top_pages,
        "daily": daily,
        "top_referrers": top_referrers,
        "events": events,
    }


# --- Stats ---

def get_stats() -> dict:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM prompts WHERE deleted_at IS NULL")
    prompt_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sessions")
    session_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM prompts WHERE deleted_at IS NULL AND rating = 1")
    thumbs_up = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM prompts WHERE deleted_at IS NULL AND rating = -1")
    thumbs_down = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {
        "prompt_count": prompt_count,
        "session_count": session_count,
        "thumbs_up": thumbs_up,
        "thumbs_down": thumbs_down,
    }
