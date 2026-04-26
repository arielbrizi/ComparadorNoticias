"""
Persistencia de snapshots de costo de infraestructura (Railway).

Guardamos un snapshot por cada pull a Railway (cron horario), con el costo
estimado por servicio. Así podemos mostrar un historial diario en el admin
y no depender únicamente del valor "en vivo".

También expone los límites de gasto USD diario/mensual (almacenados en
``ai_runtime_config``) y el spend actual derivado de los snapshots, que el
guardrail de ``ai_search`` usa para bloquear Ollama si el proyecto se está
yendo de presupuesto en Railway.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))

# Claves en ``ai_runtime_config`` para los límites USD de Railway. Las dos
# son opcionales: si no hay row (o el valor es None) significa "sin límite".
_INFRA_USD_DAILY_KEY = "infra_usd_daily_max"
_INFRA_USD_MONTHLY_KEY = "infra_usd_monthly_max"

# Cache del spend actual. Lo consulta el guard del provider chain en cada
# llamada a Ollama, así que cacheamos para no hacer 2 queries SQL por call.
_spend_cache: tuple[float, dict[str, float | None]] | None = None
_SPEND_CACHE_TTL = 30


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
    global _spend_cache
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
    if inserted:
        _spend_cache = None
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


# ── USD limits (daily / monthly) ─────────────────────────────────────────


def _get_runtime_value(key: str) -> str | None:
    """Read a single ``ai_runtime_config`` value with no caching.

    We deliberately don't share ``ai_store._runtime_cache`` to keep the
    module decoupled (avoids an import cycle with ``ai_store``). Each call
    is a single primary-key lookup so it's cheap.
    """
    try:
        with get_conn() as conn:
            row = query(
                conn,
                "SELECT value FROM ai_runtime_config WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return row["value"]
    except Exception as exc:
        logger.warning("infra_cost_store: read %s failed: %s", key, exc)
        return None


def _set_runtime_value(key: str, value: str | None) -> bool:
    """Upsert (or delete on ``None``) a single ``ai_runtime_config`` row."""
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            if value is None:
                execute(
                    conn,
                    "DELETE FROM ai_runtime_config WHERE key = ?",
                    (key,),
                )
            elif is_postgres():
                execute(
                    conn,
                    """
                    INSERT INTO ai_runtime_config (key, value, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (key, value, now_iso),
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO ai_runtime_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, now_iso),
                )
        return True
    except Exception as exc:
        logger.error("infra_cost_store: write %s failed: %s", key, exc)
        return False


def _parse_optional_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val < 0:
        return None
    return val


def get_infra_limits() -> dict[str, float | None]:
    """Return ``{"daily_max": .., "monthly_max": ..}`` (None = sin límite)."""
    return {
        "daily_max": _parse_optional_float(_get_runtime_value(_INFRA_USD_DAILY_KEY)),
        "monthly_max": _parse_optional_float(_get_runtime_value(_INFRA_USD_MONTHLY_KEY)),
    }


def set_infra_limits(
    *,
    daily_max: float | int | None,
    monthly_max: float | int | None,
) -> bool:
    """Persist both limits at once. Validates non-negative numbers or None.

    Returns ``False`` on invalid input (negative or non-numeric); both
    values are written atomically so a failed validation reverts both
    fields. Either ``None`` clears that single key.
    """
    global _spend_cache

    def _validate(value):
        if value is None:
            return True, None
        if isinstance(value, bool):
            return False, None
        if not isinstance(value, (int, float)):
            return False, None
        if value < 0:
            return False, None
        return True, float(value)

    ok_d, daily_val = _validate(daily_max)
    ok_m, monthly_val = _validate(monthly_max)
    if not ok_d or not ok_m:
        return False

    ok = _set_runtime_value(
        _INFRA_USD_DAILY_KEY,
        None if daily_val is None else str(daily_val),
    )
    ok = ok and _set_runtime_value(
        _INFRA_USD_MONTHLY_KEY,
        None if monthly_val is None else str(monthly_val),
    )
    if ok:
        _spend_cache = None
        logger.info(
            "Railway infra limits updated: daily=%s, monthly=%s",
            daily_val if daily_val is not None else "(sin límite)",
            monthly_val if monthly_val is not None else "(sin límite)",
        )
    return ok


# ── Spend en USD del día / mes ───────────────────────────────────────────


def _today_start_iso(now: datetime | None = None) -> str:
    """Return ``YYYY-MM-DDT00:00:00`` for the current ART day."""
    n = now or datetime.now(ART)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def _query_total_at_or_after(since_iso: str) -> tuple[str | None, float | None]:
    """Return ``(fetched_at, total_usd_month)`` of the **earliest** snapshot
    aggregate row at or after ``since_iso`` (or ``(None, None)`` if missing).

    The aggregate row is the one with ``raw_json`` containing
    ``"_aggregate": true`` (set by ``railway_client._normalize_services``).
    Each snapshot has exactly one such row whose ``estimated_usd_month``
    is the project total at fetch time.
    """
    try:
        with get_conn() as conn:
            row = query(
                conn,
                """
                SELECT fetched_at, estimated_usd_month
                  FROM infra_cost_snapshot
                 WHERE fetched_at >= ?
                   AND raw_json LIKE '%"_aggregate":%true%'
                 ORDER BY fetched_at ASC
                 LIMIT 1
                """,
                (since_iso,),
            ).fetchone()
    except Exception as exc:
        logger.warning("infra spend lookup failed: %s", exc)
        return None, None
    if row is None:
        return None, None
    ts = row["fetched_at"]
    val = row["estimated_usd_month"]
    return ts, (None if val is None else float(val))


