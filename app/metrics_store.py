"""
Persistencia de métricas en SQLite.
Guarda eventos por grupo/fuente para poder consultar historial por rango de fechas.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.models import ArticleGroup

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "metrics.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metric_events (
                group_id       TEXT    NOT NULL,
                source         TEXT    NOT NULL,
                published      TEXT,
                is_first       INTEGER NOT NULL DEFAULT 0,
                reaction_min   REAL,
                source_count   INTEGER NOT NULL DEFAULT 1,
                category       TEXT    NOT NULL DEFAULT '',
                title          TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (group_id, source)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_me_published
            ON metric_events (published)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_me_source
            ON metric_events (source)
        """)
    logger.info("Metrics DB ready at %s", DB_PATH)


def save_group_metrics(groups: list[ArticleGroup]) -> int:
    """Persist metric events for all groups. Returns number of new rows inserted."""
    rows: list[tuple] = []

    for g in groups:
        dated = [a for a in g.articles if a.published]
        dated.sort(key=lambda a: a.published)

        first_time = dated[0].published if dated else None

        sources_seen: set[str] = set()
        for a in g.articles:
            if a.source in sources_seen:
                continue
            sources_seen.add(a.source)

            is_first = 0
            reaction = None

            if g.source_count >= 2 and a.published and first_time:
                delta = (a.published - first_time).total_seconds() / 60
                if a == dated[0]:
                    is_first = 1
                    reaction = None
                elif delta >= 0:
                    reaction = round(delta, 2)

            pub_iso = a.published.isoformat() if a.published else None

            rows.append((
                g.group_id,
                a.source,
                pub_iso,
                is_first,
                reaction,
                g.source_count,
                g.category,
                g.representative_title,
            ))

    if not rows:
        return 0

    with _conn() as conn:
        cursor = conn.executemany(
            """INSERT OR IGNORE INTO metric_events
               (group_id, source, published, is_first, reaction_min, source_count, category, title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        inserted = cursor.rowcount
        logger.info("Metrics: %d new events saved (%d total candidates)", inserted, len(rows))
        return inserted


def query_metrics(
    desde: str | None = None,
    hasta: str | None = None,
) -> dict:
    """
    Compute aggregated metrics from stored events, optionally filtered by date range.
    `desde` and `hasta` are ISO date strings (YYYY-MM-DD).
    """
    where_clauses = []
    params: list[str] = []

    if desde:
        where_clauses.append("published >= ?")
        params.append(f"{desde}T00:00:00")
    if hasta:
        where_clauses.append("published < ?")
        params.append(f"{hasta}T23:59:59")

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with _conn() as conn:
        conn.row_factory = sqlite3.Row

        # 1. First publisher ranking
        first_rows = conn.execute(f"""
            SELECT source, COUNT(*) as cnt
            FROM metric_events
            {where_sql} {"AND" if where_clauses else "WHERE"} is_first = 1
            GROUP BY source
            ORDER BY cnt DESC
        """, params).fetchall()

        first_ranking = [{"source": r["source"], "count": r["cnt"]} for r in first_rows]

        # 2. Average reaction time
        reaction_rows = conn.execute(f"""
            SELECT source,
                   AVG(reaction_min) as avg_min,
                   COUNT(*) as cnt
            FROM metric_events
            {where_sql} {"AND" if where_clauses else "WHERE"} reaction_min IS NOT NULL
            GROUP BY source
            ORDER BY avg_min ASC
        """, params).fetchall()

        avg_reaction = [
            {
                "source": r["source"],
                "avg_minutes": round(r["avg_min"], 1),
                "sample_size": r["cnt"],
            }
            for r in reaction_rows
        ]

        # 3. Exclusivity index
        total_rows = conn.execute(f"""
            SELECT source,
                   COUNT(DISTINCT group_id) as total,
                   SUM(CASE WHEN source_count = 1 THEN 1 ELSE 0 END) as exclusive
            FROM metric_events
            {where_sql}
            GROUP BY source
            ORDER BY (CAST(SUM(CASE WHEN source_count = 1 THEN 1 ELSE 0 END) AS REAL) / COUNT(DISTINCT group_id)) DESC
        """, params).fetchall()

        exclusivity = [
            {
                "source": r["source"],
                "exclusive": r["exclusive"],
                "total": r["total"],
                "percentage": round(r["exclusive"] / r["total"] * 100, 1) if r["total"] else 0,
            }
            for r in total_rows
        ]

        # Summary counts
        summary = conn.execute(f"""
            SELECT
                COUNT(DISTINCT group_id) as total_groups,
                COUNT(DISTINCT CASE WHEN source_count >= 2 THEN group_id END) as multi_groups
            FROM metric_events
            {where_sql}
        """, params).fetchone()

        # Date range available
        date_range = conn.execute("""
            SELECT MIN(published) as min_date, MAX(published) as max_date
            FROM metric_events
            WHERE published IS NOT NULL
        """).fetchone()

    return {
        "first_publisher_ranking": first_ranking,
        "avg_reaction_time": avg_reaction,
        "exclusivity_index": exclusivity,
        "multi_source_groups": summary["multi_groups"] if summary else 0,
        "total_groups": summary["total_groups"] if summary else 0,
        "date_range": {
            "min": date_range["min_date"][:10] if date_range and date_range["min_date"] else None,
            "max": date_range["max_date"][:10] if date_range and date_range["max_date"] else None,
        },
    }
