"""
Persistencia de uso de IA — tracking de tokens/costo y configuración de provider por evento.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))

# ── Pricing (USD per 1M tokens) ──────────────────────────────────────────────

MODEL_PRICING: dict[str, dict[str, float]] = {
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "llama-3.3-70b-versatile": {"input": 0.00, "output": 0.00},
}

VALID_EVENT_TYPES = frozenset(
    {"search", "search_prefetch", "topics", "weekly_summary", "top_story"}
)
VALID_PROVIDERS = frozenset({"gemini", "groq", "gemini_fallback_groq", "groq_fallback_gemini"})

# ── Table init ────────────────────────────────────────────────────────────────


def init_ai_tables() -> None:
    """Create ai_usage_log and ai_provider_config tables."""
    with get_conn() as conn:
        if is_postgres():
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS ai_usage_log (
                    id            SERIAL PRIMARY KEY,
                    event_type    TEXT NOT NULL,
                    provider      TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    input_tokens  INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_input    REAL NOT NULL,
                    cost_output   REAL NOT NULL,
                    cost_total    REAL NOT NULL,
                    latency_ms    INTEGER,
                    success       INTEGER NOT NULL DEFAULT 1,
                    error_message TEXT,
                    created_at    TEXT NOT NULL
                )
                """,
            )
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS ai_usage_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type    TEXT NOT NULL,
                    provider      TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    input_tokens  INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_input    REAL NOT NULL,
                    cost_output   REAL NOT NULL,
                    cost_total    REAL NOT NULL,
                    latency_ms    INTEGER,
                    success       INTEGER NOT NULL DEFAULT 1,
                    error_message TEXT,
                    created_at    TEXT NOT NULL
                )
                """,
            )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_aul_created ON ai_usage_log(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_aul_event ON ai_usage_log(event_type)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_aul_provider ON ai_usage_log(provider)")

        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS ai_provider_config (
                event_type TEXT PRIMARY KEY,
                provider   TEXT NOT NULL DEFAULT 'gemini_fallback_groq',
                updated_at TEXT NOT NULL
            )
            """,
        )
        _seed_provider_config(conn)

        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS ai_schedule_config (
                event_type  TEXT PRIMARY KEY,
                quiet_start TEXT NOT NULL DEFAULT '',
                quiet_end   TEXT NOT NULL DEFAULT '',
                updated_at  TEXT NOT NULL
            )
            """,
        )

    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("AI tables ready — %s", backend)


def _seed_provider_config(conn) -> None:
    """Insert default rows for every known event type if missing."""
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    for et in sorted(VALID_EVENT_TYPES):
        execute(
            conn,
            """
            INSERT INTO ai_provider_config (event_type, provider, updated_at)
            SELECT ?, 'gemini_fallback_groq', ?
            WHERE NOT EXISTS (
                SELECT 1 FROM ai_provider_config WHERE event_type = ?
            )
            """,
            (et, now_iso, et),
        )


# ── Logging ───────────────────────────────────────────────────────────────────


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[float, float]:
    """Return (cost_input, cost_output) in USD."""
    pricing = MODEL_PRICING.get(model, {"input": 0.0, "output": 0.0})
    cost_in = input_tokens * pricing["input"] / 1_000_000
    cost_out = output_tokens * pricing["output"] / 1_000_000
    return cost_in, cost_out


def log_ai_usage(
    *,
    event_type: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    success: bool = True,
    error_message: str | None = None,
) -> None:
    """Persist a single AI call record."""
    cost_in, cost_out = compute_cost(model, input_tokens, output_tokens)
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")

    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO ai_usage_log
                    (event_type, provider, model, input_tokens, output_tokens,
                     cost_input, cost_output, cost_total, latency_ms,
                     success, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    round(cost_in, 8),
                    round(cost_out, 8),
                    round(cost_in + cost_out, 8),
                    latency_ms,
                    1 if success else 0,
                    error_message,
                    now_iso,
                ),
            )
    except Exception as exc:
        logger.warning("Failed to log AI usage: %s", exc)


# ── Provider config ───────────────────────────────────────────────────────────

_config_cache: dict[str, str] = {}
_config_cache_ts: float = 0
_CONFIG_CACHE_TTL = 30  # seconds


def get_provider_config() -> dict[str, str]:
    """Return {event_type: provider} dict (cached briefly)."""
    global _config_cache, _config_cache_ts
    now = time.time()
    if _config_cache and (now - _config_cache_ts) < _CONFIG_CACHE_TTL:
        return dict(_config_cache)

    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT event_type, provider FROM ai_provider_config").fetchall()
        _config_cache = {r["event_type"]: r["provider"] for r in rows}
        _config_cache_ts = now
    except Exception as exc:
        logger.warning("Failed to read AI provider config: %s", exc)
        if not _config_cache:
            _config_cache = {et: "gemini_fallback_groq" for et in VALID_EVENT_TYPES}
            _config_cache_ts = now

    return dict(_config_cache)


def set_provider_config(event_type: str, provider: str) -> bool:
    """Update the provider for an event type. Returns True on success."""
    global _config_cache_ts
    if event_type not in VALID_EVENT_TYPES:
        return False
    if provider not in VALID_PROVIDERS:
        return False

    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                UPDATE ai_provider_config SET provider = ?, updated_at = ?
                WHERE event_type = ?
                """,
                (provider, now_iso, event_type),
            )
        _config_cache_ts = 0  # invalidate cache
        logger.info("AI provider config updated: %s → %s", event_type, provider)
        return True
    except Exception as exc:
        logger.error("Failed to update AI provider config: %s", exc)
        return False


# ── Schedule config (quiet hours) ─────────────────────────────────────────

