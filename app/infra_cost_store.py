"""
Persistencia de snapshots de costo de infraestructura (Railway).

Guardamos un snapshot por cada pull a Railway (cron horario), con el costo
estimado por servicio. Así podemos mostrar un historial diario en el admin
y no depender únicamente del valor "en vivo".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))


def init_infra_cost_table() -> None:
    """Create the infra_cost_snapshot table if missing."""
    with get_conn() as conn:
        if is_postgres():
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS infra_cost_snapshot (
                    id                  SERIAL PRIMARY KEY,
                    fetched_at          TEXT NOT NULL,
                    service_name        TEXT NOT NULL,
                    service_id          TEXT,
                    estimated_usd_month REAL,
                    raw_json            TEXT
                )
                """,
            )
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS infra_cost_snapshot (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    fetched_at          TEXT NOT NULL,
                    service_name        TEXT NOT NULL,
                    service_id          TEXT,
                    estimated_usd_month REAL,
                    raw_json            TEXT
                )
                """,
            )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_ics_fetched ON infra_cost_snapshot(fetched_at)")

    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("Infra cost table ready — %s", backend)


def save_snapshot(services: list[dict]) -> int:
    """Persist a list of service cost rows as a single snapshot.

    Each row should look like
    ``{"service_name": str, "service_id": str, "usd_month": float, "raw": {...}}``.

    Returns the number of rows inserted.
    """
    if not services:
        return 0
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    inserted = 0
    try:
        with get_conn() as conn:
            for s in services:
                raw_json: str | None
                try:
                    raw_json = json.dumps(s.get("raw") or {}, default=str, ensure_ascii=False)
                except (TypeError, ValueError):
                    raw_json = None
                usd = s.get("usd_month")
                execute(
                    conn,
                    """
                    INSERT INTO infra_cost_snapshot
                        (fetched_at, service_name, service_id, estimated_usd_month, raw_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        now_iso,
                        s.get("service_name") or "—",
                        s.get("service_id") or None,
                        None if usd is None else float(usd),
                        raw_json,
                    ),
                )
                inserted += 1
    except Exception as exc:
        logger.warning("Failed to save infra cost snapshot: %s", exc)
        return 0
    return inserted


def latest_snapshot() -> dict:
    """Return the most recent snapshot grouped by service.

    Shape::
        {
          "fetched_at": "...",
          "services": [{"service_name": "...", "service_id": "...", "estimated_usd_month": 1.2, ...}],
          "total_usd_month": 1.2,
        }
    """
    try:
        with get_conn() as conn:
            row = query(conn, "SELECT MAX(fetched_at) AS ts FROM infra_cost_snapshot").fetchone()
            ts = row["ts"] if row else None
            if not ts:
                return {"fetched_at": None, "services": [], "total_usd_month": 0.0}
            rows = query(
                conn,
                "SELECT service_name, service_id, estimated_usd_month FROM infra_cost_snapshot "
                "WHERE fetched_at = ? ORDER BY service_name",
                (ts,),
            ).fetchall()
    except Exception as exc:
        logger.warning("latest_snapshot failed: %s", exc)
        return {"fetched_at": None, "services": [], "total_usd_month": 0.0}

    services = [dict(r) for r in rows]
    total = sum((s.get("estimated_usd_month") or 0.0) for s in services)
    return {
        "fetched_at": ts,
        "services": services,
        "total_usd_month": round(total, 4),
    }


def history(days: int = 14) -> list[dict]:
    """Return per-day totals for the last N days.

    For each day we keep ONLY the latest snapshot (``MAX(fetched_at)``) and sum
    its per-service costs. Otherwise multiple manual refreshes in the same day
    would accumulate and inflate the daily estimate.
    """
    cutoff = (datetime.now(ART) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            rows = query(
                conn,
                """
                WITH last_per_day AS (
                    SELECT SUBSTR(fetched_at, 1, 10) AS day,
                           MAX(fetched_at)          AS last_ts
                    FROM infra_cost_snapshot
                    WHERE fetched_at >= ?
                    GROUP BY SUBSTR(fetched_at, 1, 10)
                )
                SELECT lpd.day     AS day,
                       lpd.last_ts AS last_ts,
                       SUM(CASE WHEN s.estimated_usd_month IS NULL
                                THEN 0 ELSE s.estimated_usd_month END) AS total
                FROM last_per_day lpd
                JOIN infra_cost_snapshot s ON s.fetched_at = lpd.last_ts
                GROUP BY lpd.day, lpd.last_ts
                ORDER BY lpd.day DESC
                """,
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        logger.warning("infra history failed: %s", exc)
        return []

    history_rows: list[dict] = []
    for r in rows:
        history_rows.append({
            "day": r["day"],
            "estimated_usd_month": round(float(r["total"] or 0), 4),
            "last_ts": r["last_ts"],
        })
    return history_rows


def purge_old_snapshots(days: int = 90) -> int:
    """Remove snapshots older than N days."""
    cutoff = (datetime.now(ART) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            cur = execute(conn, "DELETE FROM infra_cost_snapshot WHERE fetched_at < ?", (cutoff,))
            return getattr(cur, "rowcount", 0) or 0
    except Exception as exc:
        logger.warning("purge_old_snapshots failed: %s", exc)
        return 0
