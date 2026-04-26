"""
Persistencia de uso de IA — tracking de tokens/costo y configuración de provider por evento.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from app.db import execute, get_conn, is_postgres, query

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))

_AI_LOG_PREVIEWS = os.environ.get("AI_LOG_PREVIEWS", "").lower() in ("1", "true", "yes", "on")
_PREVIEW_MAX_CHARS = 2000


def previews_enabled() -> bool:
    """Whether AI prompt/response previews should be persisted."""
    return _AI_LOG_PREVIEWS


def _should_persist_prompt_on_error(provider: str) -> bool:
    """Whether to persist ``prompt_preview`` on a failed call even when
    ``AI_LOG_PREVIEWS`` is off.

    Ollama timeouts are the main case we need to debug post-mortem (was the
    prompt too long? did the model actually receive it?), and unlike cloud
    providers the payload is local so the PII exposure is already bounded to
    our own DB. We extend this to any provider string that involves Ollama
    (including fallback combinations) so the preview survives regardless of
    which side ended up being the one that failed.
    """
    return "ollama" in (provider or "").lower()

# ── Pricing (USD per 1M tokens) ──────────────────────────────────────────────

MODEL_PRICING: dict[str, dict[str, float]] = {
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "llama-3.3-70b-versatile": {"input": 0.00, "output": 0.00},
    # Ollama is self-hosted → no per-token cost. Keys cover the common
    # defaults so the pricing lookup doesn't fall through to the 0 default
    # for every new install.
    "qwen3:8b": {"input": 0.00, "output": 0.00},
    "qwen2.5:7b-instruct": {"input": 0.00, "output": 0.00},
    "llama3.1:8b": {"input": 0.00, "output": 0.00},
    "llama3.2:3b": {"input": 0.00, "output": 0.00},
    "mistral:7b-instruct": {"input": 0.00, "output": 0.00},
}

VALID_EVENT_TYPES = frozenset(
    {"search", "search_prefetch", "topics", "weekly_summary", "top_story"}
)
VALID_PROVIDERS = frozenset({"gemini", "groq", "ollama"})
MAX_PROVIDER_CHAIN = 4

# ── Provider quota limits ────────────────────────────────────────────────────
#
# Rate/quota limits published by each provider for the model we actually use,
# expressed per ventana:
#   rpm = requests per minute  | tpm = tokens per minute
#   rpd = requests per day     | tpd = tokens per day
#
# None means "sin límite conocido" (por ejemplo, Ollama es self-hosted y no
# tiene límites externos). Los valores son los del portal del proveedor al
# momento del lookup y están pensados para repasarse cada mes — el admin
# puede sobre-escribirlos desde el panel y guardarlos en ai_provider_limits.
#
# LAST_VERIFIED: 2026-04-22 — Gemini (AI Studio free tier gemini-2.5-flash),
# Groq (free tier llama-3.3-70b-versatile). Ajustar cuando cambien.
# Defaults de cupos publicados por los portales. Verificados mirando:
#   - Gemini free tier (aistudio.google.com/rate-limit):
#       reporte comunidad abr-2026 para gemini-3-flash-preview.
#   - Groq free tier (console.groq.com/docs/rate-limits).
# Lookup: 2026-04-22. Repetir una vez al mes y actualizar la fecha.
PROVIDER_LIMIT_DEFAULTS: dict[tuple[str, str], dict[str, int | float | None]] = {
    ("gemini", "gemini-3-flash-preview"): {
        # Preview free tier — Google no publica tabla estática; los números
        # coinciden con lo que devuelve aistudio.google.com/rate-limit para
        # una cuenta sin billing activo. Ajustar desde el admin si se pasa a
        # Tier 1 (≈ 20-25 RPM / 250 RPD).
        "rpm": 5,
        "tpm": 250_000,
        "rpd": 20,
        "tpd": None,
        "monthly_usd": None,
    },
    ("groq", "llama-3.3-70b-versatile"): {
        # Free tier Groq Console. TPM/RPD confirmados en docs oficiales.
        "rpm": 30,
        "tpm": 12_000,
        "rpd": 1_000,
        "tpd": 100_000,
        "monthly_usd": None,
    },
    # Ollama: self-hosted, sin límite externo. Lo dejamos explícito para que
    # get_provider_limits devuelva algo en vez de "desconocido".
    ("ollama", "qwen3:8b"): {
        "rpm": None, "tpm": None, "rpd": None, "tpd": None,
        "monthly_usd": None,
    },
}

_LIMIT_FIELDS: tuple[str, ...] = ("rpm", "tpm", "rpd", "tpd")
_BUDGET_FIELDS: tuple[str, ...] = ("monthly_usd",)
_ALL_LIMIT_FIELDS: tuple[str, ...] = _LIMIT_FIELDS + _BUDGET_FIELDS

# ── Table init ────────────────────────────────────────────────────────────────


def init_ai_tables() -> None:
    """Create ai_usage_log and ai_provider_config tables."""
    with get_conn() as conn:
        if is_postgres():
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS ai_usage_log (
                    id              SERIAL PRIMARY KEY,
                    event_type      TEXT NOT NULL,
                    provider        TEXT NOT NULL,
                    model           TEXT NOT NULL,
                    input_tokens    INTEGER NOT NULL,
                    output_tokens   INTEGER NOT NULL,
                    cost_input      REAL NOT NULL,
                    cost_output     REAL NOT NULL,
                    cost_total      REAL NOT NULL,
                    latency_ms      INTEGER,
                    success         INTEGER NOT NULL DEFAULT 1,
                    error_message   TEXT,
                    created_at      TEXT NOT NULL,
                    prompt_preview  TEXT,
                    response_preview TEXT,
                    error_type      TEXT,
                    error_phase     TEXT,
                    http_status     INTEGER,
                    request_sent_at TEXT
                )
                """,
            )
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS ai_usage_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type      TEXT NOT NULL,
                    provider        TEXT NOT NULL,
                    model           TEXT NOT NULL,
                    input_tokens    INTEGER NOT NULL,
                    output_tokens   INTEGER NOT NULL,
                    cost_input      REAL NOT NULL,
                    cost_output     REAL NOT NULL,
                    cost_total      REAL NOT NULL,
                    latency_ms      INTEGER,
                    success         INTEGER NOT NULL DEFAULT 1,
                    error_message   TEXT,
                    created_at      TEXT NOT NULL,
                    prompt_preview  TEXT,
                    response_preview TEXT,
                    error_type      TEXT,
                    error_phase     TEXT,
                    http_status     INTEGER,
                    request_sent_at TEXT
                )
                """,
            )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_aul_created ON ai_usage_log(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_aul_event ON ai_usage_log(event_type)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_aul_provider ON ai_usage_log(provider)")
        # El quota-guard consulta "últimos 60s / 24h por proveedor" en cada
        # invocación; un índice compuesto mantiene la consulta en O(log n).
        execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_aul_provider_created "
            "ON ai_usage_log(provider, created_at)",
        )
        _migrate_ai_usage_log_previews(conn)
        _migrate_ai_usage_log_errors(conn)

        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS ai_provider_config (
                event_type TEXT PRIMARY KEY,
                provider   TEXT NOT NULL DEFAULT '["gemini","groq"]',
                updated_at TEXT NOT NULL
            )
            """,
        )
        _seed_provider_config(conn)

        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS ai_provider_limits (
                provider    TEXT NOT NULL,
                model       TEXT NOT NULL,
                rpm         INTEGER,
                tpm         INTEGER,
                rpd         INTEGER,
                tpd         INTEGER,
                monthly_usd REAL,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (provider, model)
            )
            """,
        )
        _migrate_ai_provider_limits_budget(conn)

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

        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS scheduler_config (
                job_key          TEXT PRIMARY KEY,
                interval_minutes INTEGER NOT NULL,
                updated_at       TEXT NOT NULL
            )
            """,
        )

        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS ai_runtime_config (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )

        if is_postgres():
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS ai_last_good_topics (
                    id           INTEGER PRIMARY KEY DEFAULT 1,
                    topics_json  TEXT NOT NULL,
                    ai_provider  TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    CHECK (id = 1)
                )
                """,
            )
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS ai_last_good_topics (
                    id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    topics_json  TEXT NOT NULL,
                    ai_provider  TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
                """,
            )

    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("AI tables ready — %s", backend)


