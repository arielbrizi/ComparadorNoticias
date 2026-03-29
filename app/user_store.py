"""
Persistencia de usuarios — usa las utilidades de conexión de app.db.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.config import ADMIN_EMAILS
from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)


def init_users_table() -> None:
    with get_conn() as conn:
        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                name          TEXT NOT NULL DEFAULT '',
                picture       TEXT NOT NULL DEFAULT '',
                role          TEXT NOT NULL DEFAULT 'user',
                created_at    TEXT NOT NULL,
                last_login_at TEXT NOT NULL
            )
            """,
        )
    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("Users table ready — %s", backend)


def _row_to_dict(row) -> dict:
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "picture": row["picture"],
        "role": row["role"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def _determine_role(email: str) -> str:
    return "admin" if email.lower() in [e.lower() for e in ADMIN_EMAILS] else "user"


def upsert_user(email: str, name: str = "", picture: str = "") -> dict:
    """Create a new user or update last_login for an existing one.

    Returns the user dict.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    role = _determine_role(email)

    with get_conn() as conn:
        existing = query(
            conn, "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if existing:
            execute(
                conn,
                "UPDATE users SET last_login_at = ?, name = ?, picture = ?, role = ? WHERE email = ?",
                (now_iso, name or existing["name"], picture or existing["picture"], role, email),
            )
            updated = query(
                conn, "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
            return _row_to_dict(updated)

        user_id = uuid.uuid4().hex[:16]
        execute(
            conn,
            """INSERT INTO users (id, email, name, picture, role, created_at, last_login_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, email, name, picture, role, now_iso, now_iso),
        )
        created = query(
            conn, "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _row_to_dict(created)


def get_user_by_id(user_id: str) -> dict | None:
    with get_conn() as conn:
        row = query(conn, "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row)


def get_user_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        row = query(
            conn, "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
    return _row_to_dict(row)


def list_users(limit: int = 50, offset: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = query(
            conn,
            "SELECT * FROM users ORDER BY last_login_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_users() -> int:
    with get_conn() as conn:
        row = query(conn, "SELECT COUNT(*) as cnt FROM users").fetchone()
    return row["cnt"] if row else 0