_VALID_HOUR_RE = __import__("re").compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def get_schedule_config() -> dict[str, dict[str, str]]:
    """Return {event_type: {quiet_start, quiet_end}} for all configured schedules."""
    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT event_type, quiet_start, quiet_end FROM ai_schedule_config").fetchall()
        return {
            r["event_type"]: {"quiet_start": r["quiet_start"], "quiet_end": r["quiet_end"]}
            for r in rows
            if r["quiet_start"] and r["quiet_end"]
        }
    except Exception as exc:
        logger.warning("Failed to read AI schedule config: %s", exc)
        return {}


def set_schedule_config(event_type: str, quiet_start: str, quiet_end: str) -> bool:
    """Set quiet hours for an event type. Empty strings clear the schedule."""
    if event_type not in VALID_EVENT_TYPES:
        return False

    if quiet_start and not _VALID_HOUR_RE.match(quiet_start):
        return False
    if quiet_end and not _VALID_HOUR_RE.match(quiet_end):
        return False
    if bool(quiet_start) != bool(quiet_end):
        return False

    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            if is_postgres():
                execute(
                    conn,
                    """
                    INSERT INTO ai_schedule_config (event_type, quiet_start, quiet_end, updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (event_type) DO UPDATE SET
                        quiet_start = EXCLUDED.quiet_start,
                        quiet_end = EXCLUDED.quiet_end,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (event_type, quiet_start, quiet_end, now_iso),
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO ai_schedule_config (event_type, quiet_start, quiet_end, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_type) DO UPDATE SET
                        quiet_start = excluded.quiet_start,
                        quiet_end = excluded.quiet_end,
                        updated_at = excluded.updated_at
                    """,
                    (event_type, quiet_start, quiet_end, now_iso),
                )
        logger.info("AI schedule config updated: %s → %s–%s", event_type, quiet_start or "(none)", quiet_end or "(none)")
        return True
    except Exception as exc:
        logger.error("Failed to update AI schedule config: %s", exc)
        return False


def is_in_quiet_hours(event_type: str) -> bool:
    """Return True if the current time (ART) falls within the quiet window for *event_type*."""
    schedule = get_schedule_config().get(event_type)
    if not schedule:
        return False

    quiet_start = schedule["quiet_start"]
    quiet_end = schedule["quiet_end"]
    if not quiet_start or not quiet_end:
        return False

    now = datetime.now(ART)
    current_minutes = now.hour * 60 + now.minute

    sh, sm = map(int, quiet_start.split(":"))
    eh, em = map(int, quiet_end.split(":"))
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes < end_minutes
    # Wraps midnight (e.g. 22:00–06:00)
    return current_minutes >= start_minutes or current_minutes < end_minutes


# ── Queries for admin panel ───────────────────────────────────────────────────


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


def query_ai_cost_summary(
    desde: str | None = None, hasta: str | None = None,
) -> dict:
    """Aggregated AI cost data for the admin panel."""
    where_sql, params = _where_clause(desde, hasta)

    de = _date_expr()

    with get_conn() as conn:
        totals = query(
            conn,
            f"""SELECT
                    COUNT(*) as calls,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cost_total), 0) as cost_total,
                    COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) as success_count,
                    COUNT(DISTINCT {de}) as distinct_days
                FROM ai_usage_log{where_sql}""",
            params,
        ).fetchone()

        by_provider = query(
            conn,
            f"""SELECT provider,
                    COUNT(*) as calls,
                    COALESCE(SUM(cost_total), 0) as cost_total
                FROM ai_usage_log{where_sql}
                GROUP BY provider""",
            params,
        ).fetchall()

        by_event = query(
            conn,
            f"""SELECT event_type, provider,
                    COUNT(*) as calls,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cost_input), 0) as cost_input,
                    COALESCE(SUM(cost_output), 0) as cost_output,
                    COALESCE(SUM(cost_total), 0) as cost_total
                FROM ai_usage_log{where_sql}
                GROUP BY event_type, provider
                ORDER BY cost_total DESC""",
            params,
        ).fetchall()

    return {
        "totals": {
            "calls": totals["calls"] if totals else 0,
            "input_tokens": totals["input_tokens"] if totals else 0,
            "output_tokens": totals["output_tokens"] if totals else 0,
            "cost_total": round(totals["cost_total"], 6) if totals else 0,
            "success_count": totals["success_count"] if totals else 0,
            "distinct_days": totals["distinct_days"] if totals else 0,
        },
        "by_provider": [
            {
                "provider": r["provider"],
                "calls": r["calls"],
                "cost_total": round(r["cost_total"], 6),
            }
            for r in by_provider
        ],
        "by_event": [
            {
                "event_type": r["event_type"],
                "provider": r["provider"],
                "calls": r["calls"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cost_input": round(r["cost_input"], 6),
                "cost_output": round(r["cost_output"], 6),
                "cost_total": round(r["cost_total"], 6),
            }
            for r in by_event
        ],
    }


def query_ai_daily_cost(
    desde: str | None = None, hasta: str | None = None,
) -> list[dict]:
    """Daily cost series for charting."""
    where_sql, params = _where_clause(desde, hasta)
    de = _date_expr()

    with get_conn() as conn:
        rows = query(
            conn,
            f"""SELECT {de} as day,
                    COUNT(*) as calls,
                    COALESCE(SUM(cost_total), 0) as cost_total,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens
                FROM ai_usage_log{where_sql}
                GROUP BY {de}
                ORDER BY day DESC LIMIT 90""",
            params,
        ).fetchall()

    return [
        {
            "day": r["day"],
            "calls": r["calls"],
            "cost_total": round(r["cost_total"], 6),
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
        }
        for r in rows
    ]