def _migrate_ai_usage_log_previews(conn) -> None:
    """Add prompt_preview / response_preview columns to an existing ai_usage_log."""
    try:
        if is_postgres():
            execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS prompt_preview TEXT")
            execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS response_preview TEXT")
        else:
            cur = query(conn, "PRAGMA table_info(ai_usage_log)")
            cols = {row["name"] for row in cur.fetchall()}
            if "prompt_preview" not in cols:
                execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN prompt_preview TEXT")
            if "response_preview" not in cols:
                execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN response_preview TEXT")
    except Exception as exc:
        logger.warning("ai_usage_log preview migration failed: %s", exc)


def _migrate_ai_provider_limits_budget(conn) -> None:
    """Add the ``monthly_usd`` column to pre-existing ai_provider_limits rows.

    The column was added together with the monthly USD budget feature. On a
    fresh install the ``CREATE TABLE`` already includes it; this migration
    only kicks in when the table was created by an older version of the app.
    """
    try:
        if is_postgres():
            execute(
                conn,
                "ALTER TABLE ai_provider_limits ADD COLUMN IF NOT EXISTS monthly_usd REAL",
            )
        else:
            cur = query(conn, "PRAGMA table_info(ai_provider_limits)")
            cols = {row["name"] for row in cur.fetchall()}
            if "monthly_usd" not in cols:
                execute(
                    conn,
                    "ALTER TABLE ai_provider_limits ADD COLUMN monthly_usd REAL",
                )
    except Exception as exc:
        logger.warning("ai_provider_limits monthly_usd migration failed: %s", exc)


def _migrate_ai_usage_log_errors(conn) -> None:
    """Add structured error columns (error_type, error_phase, http_status, request_sent_at).

    These let the admin panel distinguish between "request never reached the
    provider" (connect phase) and "request was delivered but timed out while
    waiting for the model" (read phase), which is specially valuable for
    Ollama timeouts. Pre-existing rows get NULL and the UI must tolerate that.
    """
    try:
        if is_postgres():
            execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS error_type TEXT")
            execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS error_phase TEXT")
            execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS http_status INTEGER")
            execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS request_sent_at TEXT")
        else:
            cur = query(conn, "PRAGMA table_info(ai_usage_log)")
            cols = {row["name"] for row in cur.fetchall()}
            if "error_type" not in cols:
                execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN error_type TEXT")
            if "error_phase" not in cols:
                execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN error_phase TEXT")
            if "http_status" not in cols:
                execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN http_status INTEGER")
            if "request_sent_at" not in cols:
                execute(conn, "ALTER TABLE ai_usage_log ADD COLUMN request_sent_at TEXT")
    except Exception as exc:
        logger.warning("ai_usage_log error-columns migration failed: %s", exc)


DEFAULT_PROVIDER_CHAIN: tuple[str, ...] = ("gemini", "groq")


