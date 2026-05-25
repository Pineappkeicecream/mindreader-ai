"""MindReader AI — SQLite persistence layer."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "mindreader.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL DEFAULT 'general',
            model TEXT NOT NULL DEFAULT 'hybrid',
            first_message TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            turn_number INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            final_prompt TEXT NOT NULL,
            summary TEXT DEFAULT '',
            domain TEXT NOT NULL DEFAULT 'general',
            preview_text TEXT DEFAULT '',
            char_count INTEGER DEFAULT 0,
            section_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            deleted_at REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, turn_number);
        CREATE INDEX IF NOT EXISTS idx_prompts_created ON prompts(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_prompts_domain ON prompts(domain);
    """)
    conn.commit()
    conn.close()


# --- Sessions ---

def save_session(session_id: str, domain: str, model: str, first_message: str = "") -> None:
    conn = _connect()
    now = time.time()
    conn.execute(
        """
        INSERT INTO sessions (id, domain, model, first_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            domain = excluded.domain,
            model = excluded.model,
            first_message = CASE
                WHEN sessions.first_message = '' THEN excluded.first_message
                ELSE sessions.first_message
            END,
            updated_at = excluded.updated_at
        """,
        (session_id, domain, model, first_message[:200], now, now),
    )
    conn.commit()
    conn.close()


def update_session(session_id: str) -> None:
    conn = _connect()
    conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id))
    conn.commit()
    conn.close()


def get_session(session_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_sessions(limit: int = 30, offset: int = 0) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        """
        SELECT
            s.id,
            s.domain,
            s.model,
            s.first_message,
            s.created_at,
            s.updated_at,
            COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS turns,
            COUNT(DISTINCT p.id) AS prompt_count
        FROM sessions s
        LEFT JOIN messages m ON m.session_id = s.id
        LEFT JOIN prompts p ON p.session_id = s.id AND p.deleted_at IS NULL
        GROUP BY s.id
        ORDER BY s.updated_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Messages ---

def save_message(session_id: str, role: str, content: str, turn_number: int = 0) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, turn_number, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, turn_number, time.time()),
    )
    conn.commit()
    conn.close()


def get_session_messages(session_id: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content, turn_number FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Prompts ---

def save_prompt(session_id: str, final_prompt: str, summary: str, domain: str, preview_text: str) -> int:
    """Save a generated prompt. Returns the prompt ID."""
    sections = [l for l in final_prompt.split("\n") if l.startswith("## ")]
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO prompts (session_id, final_prompt, summary, domain, preview_text, "
        "char_count, section_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, final_prompt, summary, domain, preview_text[:100],
         len(final_prompt), len(sections), time.time()),
    )
    prompt_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return prompt_id


def get_prompts(limit: int = 20, offset: int = 0, domain: str | None = None) -> list[dict]:
    conn = _connect()
    if domain:
        rows = conn.execute(
            "SELECT id, session_id, summary, domain, preview_text, char_count, section_count, created_at "
            "FROM prompts WHERE deleted_at IS NULL AND domain = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (domain, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, session_id, summary, domain, preview_text, char_count, section_count, created_at "
            "FROM prompts WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_prompt(prompt_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM prompts WHERE id = ? AND deleted_at IS NULL", (prompt_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_prompt(prompt_id: int) -> bool:
    conn = _connect()
    cursor = conn.execute(
        "UPDATE prompts SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (time.time(), prompt_id),
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0


# --- Stats ---

def get_stats() -> dict:
    conn = _connect()
    prompt_count = conn.execute("SELECT COUNT(*) FROM prompts WHERE deleted_at IS NULL").fetchone()[0]
    session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    return {"prompt_count": prompt_count, "session_count": session_count}