def _query_last_total_before(before_iso: str) -> tuple[str | None, float | None]:
    """Return ``(fetched_at, total_usd_month)`` of the **latest** aggregate
    snapshot strictly before ``before_iso`` (or ``(None, None)`` if missing).

    Used as a fallback baseline for ``today_usd`` when the project has
    snapshots from previous days but only one (or none) from today: we
    prefer to compute ``today_usd = latest_total - last_total_before_today``
    instead of returning ``None`` and forcing the user to take two
    snapshots within the same day before any spend can be attributed.
    """
    try:
        with get_conn() as conn:
            row = query(
                conn,
                """
                SELECT fetched_at, estimated_usd_month
                  FROM infra_cost_snapshot
                 WHERE fetched_at < ?
                   AND raw_json LIKE '%"_aggregate":%true%'
                 ORDER BY fetched_at DESC
                 LIMIT 1
                """,
                (before_iso,),
            ).fetchone()
    except Exception as exc:
        logger.warning("infra previous-day spend lookup failed: %s", exc)
        return None, None
    if row is None:
        return None, None
    ts = row["fetched_at"]
    val = row["estimated_usd_month"]
    return ts, (None if val is None else float(val))


def _query_latest_total() -> tuple[str | None, float | None]:
    """Return ``(fetched_at, total_usd_month)`` of the latest aggregate row."""
    try:
        with get_conn() as conn:
            row = query(
                conn,
                """
                SELECT fetched_at, estimated_usd_month
                  FROM infra_cost_snapshot
                 WHERE raw_json LIKE '%"_aggregate":%true%'
                 ORDER BY fetched_at DESC
                 LIMIT 1
                """,
            ).fetchone()
    except Exception as exc:
        logger.warning("infra latest lookup failed: %s", exc)
        return None, None
    if row is None:
        return None, None
    ts = row["fetched_at"]
    val = row["estimated_usd_month"]
    return ts, (None if val is None else float(val))


def get_current_spend() -> dict[str, float | None | str]:
    """Return ``{today_usd, month_usd, fetched_at}``.

    - ``month_usd`` = ``estimated_usd_month`` of the latest snapshot
      (Railway already reports cumulative spend for the billing period).
    - ``today_usd`` = ``month_usd_now − baseline``, donde la baseline se
      elige así, de mejor a peor:
          1. último snapshot **antes** del comienzo del día actual (típico
             del cron horario de ayer); permite computar gasto diario con
             solo 1 snapshot nuevo de hoy;
          2. primer snapshot del día actual, si no hay historia previa
             y ya hay al menos 2 snapshots dentro del día;
          3. ``None`` si solo hay 1 snapshot total y nada anterior.
      Es ``None`` cuando no podemos garantizar el delta.
    - ``fetched_at`` = timestamp of the latest snapshot used for ``month_usd``.

    Cached for ``_SPEND_CACHE_TTL`` seconds (the guard hits this on every
    Ollama call and the data only refreshes hourly anyway).
    """
    global _spend_cache
    now = time.time()
    if _spend_cache is not None and (now - _spend_cache[0]) < _SPEND_CACHE_TTL:
        return dict(_spend_cache[1])

    latest_ts, latest_total = _query_latest_total()
    today_start = _today_start_iso()

    today_usd: float | None
    if latest_total is None:
        today_usd = None
    else:
        # Preferimos el último snapshot ANTES de hoy (mejor baseline:
        # representa lo gastado al cierre de ayer).
        prev_ts, prev_total = _query_last_total_before(today_start)
        if prev_ts is not None and prev_total is not None:
            today_usd = max(0.0, float(latest_total) - float(prev_total))
        else:
            # No hay historia previa al día -> caemos a usar el primer
            # snapshot de hoy como baseline. Esto requiere 2 snapshots
            # dentro del día para devolver algo distinto de None.
            base_ts, base_total = _query_total_at_or_after(today_start)
            if base_ts is None or base_total is None:
                today_usd = None
            elif base_ts == latest_ts:
                today_usd = None
            else:
                today_usd = max(0.0, float(latest_total) - float(base_total))

    result: dict[str, float | None | str] = {
        "today_usd": None if today_usd is None else round(today_usd, 4),
        "month_usd": None if latest_total is None else round(float(latest_total), 4),
        "fetched_at": latest_ts,
    }
    _spend_cache = (now, dict(result))  # type: ignore[assignment]
    return result


def get_blocked_keys() -> list[str]:
    """Return which limit keys are currently exceeded (subset of ``daily``/``monthly``).

    Returns an empty list when no limit is configured, when there's no
    spend data yet, or when the spend is below both caps.
    """
    limits = get_infra_limits()
    spend = get_current_spend()
    blocked: list[str] = []

    daily_max = limits.get("daily_max")
    monthly_max = limits.get("monthly_max")
    today_usd = spend.get("today_usd")
    month_usd = spend.get("month_usd")

    if (
        isinstance(daily_max, (int, float))
        and daily_max is not None
        and isinstance(today_usd, (int, float))
        and today_usd is not None
        and float(today_usd) >= float(daily_max)
    ):
        blocked.append("daily")
    if (
        isinstance(monthly_max, (int, float))
        and monthly_max is not None
        and isinstance(month_usd, (int, float))
        and month_usd is not None
        and float(month_usd) >= float(monthly_max)
    ):
        blocked.append("monthly")
    return blocked


def reset_spend_cache() -> None:
    """Force a re-read on the next ``get_current_spend`` call.

    Called from ``save_snapshot`` so the admin's "Actualizar ahora" button
    reflects new data immediately, and from ``set_infra_limits`` after
    config changes.
    """
    global _spend_cache
    _spend_cache = None