def _seed_provider_config(conn) -> None:
    """Insert default rows for every known event type if missing."""
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    default_chain = json.dumps(list(DEFAULT_PROVIDER_CHAIN))
    for et in sorted(VALID_EVENT_TYPES):
        execute(
            conn,
            """
            INSERT INTO ai_provider_config (event_type, provider, updated_at)
            SELECT ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM ai_provider_config WHERE event_type = ?
            )
            """,
            (et, default_chain, now_iso, et),
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
    prompt_preview: str | None = None,
    response_preview: str | None = None,
    error_type: str | None = None,
    error_phase: str | None = None,
    http_status: int | None = None,
    request_sent_at: str | None = None,
) -> None:
    """Persist a single AI call record.

    Prompt and response previews are only persisted if the env var
    ``AI_LOG_PREVIEWS=1`` is set; otherwise the caller may pass them but
    they're dropped (limits storage and PII risk by default).

    Exception: on a failed call involving Ollama (``success=False`` and the
    provider string contains ``ollama``), ``prompt_preview`` is always
    persisted (truncated to ``_PREVIEW_MAX_CHARS``) so timeouts can be
    diagnosed without flipping the global flag. ``response_preview`` is
    still gated because on errors there's typically no useful response body.

    ``error_type``/``error_phase``/``http_status``/``request_sent_at`` are
    optional structured diagnostics (today populated by ``OllamaCallError``)
    that let the admin panel tell a connect-timeout from a read-timeout.
    """
    cost_in, cost_out = compute_cost(model, input_tokens, output_tokens)
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")

    if _AI_LOG_PREVIEWS:
        pp = (prompt_preview or "")[:_PREVIEW_MAX_CHARS] or None
        rp = (response_preview or "")[:_PREVIEW_MAX_CHARS] or None
    elif not success and _should_persist_prompt_on_error(provider):
        pp = (prompt_preview or "")[:_PREVIEW_MAX_CHARS] or None
        rp = None
    else:
        pp = None
        rp = None

    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO ai_usage_log
                    (event_type, provider, model, input_tokens, output_tokens,
                     cost_input, cost_output, cost_total, latency_ms,
                     success, error_message, created_at,
                     prompt_preview, response_preview,
                     error_type, error_phase, http_status, request_sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    pp,
                    rp,
                    error_type,
                    error_phase,
                    http_status,
                    request_sent_at,
                ),
            )
    except Exception as exc:
        logger.warning("Failed to log AI usage: %s", exc)


# ── Provider config ───────────────────────────────────────────────────────────

_config_cache: dict[str, list[str]] = {}
_config_cache_ts: float = 0
_CONFIG_CACHE_TTL = 30  # seconds


def _legacy_to_chain(raw: str) -> list[str]:
    """Convert a legacy ``ai_provider_config.provider`` value to a chain list.

    Old schema stored enums like ``"gemini_fallback_groq"`` or ``"ollama"`` as
    a single string. We map them to ordered lists without touching the DB —
    the first write from the admin panel will normalize the row to JSON.
    """
    raw = (raw or "").strip()
    if not raw:
        return list(DEFAULT_PROVIDER_CHAIN)
    if "_fallback_" in raw:
        primary, _, secondary = raw.partition("_fallback_")
        chain = [p for p in (primary, secondary) if p in VALID_PROVIDERS]
        return chain or list(DEFAULT_PROVIDER_CHAIN)
    if raw in VALID_PROVIDERS:
        return [raw]
    return list(DEFAULT_PROVIDER_CHAIN)


def _parse_provider_value(raw: str) -> list[str]:
    """Parse a stored ``provider`` column value into an ordered chain."""
    raw = (raw or "").strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return _legacy_to_chain(raw)
        if not isinstance(parsed, list):
            return _legacy_to_chain(raw)
        chain: list[str] = []
        for item in parsed:
            if isinstance(item, str) and item in VALID_PROVIDERS and item not in chain:
                chain.append(item)
        return chain or list(DEFAULT_PROVIDER_CHAIN)
    return _legacy_to_chain(raw)


def get_provider_config() -> dict[str, list[str]]:
    """Return ``{event_type: [providers...]}`` with an ordered fallback chain."""
    global _config_cache, _config_cache_ts
    now = time.time()
    if _config_cache and (now - _config_cache_ts) < _CONFIG_CACHE_TTL:
        return {k: list(v) for k, v in _config_cache.items()}

    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT event_type, provider FROM ai_provider_config").fetchall()
        _config_cache = {
            r["event_type"]: _parse_provider_value(r["provider"]) for r in rows
        }
        _config_cache_ts = now
    except Exception as exc:
        logger.warning("Failed to read AI provider config: %s", exc)
        if not _config_cache:
            _config_cache = {
                et: list(DEFAULT_PROVIDER_CHAIN) for et in VALID_EVENT_TYPES
            }
            _config_cache_ts = now

    return {k: list(v) for k, v in _config_cache.items()}


def set_provider_config(event_type: str, providers: list[str]) -> bool:
    """Update the provider chain for an event type. Returns True on success.

    *providers* must be an ordered, non-empty list of valid provider keys
    without duplicates. The DB column stores the JSON-encoded list.
    """
    global _config_cache_ts
    if event_type not in VALID_EVENT_TYPES:
        return False
    if not isinstance(providers, (list, tuple)):
        return False

    chain: list[str] = []
    for item in providers:
        if not isinstance(item, str) or item not in VALID_PROVIDERS:
            return False
        if item in chain:
            continue  # dedupe defensively
        chain.append(item)

    if not chain or len(chain) > MAX_PROVIDER_CHAIN:
        return False

    encoded = json.dumps(chain)
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            execute(
                conn,
                """
                UPDATE ai_provider_config SET provider = ?, updated_at = ?
                WHERE event_type = ?
                """,
                (encoded, now_iso, event_type),
            )
        _config_cache_ts = 0  # invalidate cache
        logger.info("AI provider config updated: %s → %s", event_type, chain)
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


