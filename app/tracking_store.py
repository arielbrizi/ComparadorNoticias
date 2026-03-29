"""
Persistencia de eventos de uso — tracking de features y comportamiento.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)

if is_postgres():
    import psycopg2.extras


def init_tracking_table() -> None:
    with get_conn() as conn:
        if is_postgres():
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id          SERIAL PRIMARY KEY,
                    user_id     TEXT,
                    session_id  TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    event_data  TEXT,
                    created_at  TEXT NOT NULL,
                    ip_address  TEXT,
                    user_agent  TEXT
                )
                """,
            )
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     TEXT,
                    session_id  TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    event_data  TEXT,
                    created_at  TEXT NOT NULL,
                    ip_address  TEXT,
                    user_agent  TEXT
                )
                """,
            )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_ue_created ON usage_events(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_ue_type ON usage_events(event_type)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_ue_user ON usage_events(user_id)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_ue_session ON usage_events(session_id)")
    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("Tracking table ready — %s", backend)


def log_events(
    events: list[dict],
    user_id: str | None = None,
    session_id: str = "",
    ip_address: str = "",
    user_agent: str = "",
) -> int:
    """Insert a batch of tracking events. Returns the number of rows inserted."""
    if not events:
        return 0

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    for ev in events:
        event_type = ev.get("type", "unknown")
        event_data = json.dumps(ev.get("data", {}), ensure_ascii=False) if ev.get("data") else None
        ts = ev.get("ts", now_iso)
        rows.append((user_id, session_id, event_type, event_data, ts, ip_address, user_agent))

    with get_conn() as conn:
        if is_postgres():
            cur = conn.cursor()
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO usage_events
                   (user_id, session_id, event_type, event_data, created_at, ip_address, user_agent)
                   VALUES %s""",
                rows,
            )
            inserted = cur.rowcount
        else:
            conn.executemany(
                """INSERT INTO usage_events
                   (user_id, session_id, event_type, event_data, created_at, ip_address, user_agent)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            inserted = len(rows)

    return inserted


# ── Helpers ──────────────────────────────────────────────────────────────────

def _where_clause(desde: str | None, hasta: str | None) -> tuple[str, list[str]]:
    parts: list[str] = []
    params: list[str] = []
    if desde:
        parts.append("created_at >= ?")
        params.append(f"{desde}T00:00:00")
    if hasta:
        parts.append("created_at < ?")
        params.append(f"{hasta}T23:59:59")
    sql = (" WHERE " + " AND ".join(parts)) if parts else ""
    return sql, params


def _date_expr() -> str:
    return "LEFT(created_at, 10)" if is_postgres() else "SUBSTR(created_at, 1, 10)"


# ── Queries ──────────────────────────────────────────────────────────────────

def query_usage_stats(desde: str | None = None, hasta: str | None = None) -> dict:
    """Aggregate usage stats, optionally filtered by date range."""
    where_sql, params = _where_clause(desde, hasta)

    with get_conn() as conn:
        total_row = query(
            conn,
            f"SELECT COUNT(*) as cnt FROM usage_events{where_sql}",
            params,
        ).fetchone()

        pv_row = query(
            conn,
            f"SELECT COUNT(*) as cnt FROM usage_events{where_sql}"
            f" {'AND' if params else 'WHERE'} event_type = 'page_view'",
            params,
        ).fetchone()

        unique_users = query(
            conn,
            f"SELECT COUNT(DISTINCT user_id) as cnt FROM usage_events{where_sql}"
            f" {'AND' if params else 'WHERE'} user_id IS NOT NULL",
            params,
        ).fetchone()

        unique_sessions = query(
            conn,
            f"SELECT COUNT(DISTINCT session_id) as cnt FROM usage_events{where_sql}",
            params,
        ).fetchone()

    return {
        "total_events": total_row["cnt"] if total_row else 0,
        "page_views": pv_row["cnt"] if pv_row else 0,
        "unique_users": unique_users["cnt"] if unique_users else 0,
        "unique_sessions": unique_sessions["cnt"] if unique_sessions else 0,
    }


def query_feature_usage(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """Ranking of user actions (excludes page_view, which is shown separately)."""
    where_sql, params = _where_clause(desde, hasta)
    exclude = " AND event_type != 'page_view'" if params else " WHERE event_type != 'page_view'"

    with get_conn() as conn:
        rows = query(
            conn,
            f"""SELECT event_type, COUNT(*) as cnt
                FROM usage_events{where_sql}{exclude}
                GROUP BY event_type ORDER BY cnt DESC""",
            params,
        ).fetchall()

    return [{"feature": r["event_type"], "count": r["cnt"]} for r in rows]


def query_popular_searches(limit: int = 20) -> list[dict]:
    """Return top AI search queries by frequency."""
    with get_conn() as conn:
        rows = query(
            conn,
            """SELECT event_data, COUNT(*) as cnt
               FROM usage_events
               WHERE event_type = 'ai_search' AND event_data IS NOT NULL
               GROUP BY event_data ORDER BY cnt DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    results = []
    for r in rows:
        try:
            data = json.loads(r["event_data"])
            q = data.get("query", r["event_data"])
        except (json.JSONDecodeError, TypeError):
            q = r["event_data"]
        results.append({"query": q, "count": r["cnt"]})
    return results


def query_sections_visited(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """Breakdown of page_view events by section name (parsed from event_data.view)."""
    where_sql, params = _where_clause(desde, hasta)
    extra = " AND event_type = 'page_view' AND event_data IS NOT NULL"
    if not params:
        extra = " WHERE event_type = 'page_view' AND event_data IS NOT NULL"

    with get_conn() as conn:
        rows = query(
            conn,
            f"SELECT event_data FROM usage_events{where_sql}{extra}",
            params,
        ).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        try:
            data = json.loads(r["event_data"])
            view = data.get("view", "desconocido")
        except (json.JSONDecodeError, TypeError):
            view = "desconocido"
        counts[view] = counts.get(view, 0) + 1

    return sorted(
        [{"section": k, "count": v} for k, v in counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )


def query_top_content(limit: int = 20) -> list[dict]:
    """Most clicked news groups (from group_click events)."""
    with get_conn() as conn:
        rows = query(
            conn,
            """SELECT event_data, COUNT(*) as cnt
               FROM usage_events
               WHERE event_type = 'group_click' AND event_data IS NOT NULL
               GROUP BY event_data ORDER BY cnt DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    results = []
    for r in rows:
        try:
            data = json.loads(r["event_data"])
            title = data.get("title", data.get("group_id", "?"))
            group_id = data.get("group_id", "")
        except (json.JSONDecodeError, TypeError):
            title = "?"
            group_id = ""
        results.append({"title": title, "group_id": group_id, "count": r["cnt"]})
    return results


def query_engagement(desde: str | None = None, hasta: str | None = None) -> dict:
    """Session-level engagement: avg events/session, bounce rate, avg duration."""
    where_sql, params = _where_clause(desde, hasta)
    de = _date_expr()

    with get_conn() as conn:
        session_stats = query(
            conn,
            f"""SELECT session_id,
                       COUNT(*) as events,
                       MIN(created_at) as first_ts,
                       MAX(created_at) as last_ts,
                       SUM(CASE WHEN event_type = 'page_view' THEN 1 ELSE 0 END) as pvs
                FROM usage_events{where_sql}
                GROUP BY session_id""",
            params,
        ).fetchall()

    if not session_stats:
        return {
            "avg_events_per_session": 0,
            "avg_pages_per_session": 0,
            "bounce_rate": 0,
            "avg_duration_seconds": 0,
            "total_sessions": 0,
        }

    total_sessions = len(session_stats)
    total_events = sum(r["events"] for r in session_stats)
    total_pvs = sum(r["pvs"] for r in session_stats)
    bounces = sum(1 for r in session_stats if r["pvs"] <= 1)

    durations = []
    for r in session_stats:
        try:
            t0 = r["first_ts"].replace("T", " ").replace("Z", "")[:19]
            t1 = r["last_ts"].replace("T", " ").replace("Z", "")[:19]
            fmt = "%Y-%m-%d %H:%M:%S"
            d0 = datetime.strptime(t0, fmt)
            d1 = datetime.strptime(t1, fmt)
            durations.append((d1 - d0).total_seconds())
        except Exception:
            pass

    avg_dur = sum(durations) / len(durations) if durations else 0

    return {
        "avg_events_per_session": round(total_events / total_sessions, 1),
        "avg_pages_per_session": round(total_pvs / total_sessions, 1),
        "bounce_rate": round((bounces / total_sessions) * 100, 1),
        "avg_duration_seconds": round(avg_dur),
        "total_sessions": total_sessions,
    }


def query_daily_activity(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """Sessions, unique users, page views and events per day."""
    where_sql, params = _where_clause(desde, hasta)
    de = _date_expr()

    with get_conn() as conn:
        rows = query(
            conn,
            f"""SELECT {de} as day,
                       COUNT(DISTINCT session_id) as sessions,
                       COUNT(DISTINCT user_id) as users,
                       SUM(CASE WHEN event_type = 'page_view' THEN 1 ELSE 0 END) as page_views,
                       COUNT(*) as events
                FROM usage_events{where_sql}
                GROUP BY {de}
                ORDER BY day DESC LIMIT 90""",
            params,
        ).fetchall()

    return [
        {
            "day": r["day"],
            "sessions": r["sessions"],
            "users": r["users"],
            "page_views": r["page_views"],
            "events": r["events"],
        }
        for r in rows
    ]


def query_hourly_distribution(
    desde: str | None = None, hasta: str | None = None, utc_offset: int = -3,
) -> list[dict]:
    """Events grouped by hour of day (0-23), adjusted to a timezone offset."""
    where_sql, params = _where_clause(desde, hasta)

    if is_postgres():
        hour_expr = "CAST(SUBSTRING(created_at FROM 12 FOR 2) AS INTEGER)"
    else:
        hour_expr = "CAST(SUBSTR(created_at, 12, 2) AS INTEGER)"

    with get_conn() as conn:
        rows = query(
            conn,
            f"""SELECT {hour_expr} as hour, COUNT(*) as events
                FROM usage_events{where_sql}
                GROUP BY {hour_expr}
                ORDER BY hour""",
            params,
        ).fetchall()

    shifted: dict[int, int] = {}
    for r in rows:
        local_hour = (r["hour"] + utc_offset) % 24
        shifted[local_hour] = shifted.get(local_hour, 0) + r["events"]

    return sorted(
        [{"hour": h, "events": c} for h, c in shifted.items()],
        key=lambda x: x["hour"],
    )


def purge_old_events(days: int = 90) -> int:
    """Delete tracking events older than `days`. Returns deleted count."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    with get_conn() as conn:
        cur = execute(conn, "DELETE FROM usage_events WHERE created_at < ?", (cutoff,))
        deleted = cur.rowcount
    if deleted:
        logger.info("Tracking: purged %d events older than %d days", deleted, days)
    return deleted
