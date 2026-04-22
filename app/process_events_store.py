"""
Persistencia de eventos de procesos en background — scheduler runs, RSS fetches,
startup/shutdown del servidor y otros eventos del sistema.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))

VALID_STATUSES = frozenset({"ok", "error", "warning", "info"})
VALID_COMPONENTS = frozenset({"scheduler", "ai", "rss", "lifespan", "railway", "system"})

_MAX_MESSAGE_LEN = 2000
_MAX_DETAILS_LEN = 8000


def init_process_events_table() -> None:
    """Create the process_events table if missing."""
    with get_conn() as conn:
        if is_postgres():
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS process_events (
                    id           SERIAL PRIMARY KEY,
                    created_at   TEXT NOT NULL,
                    component    TEXT NOT NULL,
                    event_type   TEXT NOT NULL,
                    status       TEXT NOT NULL,
                    duration_ms  INTEGER,
                    message      TEXT,
                    details_json TEXT
                )
                """,
            )
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS process_events (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at   TEXT NOT NULL,
                    component    TEXT NOT NULL,
                    event_type   TEXT NOT NULL,
                    status       TEXT NOT NULL,
                    duration_ms  INTEGER,
                    message      TEXT,
                    details_json TEXT
                )
                """,
            )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_pe_created ON process_events(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_pe_component ON process_events(component)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_pe_status ON process_events(status)")

    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("Process events table ready — %s", backend)


def log_process_event(
    *,
    component: str,
    event_type: str,
    status: str = "ok",
    duration_ms: int | None = None,
    message: str | None = None,
    details: dict | None = None,
) -> None:
    """Persist a single process event. Swallows errors so it never breaks the caller."""
    status = (status or "ok").lower()
    if status not in VALID_STATUSES:
        status = "info"

    trimmed_msg = message[:_MAX_MESSAGE_LEN] if message else None
    details_json: str | None = None
    if details is not None:
        try:
            encoded = json.dumps(details, default=str, ensure_ascii=False)
            details_json = encoded[:_MAX_DETAILS_LEN]
        except (TypeError, ValueError):
            details_json = None

    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")

    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO process_events
                    (created_at, component, event_type, status, duration_ms, message, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso, component, event_type, status, duration_ms, trimmed_msg, details_json),
            )
    except Exception as exc:
        logger.warning("Failed to log process event: %s", exc)


def _base_filters(
    desde: str | None,
    hasta: str | None,
    component: str | None,
    status: str | None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if desde:
        clauses.append("created_at >= ?")
        params.append(f"{desde}T00:00:00")
    if hasta:
        clauses.append("created_at <= ?")
        params.append(f"{hasta}T23:59:59")
    if component:
        clauses.append("component = ?")
        params.append(component)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_process_events(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    component: str | None = None,
    status: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict]:
    """Return process events ordered by recency."""
    where, params = _base_filters(desde, hasta, component, status)
    sql = f"SELECT * FROM process_events{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    try:
        with get_conn() as conn:
            rows = query(conn, sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("Failed to query process events: %s", exc)
        return []


def count_process_events(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    component: str | None = None,
    status: str | None = None,
) -> int:
    """Return total rows matching the given filters."""
    where, params = _base_filters(desde, hasta, component, status)
    sql = f"SELECT COUNT(*) AS c FROM process_events{where}"
    try:
        with get_conn() as conn:
            row = query(conn, sql, tuple(params)).fetchone()
        if row is None:
            return 0
        return int(row["c"] if hasattr(row, "__getitem__") else row[0])
    except Exception as exc:
        logger.warning("Failed to count process events: %s", exc)
        return 0


def list_known_components() -> list[str]:
    """Return components that appear in the DB, sorted alphabetically."""
    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT DISTINCT component FROM process_events ORDER BY component").fetchall()
        return [r["component"] for r in rows if r["component"]]
    except Exception as exc:
        logger.warning("Failed to list process event components: %s", exc)
        return []


def purge_old_events(days: int = 30) -> int:
    """Delete events older than N days. Returns number of rows affected (best effort)."""
    cutoff = (datetime.now(ART) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            cur = execute(conn, "DELETE FROM process_events WHERE created_at < ?", (cutoff,))
            return getattr(cur, "rowcount", 0) or 0
    except Exception as exc:
        logger.warning("Failed to purge process events: %s", exc)
        return 0
