"""
Utilidades compartidas de conexión a base de datos.
PostgreSQL (Railway / DATABASE_URL) con fallback a SQLite local.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_use_pg = bool(DATABASE_URL)

if _use_pg:
    import psycopg2
    import psycopg2.extras

_SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "metrics.db"


@contextmanager
def get_conn():
    """Yield a DB connection with auto-commit on success, rollback on error."""
    if _use_pg:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        _SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_SQLITE_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def query(conn, sql: str, params=()):
    """Execute a SELECT and return a cursor whose rows support r['col'] access."""
    if _use_pg:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params)


def execute(conn, sql: str, params=()):
    """Execute a DDL/DML statement."""
    if _use_pg:
        cur = conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur
    return conn.execute(sql, params)


def is_postgres() -> bool:
    return _use_pg