def query_recent_ai_calls(limit: int = 5) -> list[dict]:
    """Return the most recent ``limit`` rows from ``ai_usage_log``.

    Used by the admin "AI monitor" panel to show the last invocations
    (event, provider, tokens, latency, success/error) at a glance.
    """
    limit = max(1, min(limit, 50))
    try:
        with get_conn() as conn:
            rows = query(
                conn,
                """SELECT id, event_type, provider, model, input_tokens, output_tokens,
                          cost_total, latency_ms, success, error_message, created_at
                     FROM ai_usage_log
                     ORDER BY id DESC
                     LIMIT ?""",
                (limit,),
            ).fetchall()
    except Exception as exc:
        logger.warning("query_recent_ai_calls failed: %s", exc)
        return []

    return [
        {
            "id": r["id"],
            "event_type": r["event_type"],
            "provider": r["provider"],
            "model": r["model"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cost_total": round(r["cost_total"], 6),
            "latency_ms": r["latency_ms"],
            "success": bool(r["success"]),
            "error_message": r["error_message"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _ai_invocations_where(
    desde: str | None,
    hasta: str | None,
    provider: str | None,
    event_type: str | None,
    success: bool | None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if desde:
        clauses.append("created_at >= ?")
        params.append(f"{desde}T00:00:00")
    if hasta:
        clauses.append("created_at <= ?")
        params.append(f"{hasta}T23:59:59")
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if success is not None:
        clauses.append("success = ?")
        params.append(1 if success else 0)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_ai_invocations(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    provider: str | None = None,
    event_type: str | None = None,
    success: bool | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict]:
    """Return a paginated list of AI invocations with preview columns.

    Used by the admin Logs tab. Supports filters by date range, provider,
    event type and success flag.
    """
    where, params = _ai_invocations_where(desde, hasta, provider, event_type, success)
    sql = (
        "SELECT id, event_type, provider, model, input_tokens, output_tokens, "
        "cost_total, latency_ms, success, error_message, created_at, "
        "prompt_preview, response_preview, "
        "error_type, error_phase, http_status, request_sent_at "
        f"FROM ai_usage_log{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    params.extend([int(limit), int(offset)])
    try:
        with get_conn() as conn:
            rows = query(conn, sql, tuple(params)).fetchall()
    except Exception as exc:
        logger.warning("query_ai_invocations failed: %s", exc)
        return []

    def _col(row, name):
        # Rows can be sqlite3.Row (supports __getitem__ by name and raises
        # IndexError for unknown columns) or a psycopg dict-like row. Be
        # defensive in case the migration hasn't run yet on an older DB.
        try:
            return row[name]
        except (KeyError, IndexError):
            return None

    return [
        {
            "id": r["id"],
            "event_type": r["event_type"],
            "provider": r["provider"],
            "model": r["model"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cost_total": round(r["cost_total"], 6),
            "latency_ms": r["latency_ms"],
            "success": bool(r["success"]),
            "error_message": r["error_message"],
            "created_at": r["created_at"],
            "prompt_preview": r["prompt_preview"],
            "response_preview": r["response_preview"],
            "error_type": _col(r, "error_type"),
            "error_phase": _col(r, "error_phase"),
            "http_status": _col(r, "http_status"),
            "request_sent_at": _col(r, "request_sent_at"),
        }
        for r in rows
    ]


def count_ai_invocations(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    provider: str | None = None,
    event_type: str | None = None,
    success: bool | None = None,
) -> int:
    """Return the total number of invocations matching the filter."""
    where, params = _ai_invocations_where(desde, hasta, provider, event_type, success)
    sql = f"SELECT COUNT(*) AS c FROM ai_usage_log{where}"
    try:
        with get_conn() as conn:
            row = query(conn, sql, tuple(params)).fetchone()
        if row is None:
            return 0
        return int(row["c"] if hasattr(row, "__getitem__") else row[0])
    except Exception as exc:
        logger.warning("count_ai_invocations failed: %s", exc)
        return 0


def list_distinct_providers() -> list[str]:
    """Providers that appear in ai_usage_log. Used to populate filter dropdowns."""
    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT DISTINCT provider FROM ai_usage_log ORDER BY provider").fetchall()
        return [r["provider"] for r in rows if r["provider"]]
    except Exception as exc:
        logger.warning("list_distinct_providers failed: %s", exc)
        return []


def query_provider_health(
    provider: str, window_hours: int = 24, recent_n: int = 20,
) -> dict:
    """Return health snapshot for a single provider.

    - ``last_success`` / ``last_error``: most recent timestamps and context.
    - ``recent_calls`` / ``recent_success_count``: success rate across the
      last ``recent_n`` calls (regardless of window).
    - ``errors_last_window``: error count in the last ``window_hours`` hours.
    """
    cutoff = (datetime.now(ART) - timedelta(hours=window_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    try:
        with get_conn() as conn:
            last_success = query(
                conn,
                """SELECT event_type, created_at FROM ai_usage_log
                    WHERE provider = ? AND success = 1
                    ORDER BY id DESC LIMIT 1""",
                (provider,),
            ).fetchone()

            last_error = query(
                conn,
                """SELECT event_type, error_message, created_at FROM ai_usage_log
                    WHERE provider = ? AND success = 0
                    ORDER BY id DESC LIMIT 1""",
                (provider,),
            ).fetchone()

            recent_rows = query(
                conn,
                """SELECT success FROM ai_usage_log
                    WHERE provider = ?
                    ORDER BY id DESC LIMIT ?""",
                (provider, recent_n),
            ).fetchall()

            errors_window = query(
                conn,
                """SELECT COUNT(*) as c FROM ai_usage_log
                    WHERE provider = ? AND success = 0 AND created_at >= ?""",
                (provider, cutoff),
            ).fetchone()
    except Exception as exc:
        logger.warning("query_provider_health(%s) failed: %s", provider, exc)
        return {
            "last_success": None,
            "last_error": None,
            "recent_calls": 0,
            "recent_success_count": 0,
            "errors_last_window": 0,
        }

    recent_calls = len(recent_rows)
    success_count = sum(1 for r in recent_rows if r["success"])

    return {
        "last_success": (
            {"event_type": last_success["event_type"], "created_at": last_success["created_at"]}
            if last_success else None
        ),
        "last_error": (
            {
                "event_type": last_error["event_type"],
                "error_message": (last_error["error_message"] or "")[:240],
                "created_at": last_error["created_at"],
            }
            if last_error else None
        ),
        "recent_calls": recent_calls,
        "recent_success_count": success_count,
        "errors_last_window": errors_window["c"] if errors_window else 0,
    }


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
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
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
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
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


# ── Last good topics (fallback) ──────────────────────────────────────────────


def save_last_good_topics(
    topics: list[dict], ai_provider: str, generated_at: str,
) -> None:
    """Persist the last successfully generated topics for fallback on provider failure."""
    if not topics:
        return
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    topics_json = json.dumps(topics, ensure_ascii=False)
    try:
        with get_conn() as conn:
            if is_postgres():
                execute(
                    conn,
                    """
                    INSERT INTO ai_last_good_topics (id, topics_json, ai_provider, generated_at, updated_at)
                    VALUES (1, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        topics_json  = EXCLUDED.topics_json,
                        ai_provider  = EXCLUDED.ai_provider,
                        generated_at = EXCLUDED.generated_at,
                        updated_at   = EXCLUDED.updated_at
                    """,
                    (topics_json, ai_provider, generated_at, now_iso),
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO ai_last_good_topics (id, topics_json, ai_provider, generated_at, updated_at)
                    VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        topics_json  = excluded.topics_json,
                        ai_provider  = excluded.ai_provider,
                        generated_at = excluded.generated_at,
                        updated_at   = excluded.updated_at
                    """,
                    (topics_json, ai_provider, generated_at, now_iso),
                )
        logger.info("Last good topics saved (%d topics, provider=%s)", len(topics), ai_provider)
    except Exception as exc:
        logger.warning("Failed to save last good topics: %s", exc)


# ── Scheduler config (dynamic intervals) ─────────────────────────────────────

SCHEDULER_DEFAULTS: dict[str, int] = {
    "refresh_news": 10,
    "prefetch_topics": 60,
}

VALID_SCHEDULER_INTERVALS: dict[str, list[int]] = {
    "refresh_news": [5, 10, 15, 20, 30, 60],
    "prefetch_topics": [30, 60, 120, 180, 240, 360],
}

_scheduler_cache: dict[str, int] = {}
_scheduler_cache_ts: float = 0
_SCHEDULER_CACHE_TTL = 30


def get_scheduler_config() -> dict[str, int]:
    """Return {job_key: interval_minutes} for all scheduler jobs."""
    global _scheduler_cache, _scheduler_cache_ts
    now = time.time()
    if _scheduler_cache and (now - _scheduler_cache_ts) < _SCHEDULER_CACHE_TTL:
        return dict(_scheduler_cache)

    result = dict(SCHEDULER_DEFAULTS)
    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT job_key, interval_minutes FROM scheduler_config").fetchall()
        for r in rows:
            result[r["job_key"]] = r["interval_minutes"]
        _scheduler_cache = result
        _scheduler_cache_ts = now
    except Exception as exc:
        logger.warning("Failed to read scheduler config: %s", exc)
        if not _scheduler_cache:
            _scheduler_cache = dict(SCHEDULER_DEFAULTS)
            _scheduler_cache_ts = now

    return dict(_scheduler_cache)


def set_scheduler_interval(job_key: str, interval_minutes: int) -> bool:
    """Update the interval for a scheduler job. Returns True on success."""
    global _scheduler_cache_ts
    if job_key not in VALID_SCHEDULER_INTERVALS:
        return False
    if interval_minutes not in VALID_SCHEDULER_INTERVALS[job_key]:
        return False

    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            if is_postgres():
                execute(
                    conn,
                    """
                    INSERT INTO scheduler_config (job_key, interval_minutes, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (job_key) DO UPDATE SET
                        interval_minutes = EXCLUDED.interval_minutes,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (job_key, interval_minutes, now_iso),
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO scheduler_config (job_key, interval_minutes, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_key) DO UPDATE SET
                        interval_minutes = excluded.interval_minutes,
                        updated_at = excluded.updated_at
                    """,
                    (job_key, interval_minutes, now_iso),
                )
        _scheduler_cache_ts = 0
        logger.info("Scheduler config updated: %s → %d min", job_key, interval_minutes)
        return True
    except Exception as exc:
        logger.error("Failed to update scheduler config: %s", exc)
        return False


# ── Runtime config (key/value) ───────────────────────────────────────────────

OLLAMA_TIMEOUT_DEFAULT = 120
OLLAMA_TIMEOUT_MIN = 30
OLLAMA_TIMEOUT_MAX = 900

_OLLAMA_TIMEOUT_KEY = "ollama_timeout_seconds"

_runtime_cache: dict[str, str] = {}
_runtime_cache_ts: float = 0
_RUNTIME_CACHE_TTL = 30


def _get_runtime_value(key: str) -> str | None:
    """Read a single ai_runtime_config value with a 30s cache."""
    global _runtime_cache, _runtime_cache_ts
    now = time.time()
    if _runtime_cache and (now - _runtime_cache_ts) < _RUNTIME_CACHE_TTL:
        return _runtime_cache.get(key)

    try:
        with get_conn() as conn:
            rows = query(conn, "SELECT key, value FROM ai_runtime_config").fetchall()
        _runtime_cache = {r["key"]: r["value"] for r in rows}
        _runtime_cache_ts = now
    except Exception as exc:
        logger.warning("Failed to read ai_runtime_config: %s", exc)
        return None
    return _runtime_cache.get(key)


def _set_runtime_value(key: str, value: str) -> bool:
    global _runtime_cache_ts
    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            if is_postgres():
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
        _runtime_cache_ts = 0
        return True
    except Exception as exc:
        logger.error("Failed to update ai_runtime_config (%s): %s", key, exc)
        return False


def get_ollama_timeout() -> int:
    """Return the configured Ollama invocation timeout in seconds.

    Falls back to ``OLLAMA_TIMEOUT_DEFAULT`` when no row exists, the value
    is unparseable, or the stored value drifted outside the allowed range.
    """
    raw = _get_runtime_value(_OLLAMA_TIMEOUT_KEY)
    if raw is None:
        return OLLAMA_TIMEOUT_DEFAULT
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return OLLAMA_TIMEOUT_DEFAULT
    if val < OLLAMA_TIMEOUT_MIN or val > OLLAMA_TIMEOUT_MAX:
        return OLLAMA_TIMEOUT_DEFAULT
    return val


def set_ollama_timeout(seconds: int) -> bool:
    """Persist a new Ollama timeout. Returns False if out of range."""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return False
    if seconds < OLLAMA_TIMEOUT_MIN or seconds > OLLAMA_TIMEOUT_MAX:
        return False
    ok = _set_runtime_value(_OLLAMA_TIMEOUT_KEY, str(seconds))
    if ok:
        logger.info("Ollama timeout updated: %ds", seconds)
    return ok


def load_last_good_topics() -> dict | None:
    """Load the last successfully generated topics from DB.

    Returns dict with keys ``topics``, ``ai_provider``, ``generated_at``
    or ``None`` if nothing was ever persisted.
    """
    try:
        with get_conn() as conn:
            row = query(
                conn,
                "SELECT topics_json, ai_provider, generated_at FROM ai_last_good_topics WHERE id = 1",
            ).fetchone()
        if not row:
            return None
        topics = json.loads(row["topics_json"])
        return {
            "topics": topics,
            "ai_provider": row["ai_provider"],
            "generated_at": row["generated_at"],
        }
    except Exception as exc:
        logger.warning("Failed to load last good topics: %s", exc)
        return None


# ── Provider limits (quota guard) ─────────────────────────────────────────────

_BASE_PROVIDERS: frozenset[str] = frozenset({"gemini", "groq", "ollama"})

_limits_cache: dict[tuple[str, str], dict[str, int | None]] = {}
_limits_cache_ts: float = 0
_LIMITS_CACHE_TTL = 30

_usage_cache: dict[str, tuple[float, dict[str, int]]] = {}
_USAGE_CACHE_TTL = 10

# Costo por mes/día por proveedor (y "__global__" para el agregado). Se consulta
# en cada precheck del cuota-guard cuando hay un presupuesto USD configurado,
# así que cacheamos con el mismo TTL que ``_usage_cache`` para no martillar la
# DB con un SUM(cost_total) por cada llamada.
_cost_cache: dict[str, tuple[float, dict[str, float]]] = {}
_COST_CACHE_TTL = 10
_GLOBAL_COST_KEY = "__global__"

_GLOBAL_MONTHLY_BUDGET_KEY = "monthly_budget_usd_global"


def _default_limits(provider: str, model: str) -> dict[str, int | float | None]:
    """Return the hardcoded defaults for (provider, model), or all-None."""
    base = PROVIDER_LIMIT_DEFAULTS.get((provider, model))
    if base:
        return {k: base.get(k) for k in _ALL_LIMIT_FIELDS}
    return {k: None for k in _ALL_LIMIT_FIELDS}


def _parse_limit_field(value) -> int | None:
    """Normalize an incoming limit field to ``None`` or a non-negative int."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0:
            return None
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            v = int(s)
        except ValueError:
            return None
        return v if v >= 0 else None
    return None


def _load_provider_limits_from_db() -> dict[tuple[str, str], dict[str, int | float | None]]:
    """Return overrides stored in ``ai_provider_limits`` (only fields present)."""
    out: dict[tuple[str, str], dict[str, int | float | None]] = {}
    try:
        with get_conn() as conn:
            rows = query(
                conn,
                "SELECT provider, model, rpm, tpm, rpd, tpd, monthly_usd "
                "FROM ai_provider_limits",
            ).fetchall()
    except Exception as exc:
        logger.warning("Failed to read ai_provider_limits: %s", exc)
        return out
    for r in rows:
        key = (r["provider"], r["model"])
        out[key] = {f: r[f] for f in _ALL_LIMIT_FIELDS}
    return out


def get_provider_limits() -> dict[tuple[str, str], dict[str, int | float | None]]:
    """Return the merged view of provider limits (defaults + DB overrides).

    The resulting dict is keyed by ``(provider, model)``. A row in the DB is
    authoritative in its entirety: if it exists, the 5 fields (rpm/tpm/rpd/
    tpd/monthly_usd) replace the defaults for that pair (``None`` in a field
    means "no limit"). If no row exists, the hardcoded
    ``PROVIDER_LIMIT_DEFAULTS`` for that pair apply.
    Cached for ``_LIMITS_CACHE_TTL`` seconds.
    """
    global _limits_cache, _limits_cache_ts
    now = time.time()
    if _limits_cache and (now - _limits_cache_ts) < _LIMITS_CACHE_TTL:
        return {k: dict(v) for k, v in _limits_cache.items()}

    merged: dict[tuple[str, str], dict[str, int | float | None]] = {
        key: dict(vals) for key, vals in PROVIDER_LIMIT_DEFAULTS.items()
    }
    overrides = _load_provider_limits_from_db()
    for key, vals in overrides.items():
        merged[key] = {f: vals.get(f) for f in _ALL_LIMIT_FIELDS}

    _limits_cache = {k: dict(v) for k, v in merged.items()}
    _limits_cache_ts = now
    return {k: dict(v) for k, v in merged.items()}


def get_provider_limit(provider: str, model: str) -> dict[str, int | float | None]:
    """Return the limits for a specific ``(provider, model)`` pair."""
    all_limits = get_provider_limits()
    if (provider, model) in all_limits:
        return dict(all_limits[(provider, model)])
    return _default_limits(provider, model)


def is_default_provider_limit(provider: str, model: str) -> bool:
    """True when no admin override exists for ``(provider, model)``."""
    overrides = _load_provider_limits_from_db()
    return (provider, model) not in overrides


def set_provider_limits(
    provider: str,
    model: str,
    rpm: int | None,
    tpm: int | None,
    rpd: int | None,
    tpd: int | None,
    monthly_usd: float | int | None = None,
) -> bool:
    """Persist an override row. ``None`` in a field means "sin límite".

    ``monthly_usd`` es el presupuesto en USD que la cuenta puede gastar en
    ese par ``(provider, model)`` durante el mes calendario en curso (ART).
    De ese mensual se deriva un cap diario auto-ajustado: ver
    :func:`compute_daily_cap`.
    """
    global _limits_cache_ts, _usage_cache, _cost_cache
    if provider not in _BASE_PROVIDERS:
        return False
    if not isinstance(model, str) or not model.strip():
        return False

    values: dict[str, int | float | None] = {}
    for field, raw in (("rpm", rpm), ("tpm", tpm), ("rpd", rpd), ("tpd", tpd)):
        if raw is not None and (
            isinstance(raw, bool) or not isinstance(raw, int) or raw < 0
        ):
            return False
        values[field] = raw

    if monthly_usd is None:
        values["monthly_usd"] = None
    elif isinstance(monthly_usd, bool):
        return False
    elif isinstance(monthly_usd, (int, float)):
        if monthly_usd < 0:
            return False
        values["monthly_usd"] = float(monthly_usd)
    else:
        return False

    now_iso = datetime.now(ART).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with get_conn() as conn:
            if is_postgres():
                execute(
                    conn,
                    """
                    INSERT INTO ai_provider_limits
                        (provider, model, rpm, tpm, rpd, tpd, monthly_usd, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (provider, model) DO UPDATE SET
                        rpm = EXCLUDED.rpm,
                        tpm = EXCLUDED.tpm,
                        rpd = EXCLUDED.rpd,
                        tpd = EXCLUDED.tpd,
                        monthly_usd = EXCLUDED.monthly_usd,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        provider, model,
                        values["rpm"], values["tpm"],
                        values["rpd"], values["tpd"],
                        values["monthly_usd"],
                        now_iso,
                    ),
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO ai_provider_limits
                        (provider, model, rpm, tpm, rpd, tpd, monthly_usd, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, model) DO UPDATE SET
                        rpm = excluded.rpm,
                        tpm = excluded.tpm,
                        rpd = excluded.rpd,
                        tpd = excluded.tpd,
                        monthly_usd = excluded.monthly_usd,
                        updated_at = excluded.updated_at
                    """,
                    (
                        provider, model,
                        values["rpm"], values["tpm"],
                        values["rpd"], values["tpd"],
                        values["monthly_usd"],
                        now_iso,
                    ),
                )
        _limits_cache_ts = 0
        _usage_cache = {}
        _cost_cache = {}
        logger.info(
            "AI provider limit updated: %s/%s → rpm=%s tpm=%s rpd=%s tpd=%s monthly_usd=%s",
            provider, model,
            values["rpm"], values["tpm"], values["rpd"], values["tpd"],
            values["monthly_usd"],
        )
        return True
    except Exception as exc:
        logger.error("Failed to update ai_provider_limits: %s", exc)
        return False


def reset_provider_limits(provider: str, model: str) -> bool:
    """Delete the override row, making the defaults authoritative again."""
    global _limits_cache_ts, _usage_cache, _cost_cache
    if provider not in _BASE_PROVIDERS:
        return False
    try:
        with get_conn() as conn:
            execute(
                conn,
                "DELETE FROM ai_provider_limits WHERE provider = ? AND model = ?",
                (provider, model),
            )
        _limits_cache_ts = 0
        _usage_cache = {}
        _cost_cache = {}
        logger.info("AI provider limit reset to defaults: %s/%s", provider, model)
        return True
    except Exception as exc:
        logger.error("Failed to reset ai_provider_limits: %s", exc)
        return False


def _usage_cutoff(seconds: int) -> str:
    """Return an ISO cutoff string compatible with ``ai_usage_log.created_at``."""
    return (datetime.now(ART) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def query_provider_usage(provider: str) -> dict[str, int]:
    """Return current usage for ``provider`` across the rolling 60s and 24h.

    Uses ``ai_usage_log`` filtered by ``success = 1`` (failed calls don't
    consume the provider-side quota). Returns ``{rpm_used, tpm_used,
    rpd_used, tpd_used}``. Cached per provider for ``_USAGE_CACHE_TTL`` seconds
    to keep the precheck cheap even on bursty traffic.
    """
    global _usage_cache
    now = time.time()
    cached = _usage_cache.get(provider)
    if cached and (now - cached[0]) < _USAGE_CACHE_TTL:
        return dict(cached[1])

    minute_cutoff = _usage_cutoff(60)
    day_cutoff = _usage_cutoff(60 * 60 * 24)

    result = {"rpm_used": 0, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0}
    try:
        with get_conn() as conn:
            minute_row = query(
                conn,
                """SELECT COUNT(*) AS c,
                          COALESCE(SUM(input_tokens + output_tokens), 0) AS t
                     FROM ai_usage_log
                    WHERE provider = ? AND success = 1 AND created_at >= ?""",
                (provider, minute_cutoff),
            ).fetchone()
            day_row = query(
                conn,
                """SELECT COUNT(*) AS c,
                          COALESCE(SUM(input_tokens + output_tokens), 0) AS t
                     FROM ai_usage_log
                    WHERE provider = ? AND success = 1 AND created_at >= ?""",
                (provider, day_cutoff),
            ).fetchone()
        if minute_row is not None:
            result["rpm_used"] = int(minute_row["c"] or 0)
            result["tpm_used"] = int(minute_row["t"] or 0)
        if day_row is not None:
            result["rpd_used"] = int(day_row["c"] or 0)
            result["tpd_used"] = int(day_row["t"] or 0)
    except Exception as exc:
        logger.warning("query_provider_usage(%s) failed: %s", provider, exc)
        return result

    _usage_cache[provider] = (now, dict(result))
    return result


def invalidate_provider_usage_cache(provider: str | None = None) -> None:
    """Drop the usage and cost cache entries for ``provider`` (or all if ``None``)."""
    global _usage_cache, _cost_cache
    if provider is None:
        _usage_cache = {}
        _cost_cache = {}
    else:
        _usage_cache.pop(provider, None)
        _cost_cache.pop(provider, None)
        _cost_cache.pop(_GLOBAL_COST_KEY, None)


# ── Cost budget (USD/mes) ─────────────────────────────────────────────────────


def _month_cutoff() -> str:
    """Return the ISO start of the current calendar month in ART."""
    now = datetime.now(ART)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:%M:%S")


def _today_cutoff() -> str:
    """Return the ISO start of the current day in ART."""
    now = datetime.now(ART)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:%M:%S")


def _days_remaining_in_month(now: datetime | None = None) -> int:
    """Days remaining in the current month including today (≥1)."""
    if now is None:
        now = datetime.now(ART)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    last_day = (next_month - timedelta(days=1)).day
    return max(1, last_day - now.day + 1)


def compute_daily_cap(
    monthly_budget: float | None,
    month_used: float,
    now: datetime | None = None,
) -> float | None:
    """Return today's USD cap derived from the monthly budget.

    ``daily_cap = max(0, (monthly_budget - month_used) / days_remaining)``

    Si gastamos menos en días previos, ``days_remaining`` repartido sobre la
    plata que sobró deja un cap más grande para hoy. Si nos pasamos, baja a
    cero. Devuelve ``None`` cuando no hay presupuesto configurado para que
    los callers sepan que no hay nada que enforce-ar.
    """
    if monthly_budget is None:
        return None
    try:
        budget = float(monthly_budget)
    except (TypeError, ValueError):
        return None
    if budget < 0:
        return None
    remaining = budget - max(0.0, float(month_used or 0.0))
    if remaining <= 0:
        return 0.0
    days = _days_remaining_in_month(now)
    return remaining / days


def query_provider_cost_window(provider: str, since_iso: str) -> float:
    """Return the total ``cost_total`` (USD, success=1) for ``provider`` since ``since_iso``."""
    try:
        with get_conn() as conn:
            row = query(
                conn,
                """SELECT COALESCE(SUM(cost_total), 0) AS c
                     FROM ai_usage_log
                    WHERE provider = ? AND success = 1 AND created_at >= ?""",
                (provider, since_iso),
            ).fetchone()
        if row is None:
            return 0.0
        return float(row["c"] or 0.0)
    except Exception as exc:
        logger.warning("query_provider_cost_window(%s) failed: %s", provider, exc)
        return 0.0


def query_total_cost_window(since_iso: str) -> float:
    """Return the total ``cost_total`` (USD, success=1) across all providers since ``since_iso``."""
    try:
        with get_conn() as conn:
            row = query(
                conn,
                """SELECT COALESCE(SUM(cost_total), 0) AS c
                     FROM ai_usage_log
                    WHERE success = 1 AND created_at >= ?""",
                (since_iso,),
            ).fetchone()
        if row is None:
            return 0.0
        return float(row["c"] or 0.0)
    except Exception as exc:
        logger.warning("query_total_cost_window failed: %s", exc)
        return 0.0


def query_provider_cost_summary(provider: str) -> dict[str, float]:
    """Return ``{"month_used": float, "today_used": float}`` for ``provider``.

    Cached per provider for ``_COST_CACHE_TTL`` seconds.
    """
    global _cost_cache
    now = time.time()
    cached = _cost_cache.get(provider)
    if cached and (now - cached[0]) < _COST_CACHE_TTL:
        return dict(cached[1])

    result = {
        "month_used": query_provider_cost_window(provider, _month_cutoff()),
        "today_used": query_provider_cost_window(provider, _today_cutoff()),
    }
    _cost_cache[provider] = (now, dict(result))
    return result


def query_global_cost_summary() -> dict[str, float]:
    """Return ``{"month_used": float, "today_used": float}`` aggregated across all providers."""
    global _cost_cache
    now = time.time()
    cached = _cost_cache.get(_GLOBAL_COST_KEY)
    if cached and (now - cached[0]) < _COST_CACHE_TTL:
        return dict(cached[1])

    result = {
        "month_used": query_total_cost_window(_month_cutoff()),
        "today_used": query_total_cost_window(_today_cutoff()),
    }
    _cost_cache[_GLOBAL_COST_KEY] = (now, dict(result))
    return result


def get_global_monthly_budget() -> float | None:
    """Return the configured global USD/month budget, or ``None`` if unset."""
    raw = _get_runtime_value(_GLOBAL_MONTHLY_BUDGET_KEY)
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val < 0:
        return None
    return val


def set_global_monthly_budget(value: float | int | None) -> bool:
    """Persist the global USD/month budget. ``None`` deletes the row.

    Returns ``False`` on invalid values (negative or non-numeric).
    """
    global _runtime_cache_ts, _cost_cache
    if value is None:
        try:
            with get_conn() as conn:
                execute(
                    conn,
                    "DELETE FROM ai_runtime_config WHERE key = ?",
                    (_GLOBAL_MONTHLY_BUDGET_KEY,),
                )
            _runtime_cache_ts = 0
            _cost_cache = {}
            logger.info("Global monthly USD budget cleared")
            return True
        except Exception as exc:
            logger.error("Failed to clear global monthly budget: %s", exc)
            return False

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value < 0:
        return False

    ok = _set_runtime_value(_GLOBAL_MONTHLY_BUDGET_KEY, str(float(value)))
    if ok:
        _cost_cache = {}
        logger.info("Global monthly USD budget updated: $%.4f", float(value))
    return ok
