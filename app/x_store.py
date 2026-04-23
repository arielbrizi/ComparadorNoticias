"""
Persistencia de la integración con X (ex-Twitter).

Guarda:
- ``x_campaigns``: configuración por campaña (enabled, schedule, template).
- ``x_tier_config``: tier contratado + caps efectivos.
- ``x_oauth_state``: tokens OAuth2 vigentes (sobreviven a redeploys).
- ``x_usage_log``: historial de intentos de posteo (ok / error / bloqueos).

Patrón de diseño calcado de ``app/ai_store.py``: SQLite local / Postgres en
Railway, caches TTL-based, CRUD de filas únicas con ``ON CONFLICT``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))


# ── Constantes ───────────────────────────────────────────────────────────────

VALID_CAMPAIGN_KEYS: frozenset[str] = frozenset({
    "cloud", "topstory", "weekly", "topics", "breaking",
})

VALID_CAMPAIGN_STATUSES: frozenset[str] = frozenset({
    "ok", "error", "rate_limited", "quota_exceeded", "disabled_by_tier", "skipped",
})

VALID_TIERS: frozenset[str] = frozenset({"disabled", "basic", "pro", "pay_per_use"})

# Alias legacy: los tiers "custom" y "free" se renombraron a "pay_per_use" y
# "disabled" respectivamente para reflejar lo que realmente hacen en la app
# (Free dejó de existir como plan de X en feb-2026; internamente es un
# kill-switch que apaga todas las campañas). Toda fila vieja se migra
# on-read / on-init a la clave nueva.
_LEGACY_TIER_ALIASES: dict[str, str] = {
    "custom": "pay_per_use",
    "free": "disabled",
}

# Valores publicados por X (2026-04). ``disabled`` es el kill-switch interno
# (antes llamado "free") que apaga todas las campañas; Basic/Pro traen el cap
# típico del tier; ``pay_per_use`` deja que el admin defina los 3 campos (se
# cobra por request, ~USD 0.01 por tweet).
TIER_DEFAULTS: dict[str, dict[str, Any]] = {
    "disabled": {
        "daily_cap": 0,
        "monthly_cap": 0,
        "monthly_usd": 0.0,
        "posting_allowed": False,
    },
    "basic": {
        "daily_cap": 50,
        "monthly_cap": 1500,
        "monthly_usd": 200.0,
        "posting_allowed": True,
    },
    "pro": {
        "daily_cap": 10_000,
        "monthly_cap": 300_000,
        "monthly_usd": 5_000.0,
        "posting_allowed": True,
    },
    "pay_per_use": {
        "daily_cap": 50,
        "monthly_cap": 1500,
        "monthly_usd": 0.0,
        "posting_allowed": True,
    },
}


# Plantillas por defecto de cada campaña. Los placeholders se resuelven en
# ``app/x_campaigns.py`` antes de postear.
CAMPAIGN_DEFAULTS: dict[str, dict[str, Any]] = {
    "cloud": {
        "enabled": False,
        "schedule": {"hour": 9, "minute": 30},
        "template": {
            "text": "La nube del día — {date}\n\nTop palabras: {top_words}\n\n{hashtags}",
            "hashtags": "#Argentina #Noticias #VsNews",
            "attach_image": True,
            "thread": False,
        },
    },
    "topstory": {
        "enabled": False,
        "schedule": {"hour": 8, "minute": 30},
        "template": {
            "text": "La noticia del día 📰\n\n{title}\n\n{url}\n\n{hashtags}",
            "hashtags": "#Argentina #Noticias",
            "attach_image": False,
            "thread": False,
        },
    },
    "weekly": {
        "enabled": False,
        "schedule": {"hour": 10, "minute": 0, "day_of_week": "mon"},
        "template": {
            "text": "Resumen semanal {week_start} → {week_end}\n\n{summary}\n\n{hashtags}",
            "hashtags": "#Argentina #ResumenSemanal",
            "attach_image": False,
            "thread": True,
            "thread_max_posts": 4,
        },
    },
    "topics": {
        "enabled": False,
        "schedule": {"hour": 12, "minute": 0},
        "template": {
            "text": "Temas del día en Argentina 🧵\n\n{topics_list}\n\n{hashtags}",
            "hashtags": "#Argentina #Noticias",
            "attach_image": False,
            "thread": True,
            "thread_max_posts": 5,
        },
    },
    "breaking": {
        "enabled": False,
        "schedule": {
            "min_source_count": 3,
            "categories": ["Política", "Economía"],
            "cooldown_minutes": 60,
        },
        "template": {
            "text": "🚨 {title}\n\n{url}\n\n{hashtags}",
            "hashtags": "#UltimoMomento #Argentina",
            "attach_image": False,
            "thread": False,
        },
    },
}


# ── Init ─────────────────────────────────────────────────────────────────────


def init_x_tables() -> None:
    """Crear todas las tablas necesarias para la integración con X."""
    with get_conn() as conn:
        if is_postgres():
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_campaigns (
                    campaign_key    TEXT PRIMARY KEY,
                    enabled         INTEGER NOT NULL DEFAULT 0,
                    schedule_json   TEXT NOT NULL,
                    template_json   TEXT NOT NULL,
                    last_run_at     TEXT,
                    last_run_status TEXT,
                    updated_at      TEXT NOT NULL
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_tier_config (
                    id           INTEGER PRIMARY KEY DEFAULT 1,
                    tier         TEXT NOT NULL DEFAULT 'disabled',
                    daily_cap    INTEGER NOT NULL DEFAULT 0,
                    monthly_cap  INTEGER NOT NULL DEFAULT 0,
                    monthly_usd  REAL NOT NULL DEFAULT 0,
                    updated_at   TEXT NOT NULL,
                    CHECK (id = 1)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_oauth_state (
                    id            INTEGER PRIMARY KEY DEFAULT 1,
                    access_token  TEXT,
                    refresh_token TEXT,
                    expires_at    TEXT,
                    handle        TEXT,
                    updated_at    TEXT NOT NULL,
                    CHECK (id = 1)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_usage_log (
                    id             SERIAL PRIMARY KEY,
                    campaign_key   TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    post_id        TEXT,
                    response_code  INTEGER,
                    error_message  TEXT,
                    preview        TEXT,
                    posts_count    INTEGER NOT NULL DEFAULT 1,
                    created_at     TEXT NOT NULL
                )
                """,
            )
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_campaigns (
                    campaign_key    TEXT PRIMARY KEY,
                    enabled         INTEGER NOT NULL DEFAULT 0,
                    schedule_json   TEXT NOT NULL,
                    template_json   TEXT NOT NULL,
                    last_run_at     TEXT,
                    last_run_status TEXT,
                    updated_at      TEXT NOT NULL
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_tier_config (
                    id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    tier         TEXT NOT NULL DEFAULT 'disabled',
                    daily_cap    INTEGER NOT NULL DEFAULT 0,
                    monthly_cap  INTEGER NOT NULL DEFAULT 0,
                    monthly_usd  REAL NOT NULL DEFAULT 0,
                    updated_at   TEXT NOT NULL
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_oauth_state (
                    id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    access_token  TEXT,
                    refresh_token TEXT,
                    expires_at    TEXT,
                    handle        TEXT,
                    updated_at    TEXT NOT NULL
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS x_usage_log (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_key   TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    post_id        TEXT,
                    response_code  INTEGER,
                    error_message  TEXT,
                    preview        TEXT,
                    posts_count    INTEGER NOT NULL DEFAULT 1,
                    created_at     TEXT NOT NULL
                )
                """,
            )

        execute(conn, "CREATE INDEX IF NOT EXISTS idx_xul_created ON x_usage_log(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_xul_campaign ON x_usage_log(campaign_key)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_xul_status ON x_usage_log(status)")

        _seed_campaigns(conn)
        _seed_tier(conn)
        _migrate_legacy_tiers(conn)

    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("X tables ready — %s", backend)


def _now_iso() -> str:
    return datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_campaigns(conn) -> None:
    """Inserta una fila por cada campaña conocida si falta."""
    now_iso = _now_iso()
    for key, cfg in CAMPAIGN_DEFAULTS.items():
        execute(
            conn,
            """
            INSERT INTO x_campaigns
                (campaign_key, enabled, schedule_json, template_json, updated_at)
            SELECT ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM x_campaigns WHERE campaign_key = ?
            )
            """,
            (
                key,
                1 if cfg["enabled"] else 0,
                json.dumps(cfg["schedule"], ensure_ascii=False),
                json.dumps(cfg["template"], ensure_ascii=False),
                now_iso,
                key,
            ),
        )


def _migrate_legacy_tiers(conn) -> None:
    """Renombra tiers obsoletos (``custom`` → ``pay_per_use``) si persisten en DB.

    Es idempotente: si no hay filas con valores legacy, no pasa nada. Pensado
    para corridas post-deploy donde la DB se arrastra desde una versión previa.
    """
    for legacy, new in _LEGACY_TIER_ALIASES.items():
        try:
            execute(
                conn,
                "UPDATE x_tier_config SET tier = ? WHERE tier = ?",
                (new, legacy),
            )
        except Exception as exc:
            logger.warning("Legacy tier migration %s→%s failed: %s", legacy, new, exc)


def _seed_tier(conn) -> None:
    """Inserta la fila única de tier_config en estado ``disabled`` si falta."""
    now_iso = _now_iso()
    defaults = TIER_DEFAULTS["disabled"]
    execute(
        conn,
        """
        INSERT INTO x_tier_config
            (id, tier, daily_cap, monthly_cap, monthly_usd, updated_at)
        SELECT 1, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (SELECT 1 FROM x_tier_config WHERE id = 1)
        """,
        (
            "disabled",
            int(defaults["daily_cap"]),
            int(defaults["monthly_cap"]),
            float(defaults["monthly_usd"]),
            now_iso,
        ),
    )


# ── Campaigns ────────────────────────────────────────────────────────────────

_campaign_cache: dict[str, dict[str, Any]] = {}
_campaign_cache_ts: float = 0
_CAMPAIGN_CACHE_TTL = 15


def _parse_json(raw: str, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _campaign_row_to_dict(row) -> dict[str, Any]:
    key = row["campaign_key"]
    defaults = CAMPAIGN_DEFAULTS.get(key, {"schedule": {}, "template": {}})
    schedule = _parse_json(row["schedule_json"], dict(defaults["schedule"]))
    template = _parse_json(row["template_json"], dict(defaults["template"]))
    return {
        "campaign_key": key,
        "enabled": bool(row["enabled"]),
        "schedule": schedule,
        "template": template,
        "last_run_at": row["last_run_at"],
        "last_run_status": row["last_run_status"],
        "updated_at": row["updated_at"],
    }


def list_campaigns() -> list[dict[str, Any]]:
    """Devuelve la configuración de las 5 campañas en orden fijo."""
    global _campaign_cache, _campaign_cache_ts
    now = time.time()
    if _campaign_cache and (now - _campaign_cache_ts) < _CAMPAIGN_CACHE_TTL:
        return [dict(_campaign_cache[k]) for k in CAMPAIGN_DEFAULTS.keys() if k in _campaign_cache]

    result: dict[str, dict[str, Any]] = {}
    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT * FROM x_campaigns").fetchall()
        for r in rows:
            if r["campaign_key"] in VALID_CAMPAIGN_KEYS:
                result[r["campaign_key"]] = _campaign_row_to_dict(r)
        _campaign_cache = {k: dict(v) for k, v in result.items()}
        _campaign_cache_ts = now
    except Exception as exc:
        logger.warning("Failed to read x_campaigns: %s", exc)
        for key, cfg in CAMPAIGN_DEFAULTS.items():
            result[key] = {
                "campaign_key": key,
                "enabled": cfg["enabled"],
                "schedule": dict(cfg["schedule"]),
                "template": dict(cfg["template"]),
                "last_run_at": None,
                "last_run_status": None,
                "updated_at": None,
            }

    return [dict(result[k]) for k in CAMPAIGN_DEFAULTS.keys() if k in result]


def get_campaign_config(campaign_key: str) -> dict[str, Any] | None:
    """Devuelve la config de una campaña puntual (o ``None`` si no existe)."""
    if campaign_key not in VALID_CAMPAIGN_KEYS:
        return None
    for c in list_campaigns():
        if c["campaign_key"] == campaign_key:
            return dict(c)
    return None


def set_campaign_config(
    campaign_key: str,
    *,
    enabled: bool | None = None,
    schedule: dict[str, Any] | None = None,
    template: dict[str, Any] | None = None,
) -> bool:
    """Actualiza uno o más campos de una campaña.

    Sólo se escriben los campos que llegan con valor distinto de ``None``.
    Si no existe la fila (sólo puede pasar con una DB corrupta) se inserta.
    """
    global _campaign_cache_ts
    if campaign_key not in VALID_CAMPAIGN_KEYS:
        return False

    current = get_campaign_config(campaign_key)
    if not current:
        return False

    new_enabled = bool(enabled) if enabled is not None else current["enabled"]
    new_schedule = schedule if schedule is not None else current["schedule"]
    new_template = template if template is not None else current["template"]

    if not isinstance(new_schedule, dict) or not isinstance(new_template, dict):
        return False

    now_iso = _now_iso()
    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                UPDATE x_campaigns
                SET enabled = ?, schedule_json = ?, template_json = ?, updated_at = ?
                WHERE campaign_key = ?
                """,
                (
                    1 if new_enabled else 0,
                    json.dumps(new_schedule, ensure_ascii=False),
                    json.dumps(new_template, ensure_ascii=False),
                    now_iso,
                    campaign_key,
                ),
            )
        _campaign_cache_ts = 0
        logger.info("X campaign updated: %s (enabled=%s)", campaign_key, new_enabled)
        return True
    except Exception as exc:
        logger.error("Failed to update x_campaigns(%s): %s", campaign_key, exc)
        return False


def record_campaign_run(campaign_key: str, status: str) -> None:
    """Actualiza ``last_run_at`` / ``last_run_status`` en ``x_campaigns``."""
    global _campaign_cache_ts
    if campaign_key not in VALID_CAMPAIGN_KEYS:
        return
    now_iso = _now_iso()
    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                UPDATE x_campaigns
                SET last_run_at = ?, last_run_status = ?
                WHERE campaign_key = ?
                """,
                (now_iso, status, campaign_key),
            )
        _campaign_cache_ts = 0
    except Exception as exc:
        logger.warning("record_campaign_run(%s) failed: %s", campaign_key, exc)


def disable_all_campaigns() -> int:
    """Pone ``enabled=0`` en todas las campañas (usado al mover a tier=disabled)."""
    global _campaign_cache_ts
    now_iso = _now_iso()
    try:
        with get_conn() as conn:
            cur = execute(
                conn,
                "UPDATE x_campaigns SET enabled = 0, updated_at = ?",
                (now_iso,),
            )
            n = cur.rowcount if hasattr(cur, "rowcount") else 0
        _campaign_cache_ts = 0
        logger.info("X campaigns disabled en masse (%d rows)", n or 0)
        return int(n or 0)
    except Exception as exc:
        logger.error("disable_all_campaigns failed: %s", exc)
        return 0


# ── Tier config ──────────────────────────────────────────────────────────────


_tier_cache: dict[str, Any] | None = None
_tier_cache_ts: float = 0
_TIER_CACHE_TTL = 30


def get_tier_config() -> dict[str, Any]:
    """Devuelve el tier actual + caps vigentes (cacheado 30s)."""
    global _tier_cache, _tier_cache_ts
    now = time.time()
    if _tier_cache and (now - _tier_cache_ts) < _TIER_CACHE_TTL:
        return dict(_tier_cache)

    try:
        with get_conn() as conn:
            row = query(
                conn,
                "SELECT tier, daily_cap, monthly_cap, monthly_usd, updated_at "
                "FROM x_tier_config WHERE id = 1",
            ).fetchone()
    except Exception as exc:
        logger.warning("Failed to read x_tier_config: %s", exc)
        row = None

    if not row:
        defaults = TIER_DEFAULTS["disabled"]
        out = {
            "tier": "disabled",
            "daily_cap": int(defaults["daily_cap"]),
            "monthly_cap": int(defaults["monthly_cap"]),
            "monthly_usd": float(defaults["monthly_usd"]),
            "posting_allowed": bool(defaults["posting_allowed"]),
            "updated_at": None,
        }
    else:
        raw_tier = row["tier"]
        # Migración on-read: claves legacy (ej. "custom", "free") se traducen a
        # su equivalente moderno. Si la clave no existe en ningún lado, caemos
        # al kill-switch ("disabled") para no habilitar posteo por accidente.
        tier = _LEGACY_TIER_ALIASES.get(raw_tier, raw_tier)
        if tier not in VALID_TIERS:
            tier = "disabled"
        defaults = TIER_DEFAULTS.get(tier, TIER_DEFAULTS["disabled"])
        out = {
            "tier": tier,
            "daily_cap": int(row["daily_cap"] or 0),
            "monthly_cap": int(row["monthly_cap"] or 0),
            "monthly_usd": float(row["monthly_usd"] or 0),
            "posting_allowed": bool(defaults["posting_allowed"]) and tier != "disabled",
            "updated_at": row["updated_at"],
        }

    _tier_cache = dict(out)
    _tier_cache_ts = now
    return dict(out)


def set_tier_config(
    tier: str,
    *,
    daily_cap: int | None = None,
    monthly_cap: int | None = None,
    monthly_usd: float | None = None,
) -> bool:
    """Actualiza la fila única de tier. Los caps se normalizan según el tier.

    - ``disabled``: kill-switch, fuerza caps=0 e ignora el input.
    - ``basic`` / ``pro``: si el admin no pasa cap, usa defaults; si pasa,
      respeta lo que mande (no hay tope hard porque el panel muestra warning).
    - ``pay_per_use``: usa exactamente lo que pase el admin (default 0 si falta).

    Compat: acepta los aliases legacy ``"custom"`` → ``pay_per_use`` y
    ``"free"`` → ``disabled`` y los normaliza antes de validar.
    """
    global _tier_cache_ts
    tier = _LEGACY_TIER_ALIASES.get(tier, tier)
    if tier not in VALID_TIERS:
        return False

    defaults = TIER_DEFAULTS[tier]
    if tier == "disabled":
        daily = 0
        monthly = 0
        usd = 0.0
    else:
        daily = int(daily_cap) if daily_cap is not None else int(defaults["daily_cap"])
        monthly = int(monthly_cap) if monthly_cap is not None else int(defaults["monthly_cap"])
        usd = float(monthly_usd) if monthly_usd is not None else float(defaults["monthly_usd"])

    if daily < 0 or monthly < 0 or usd < 0:
        return False

    now_iso = _now_iso()
    try:
        with get_conn() as conn:
            if is_postgres():
                execute(
                    conn,
                    """
                    INSERT INTO x_tier_config (id, tier, daily_cap, monthly_cap, monthly_usd, updated_at)
                    VALUES (1, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        tier = EXCLUDED.tier,
                        daily_cap = EXCLUDED.daily_cap,
                        monthly_cap = EXCLUDED.monthly_cap,
                        monthly_usd = EXCLUDED.monthly_usd,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (tier, daily, monthly, usd, now_iso),
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO x_tier_config (id, tier, daily_cap, monthly_cap, monthly_usd, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        tier = excluded.tier,
                        daily_cap = excluded.daily_cap,
                        monthly_cap = excluded.monthly_cap,
                        monthly_usd = excluded.monthly_usd,
                        updated_at = excluded.updated_at
                    """,
                    (tier, daily, monthly, usd, now_iso),
                )
        _tier_cache_ts = 0
        logger.info(
            "X tier updated: %s (daily=%d, monthly=%d, usd=%.2f)",
            tier, daily, monthly, usd,
        )
        if tier == "disabled":
            disable_all_campaigns()
        return True
    except Exception as exc:
        logger.error("Failed to update x_tier_config: %s", exc)
        return False


# ── Cap enforcement ──────────────────────────────────────────────────────────


def _today_iso() -> str:
    return datetime.now(ART).strftime("%Y-%m-%d")


def _month_iso() -> str:
    return datetime.now(ART).strftime("%Y-%m")


def _date_prefix_expr() -> str:
    return "LEFT(created_at, 10)" if is_postgres() else "SUBSTR(created_at, 1, 10)"


def _month_prefix_expr() -> str:
    return "LEFT(created_at, 7)" if is_postgres() else "SUBSTR(created_at, 1, 7)"


def count_posts_today() -> int:
    """Cuenta posts con ``status='ok'`` en el día ART actual."""
    today = _today_iso()
    expr = _date_prefix_expr()
    try:
        with get_conn() as conn:
            row = query(
                conn,
                f"SELECT COALESCE(SUM(posts_count), 0) AS c FROM x_usage_log "
                f"WHERE status = ? AND {expr} = ?",
                ("ok", today),
            ).fetchone()
        return int(row["c"] if row else 0)
    except Exception as exc:
        logger.warning("count_posts_today failed: %s", exc)
        return 0


def count_posts_this_month() -> int:
    """Cuenta posts con ``status='ok'`` en el mes ART actual."""
    month = _month_iso()
    expr = _month_prefix_expr()
    try:
        with get_conn() as conn:
            row = query(
                conn,
                f"SELECT COALESCE(SUM(posts_count), 0) AS c FROM x_usage_log "
                f"WHERE status = ? AND {expr} = ?",
                ("ok", month),
            ).fetchone()
        return int(row["c"] if row else 0)
    except Exception as exc:
        logger.warning("count_posts_this_month failed: %s", exc)
        return 0


def check_cap(extra_posts: int = 1) -> tuple[bool, str]:
    """Devuelve ``(allowed, reason)`` verificando tier + caps.

    Se chequea agregando *extra_posts* (útil para hilos, que descuentan N del
    cap). ``reason`` es una clave corta: ``ok``, ``disabled_by_tier``,
    ``daily_cap_reached``, ``monthly_cap_reached``.
    """
    tier_cfg = get_tier_config()
    if not tier_cfg["posting_allowed"]:
        return False, "disabled_by_tier"

    extra = max(1, int(extra_posts))

    if tier_cfg["daily_cap"] > 0:
        used_today = count_posts_today()
        if used_today + extra > tier_cfg["daily_cap"]:
            return False, "daily_cap_reached"

    if tier_cfg["monthly_cap"] > 0:
        used_month = count_posts_this_month()
        if used_month + extra > tier_cfg["monthly_cap"]:
            return False, "monthly_cap_reached"

    return True, "ok"


# ── OAuth state (persistent tokens) ──────────────────────────────────────────


def get_oauth_state() -> dict[str, Any]:
    """Devuelve el último estado OAuth guardado (tokens, handle, expiración)."""
    try:
        with get_conn() as conn:
            row = query(
                conn,
                "SELECT access_token, refresh_token, expires_at, handle, updated_at "
                "FROM x_oauth_state WHERE id = 1",
            ).fetchone()
    except Exception as exc:
        logger.warning("Failed to read x_oauth_state: %s", exc)
        return {}
    if not row:
        return {}
    return {
        "access_token": row["access_token"],
        "refresh_token": row["refresh_token"],
        "expires_at": row["expires_at"],
        "handle": row["handle"],
        "updated_at": row["updated_at"],
    }


def save_oauth_state(
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_at: str | None = None,
    handle: str | None = None,
) -> bool:
    """Persiste un nuevo par (access_token, refresh_token). Los ``None`` no pisan."""
    current = get_oauth_state()
    new_access = access_token if access_token is not None else current.get("access_token")
    new_refresh = refresh_token if refresh_token is not None else current.get("refresh_token")
    new_expires = expires_at if expires_at is not None else current.get("expires_at")
    new_handle = handle if handle is not None else current.get("handle")

    now_iso = _now_iso()
    try:
        with get_conn() as conn:
            if is_postgres():
                execute(
                    conn,
                    """
                    INSERT INTO x_oauth_state
                        (id, access_token, refresh_token, expires_at, handle, updated_at)
                    VALUES (1, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        expires_at = EXCLUDED.expires_at,
                        handle = EXCLUDED.handle,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (new_access, new_refresh, new_expires, new_handle, now_iso),
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO x_oauth_state
                        (id, access_token, refresh_token, expires_at, handle, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        access_token = excluded.access_token,
                        refresh_token = excluded.refresh_token,
                        expires_at = excluded.expires_at,
                        handle = excluded.handle,
                        updated_at = excluded.updated_at
                    """,
                    (new_access, new_refresh, new_expires, new_handle, now_iso),
                )
        return True
    except Exception as exc:
        logger.error("Failed to save x_oauth_state: %s", exc)
        return False


# ── Usage log ────────────────────────────────────────────────────────────────


_PREVIEW_MAX = 500


def log_x_post(
    *,
    campaign_key: str,
    status: str,
    post_id: str | None = None,
    response_code: int | None = None,
    error_message: str | None = None,
    preview: str | None = None,
    posts_count: int = 1,
) -> None:
    """Graba una fila en ``x_usage_log`` y actualiza ``x_campaigns.last_run``."""
    if status not in VALID_CAMPAIGN_STATUSES:
        status = "error"

    preview_trim = (preview or "")[:_PREVIEW_MAX] or None
    err_trim = (error_message or "")[:500] or None
    now_iso = _now_iso()

    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO x_usage_log
                    (campaign_key, status, post_id, response_code,
                     error_message, preview, posts_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_key,
                    status,
                    post_id,
                    response_code,
                    err_trim,
                    preview_trim,
                    max(1, int(posts_count)),
                    now_iso,
                ),
            )
    except Exception as exc:
        logger.warning("Failed to log x post: %s", exc)
        return

    if campaign_key in VALID_CAMPAIGN_KEYS:
        record_campaign_run(campaign_key, status)


def query_x_usage(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    campaign_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Listado paginado de intentos de posteo."""
    clauses: list[str] = []
    params: list[Any] = []
    if desde:
        clauses.append("created_at >= ?")
        params.append(f"{desde}T00:00:00")
    if hasta:
        clauses.append("created_at <= ?")
        params.append(f"{hasta}T23:59:59")
    if campaign_key:
        clauses.append("campaign_key = ?")
        params.append(campaign_key)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, campaign_key, status, post_id, response_code, "
        "error_message, preview, posts_count, created_at "
        f"FROM x_usage_log{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    try:
        with get_conn() as conn:
            rows = query(conn, sql, tuple(params)).fetchall()
    except Exception as exc:
        logger.warning("query_x_usage failed: %s", exc)
        return []

    return [
        {
            "id": r["id"],
            "campaign_key": r["campaign_key"],
            "status": r["status"],
            "post_id": r["post_id"],
            "response_code": r["response_code"],
            "error_message": r["error_message"],
            "preview": r["preview"],
            "posts_count": r["posts_count"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def count_x_usage(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    campaign_key: str | None = None,
    status: str | None = None,
) -> int:
    clauses: list[str] = []
    params: list[Any] = []
    if desde:
        clauses.append("created_at >= ?")
        params.append(f"{desde}T00:00:00")
    if hasta:
        clauses.append("created_at <= ?")
        params.append(f"{hasta}T23:59:59")
    if campaign_key:
        clauses.append("campaign_key = ?")
        params.append(campaign_key)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        with get_conn() as conn:
            row = query(conn, f"SELECT COUNT(*) AS c FROM x_usage_log{where}", tuple(params)).fetchone()
        if not row:
            return 0
        return int(row["c"] if hasattr(row, "__getitem__") else row[0])
    except Exception as exc:
        logger.warning("count_x_usage failed: %s", exc)
        return 0


def purge_old_x_usage(days: int = 90) -> int:
    """Borra filas más viejas que ``days`` días."""
    cutoff = (datetime.now(ART) - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            cur = execute(conn, "DELETE FROM x_usage_log WHERE created_at < ?", (cutoff,))
            n = cur.rowcount if hasattr(cur, "rowcount") else 0
        if n:
            logger.info("Purged %d old x_usage_log rows (< %s)", n, cutoff)
        return int(n or 0)
    except Exception as exc:
        logger.warning("purge_old_x_usage failed: %s", exc)
        return 0
