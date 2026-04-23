"""
Runners de campañas de X (una función por tipo de post).

Cada runner:

1. Lee la config de la campaña desde ``x_store``.
2. Cuando ``enabled=False`` o el tier bloquea posteo, graba ``skipped`` /
   ``disabled_by_tier`` y devuelve sin pegarle a X.
3. Chequea ``check_cap`` y si no pasa, graba ``quota_exceeded``.
4. Genera el contenido usando la información del caller (wordcloud cache,
   lista de grupos del día, etc.).
5. Postea (single tweet o hilo) vía ``x_client`` y graba el resultado.

Los runners no leen ``_groups`` / ``_wordcloud_cache`` directamente: el caller
(scheduler wrapper en ``main.py`` o endpoint admin) les pasa los datos para
que sean fácilmente testeables.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app import x_client, x_store
from app.models import ArticleGroup

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))

MAX_TWEET_CHARS = x_client.MAX_TWEET_CHARS

SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://vs-news.up.railway.app").rstrip("/")


@dataclass
class CampaignResult:
    """Resultado estructurado que devuelven los runners al caller."""
    ok: bool
    status: str            # "ok" | "error" | "skipped" | "disabled_by_tier" | ...
    reason: str = ""
    post_ids: list[str] | None = None
    message: str = ""


# ── Helpers de formato ───────────────────────────────────────────────────────


def _site_url(group_id: str | None = None) -> str:
    if not group_id:
        return SITE_BASE_URL
    return f"{SITE_BASE_URL}/?g={group_id}"


def _today_str() -> str:
    return datetime.now(ART).strftime("%d/%m/%Y")


def _clip(text: str, limit: int) -> str:
    """Cortar a *limit* respetando palabras cuando es posible."""
    if not text or len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp >= int(limit * 0.6):
        cut = cut[:sp]
    return cut.rstrip(" .,;:—-") + "…"


def _fmt_template(template: str, variables: dict[str, Any]) -> str:
    """Interpola ``{placeholders}`` tolerando claves faltantes."""
    out = template or ""
    for key, val in variables.items():
        out = out.replace("{" + key + "}", str(val) if val is not None else "")
    # Limpia placeholders que no se hayan mapeado.
    out = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "", out)
    return out.strip()


def _shorten_to_tweet(text: str, *, reserve: int = 0) -> str:
    """Deja el texto dentro de MAX_TWEET_CHARS menos *reserve* (para URL/hashtags)."""
    limit = MAX_TWEET_CHARS - max(0, reserve)
    if limit <= 0:
        return text[:MAX_TWEET_CHARS]
    return _clip(text, limit)


# ── 1. Nube del día ──────────────────────────────────────────────────────────


def run_cloud_campaign(
    words: list[list[Any]] | None,
    *,
    test: bool = False,
) -> CampaignResult:
    """Postea la nube del día como imagen + texto."""
    cfg = x_store.get_campaign_config("cloud")
    if not cfg:
        return _skipped("cloud", "campaign_missing", test=test)

    if not cfg["enabled"] and not test:
        return _skipped("cloud", "disabled", test=test)

    if not words:
        return _log_and_return("cloud", "skipped", "no_wordcloud_data")

    allowed, cap_reason = x_store.check_cap(extra_posts=1)
    if not allowed:
        return _log_and_return("cloud", cap_reason, cap_reason)

    template = cfg["template"]
    top_pairs = words[: min(10, len(words))]
    top_words_str = ", ".join(str(p[0]) for p in top_pairs if p and p[0])
    variables = {
        "date": _today_str(),
        "top_words": top_words_str,
        "hashtags": template.get("hashtags", ""),
    }
    body = _fmt_template(template.get("text", ""), variables)
    body = _shorten_to_tweet(body)

    media_ids: list[str] | None = None
    if template.get("attach_image", True):
        try:
            from app.wordcloud import render_png
            png = render_png(words, title=f"Nube del día — {_today_str()}")
            media_id = x_client.upload_media(png, mime="image/png")
            media_ids = [media_id]
        except Exception as exc:
            logger.warning("cloud campaign image render/upload failed: %s", exc)
            return _log_and_return(
                "cloud", "error", "image_upload_failed",
                error=str(exc), preview=body,
            )

    return _post_single("cloud", body, media_ids=media_ids)


# ── 2. Noticia del día ───────────────────────────────────────────────────────


def run_topstory_campaign(
    story: dict | None,
    *,
    test: bool = False,
) -> CampaignResult:
    """Postea la noticia del día. *story* es el dict que devuelve ``ai_top_story``.

    Si *story* es ``None`` o ``ai_available=False``, graba skipped.
    """
    cfg = x_store.get_campaign_config("topstory")
    if not cfg:
        return _skipped("topstory", "campaign_missing", test=test)
    if not cfg["enabled"] and not test:
        return _skipped("topstory", "disabled", test=test)

    if not story or not story.get("title"):
        return _log_and_return("topstory", "skipped", "no_top_story")

    allowed, cap_reason = x_store.check_cap(extra_posts=1)
    if not allowed:
        return _log_and_return("topstory", cap_reason, cap_reason)

    template = cfg["template"]
    hashtags = template.get("hashtags", "")
    url = _site_url(story.get("group_id"))

    # Reservamos espacio para url (t.co = ~23 chars) y hashtags.
    reserve = len(url) + len(hashtags) + 10
    title = _shorten_to_tweet(str(story.get("title") or ""), reserve=reserve)

    variables = {
        "title": title,
        "url": url,
        "hashtags": hashtags,
        "date": _today_str(),
        "summary": _clip(str(story.get("summary") or ""), 180),
    }
    body = _fmt_template(template.get("text", ""), variables)
    body = _shorten_to_tweet(body)
    return _post_single("topstory", body)


# ── 3. Resumen semanal ───────────────────────────────────────────────────────


def run_weekly_campaign(
    weekly: dict | None,
    *,
    week_start: str,
    week_end: str,
    test: bool = False,
) -> CampaignResult:
    """Postea el resumen semanal (simple tweet o hilo, según config)."""
    cfg = x_store.get_campaign_config("weekly")
    if not cfg:
        return _skipped("weekly", "campaign_missing", test=test)
    if not cfg["enabled"] and not test:
        return _skipped("weekly", "disabled", test=test)

    themes = (weekly or {}).get("themes") or []
    if not themes:
        return _log_and_return("weekly", "skipped", "no_weekly_data")

    template = cfg["template"]
    hashtags = template.get("hashtags", "")
    use_thread = bool(template.get("thread", True))
    max_posts = max(1, min(int(template.get("thread_max_posts", 4) or 4), 10))

    if use_thread:
        posts = _weekly_as_thread(themes, week_start, week_end, hashtags, max_posts)
    else:
        summary = themes[0].get("summary") or themes[0].get("label") or ""
        variables = {
            "week_start": week_start,
            "week_end": week_end,
            "summary": _clip(str(summary), 180),
            "hashtags": hashtags,
        }
        posts = [_shorten_to_tweet(_fmt_template(template.get("text", ""), variables))]

    allowed, cap_reason = x_store.check_cap(extra_posts=len(posts))
    if not allowed:
        return _log_and_return("weekly", cap_reason, cap_reason)

    if len(posts) == 1:
        return _post_single("weekly", posts[0])
    return _post_thread("weekly", posts)


def _weekly_as_thread(
    themes: list[dict],
    week_start: str,
    week_end: str,
    hashtags: str,
    max_posts: int,
) -> list[str]:
    head = f"📰 Resumen semanal {week_start} → {week_end}\n\n"
    head += f"Los {min(len(themes), max_posts - 1)} temas que marcaron la agenda:"
    out = [_shorten_to_tweet(head)]
    for idx, theme in enumerate(themes[: max_posts - 1], start=1):
        label = str(theme.get("label") or f"Tema {idx}")
        summary = str(theme.get("summary") or "")
        body = f"{idx}. {label}\n\n{_clip(summary, 220 - len(label))}"
        out.append(_shorten_to_tweet(body))
    if hashtags:
        out.append(_shorten_to_tweet(hashtags))
    return out[:max_posts]


# ── 4. Temas del día ─────────────────────────────────────────────────────────


def run_topics_campaign(
    topics_data: dict | None,
    *,
    test: bool = False,
) -> CampaignResult:
    """Postea los temas del día como tweet o hilo breve."""
    cfg = x_store.get_campaign_config("topics")
    if not cfg:
        return _skipped("topics", "campaign_missing", test=test)
    if not cfg["enabled"] and not test:
        return _skipped("topics", "disabled", test=test)

    topics = (topics_data or {}).get("topics") or []
    if not topics:
        return _log_and_return("topics", "skipped", "no_topics_data")

    template = cfg["template"]
    hashtags = template.get("hashtags", "")
    use_thread = bool(template.get("thread", True))
    max_posts = max(1, min(int(template.get("thread_max_posts", 5) or 5), 10))

    items = [
        f"{t.get('emoji', '•')} {t.get('label', '')}".strip()
        for t in topics
        if t.get("label")
    ]
    items = items[: max_posts if use_thread else 5]
    if not items:
        return _log_and_return("topics", "skipped", "no_topic_labels")

    if use_thread and len(items) > 1:
        intro = f"🔥 Temas del día en Argentina — {_today_str()}"
        posts = [_shorten_to_tweet(intro)]
        for idx, item in enumerate(items, start=1):
            posts.append(_shorten_to_tweet(f"{idx}. {item}"))
        if hashtags:
            posts.append(_shorten_to_tweet(hashtags))
        posts = posts[: max_posts + 1]
        allowed, cap_reason = x_store.check_cap(extra_posts=len(posts))
        if not allowed:
            return _log_and_return("topics", cap_reason, cap_reason)
        return _post_thread("topics", posts)

    list_str = " · ".join(items)
    variables = {
        "date": _today_str(),
        "topics_list": list_str,
        "hashtags": hashtags,
    }
    body = _fmt_template(template.get("text", ""), variables)
    body = _shorten_to_tweet(body)
    allowed, cap_reason = x_store.check_cap(extra_posts=1)
    if not allowed:
        return _log_and_return("topics", cap_reason, cap_reason)
    return _post_single("topics", body)


# ── 5. Breaking news ─────────────────────────────────────────────────────────


_last_breaking_at: datetime | None = None
_last_breaking_group_id: str | None = None


def run_breaking_campaign(
    group: ArticleGroup | None,
    *,
    test: bool = False,
) -> CampaignResult:
    """Postea un breaking para un grupo puntual. El caller decide cuándo dispararlo."""
    global _last_breaking_at, _last_breaking_group_id

    cfg = x_store.get_campaign_config("breaking")
    if not cfg:
        return _skipped("breaking", "campaign_missing", test=test)
    if not cfg["enabled"] and not test:
        return _skipped("breaking", "disabled", test=test)
    if not group:
        return _log_and_return("breaking", "skipped", "no_group")

    sched = cfg.get("schedule") or {}
    min_sources = int(sched.get("min_source_count", 3) or 3)
    if group.source_count < min_sources and not test:
        return _log_and_return("breaking", "skipped", "below_min_source_count")

    allowed_cats = sched.get("categories") or []
    if allowed_cats and (group.category or "") not in allowed_cats and not test:
        return _log_and_return("breaking", "skipped", "category_not_allowed")

    cooldown = int(sched.get("cooldown_minutes", 60) or 60)
    now = datetime.now(timezone.utc)
    if (
        _last_breaking_at is not None
        and _last_breaking_group_id == group.group_id
        and not test
    ):
        return _log_and_return("breaking", "skipped", "same_group_recent")
    if (
        _last_breaking_at is not None
        and (now - _last_breaking_at) < timedelta(minutes=cooldown)
        and not test
    ):
        return _log_and_return("breaking", "skipped", "cooldown_active")

    allowed, cap_reason = x_store.check_cap(extra_posts=1)
    if not allowed:
        return _log_and_return("breaking", cap_reason, cap_reason)

    template = cfg["template"]
    hashtags = template.get("hashtags", "")
    url = _site_url(group.group_id)
    title = _shorten_to_tweet(group.representative_title, reserve=len(url) + len(hashtags) + 10)

    variables = {
        "title": title,
        "url": url,
        "hashtags": hashtags,
        "date": _today_str(),
        "category": group.category or "",
    }
    body = _fmt_template(template.get("text", ""), variables)
    body = _shorten_to_tweet(body)

    result = _post_single("breaking", body)
    if result.ok:
        _last_breaking_at = now
        _last_breaking_group_id = group.group_id
    return result


# ── Utilidades compartidas ──────────────────────────────────────────────────


def _post_single(
    campaign_key: str,
    body: str,
    *,
    media_ids: list[str] | None = None,
) -> CampaignResult:
    """Ejecuta ``post_tweet`` + logueo estándar."""
    try:
        res = x_client.post_tweet(body, media_ids=media_ids)
    except x_client.XClientError as exc:
        status = "rate_limited" if exc.rate_limited else "error"
        x_store.log_x_post(
            campaign_key=campaign_key,
            status=status,
            response_code=exc.status_code,
            error_message=str(exc),
            preview=body,
        )
        return CampaignResult(ok=False, status=status, reason=str(exc), message=str(exc))
    except Exception as exc:
        x_store.log_x_post(
            campaign_key=campaign_key,
            status="error",
            error_message=str(exc),
            preview=body,
        )
        logger.exception("X campaign %s failed unexpectedly", campaign_key)
        return CampaignResult(ok=False, status="error", reason=str(exc), message=str(exc))

    x_store.log_x_post(
        campaign_key=campaign_key,
        status="ok",
        post_id=res.post_id,
        response_code=200,
        preview=res.text,
    )
    return CampaignResult(ok=True, status="ok", post_ids=[res.post_id], message=res.text)


def _post_thread(campaign_key: str, posts: list[str]) -> CampaignResult:
    """Ejecuta ``post_thread`` + logueo consolidado."""
    if not posts:
        return _log_and_return(campaign_key, "skipped", "empty_thread")
    try:
        results = x_client.post_thread(posts)
    except x_client.XClientError as exc:
        status = "rate_limited" if exc.rate_limited else "error"
        x_store.log_x_post(
            campaign_key=campaign_key,
            status=status,
            response_code=exc.status_code,
            error_message=str(exc),
            preview=posts[0] if posts else "",
            posts_count=len(posts),
        )
        return CampaignResult(ok=False, status=status, reason=str(exc), message=str(exc))
    except Exception as exc:
        x_store.log_x_post(
            campaign_key=campaign_key,
            status="error",
            error_message=str(exc),
            preview=posts[0] if posts else "",
            posts_count=len(posts),
        )
        logger.exception("X thread %s failed unexpectedly", campaign_key)
        return CampaignResult(ok=False, status="error", reason=str(exc), message=str(exc))

    ids = [r.post_id for r in results]
    x_store.log_x_post(
        campaign_key=campaign_key,
        status="ok",
        post_id=",".join(ids),
        response_code=200,
        preview=results[0].text,
        posts_count=len(ids),
    )
    return CampaignResult(
        ok=True, status="ok", post_ids=ids, message=f"thread of {len(ids)} tweets",
    )


def _skipped(campaign_key: str, reason: str, *, test: bool) -> CampaignResult:
    """Atajo para `enabled=False` sin dejar fila en `x_usage_log`."""
    logger.info("X campaign %s skipped (%s, test=%s)", campaign_key, reason, test)
    return CampaignResult(ok=False, status="skipped", reason=reason)


def _log_and_return(
    campaign_key: str,
    status: str,
    reason: str,
    *,
    error: str | None = None,
    preview: str | None = None,
) -> CampaignResult:
    """Graba una fila en ``x_usage_log`` (status y razón) y devuelve fallo."""
    x_store.log_x_post(
        campaign_key=campaign_key,
        status=status if status in x_store.VALID_CAMPAIGN_STATUSES else "error",
        error_message=error or reason,
        preview=preview,
        posts_count=1,
    )
    return CampaignResult(ok=False, status=status, reason=reason, message=error or reason)


# ── Breaking detection ───────────────────────────────────────────────────────


def pick_breaking_candidate(
    groups: Iterable[ArticleGroup],
    *,
    min_source_count: int = 3,
    allowed_categories: list[str] | None = None,
    now: datetime | None = None,
    max_age_minutes: int = 180,
) -> ArticleGroup | None:
    """Elige el mejor candidato para un breaking entre los grupos actuales.

    Criterio: el grupo multi-fuente más "fresco" (mayor ``published``) que
    cumple el piso de fuentes y categoría permitida, siempre que haya sido
    publicado dentro de la ventana *max_age_minutes* y que no sea el mismo
    que ya se posteó en la última ventana de cooldown.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max_age_minutes)

    def _age(g: ArticleGroup) -> datetime:
        pub = g.published
        if pub is None:
            return cutoff - timedelta(hours=24)
        return pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc)

    best: ArticleGroup | None = None
    best_pub: datetime | None = None
    for g in groups:
        if g.source_count < min_source_count:
            continue
        if allowed_categories and (g.category or "") not in allowed_categories:
            continue
        if _last_breaking_group_id and g.group_id == _last_breaking_group_id:
            continue
        age = _age(g)
        if age < cutoff:
            continue
        if best_pub is None or age > best_pub:
            best = g
            best_pub = age
    return best


__all__ = [
    "CampaignResult",
    "run_cloud_campaign",
    "run_topstory_campaign",
    "run_weekly_campaign",
    "run_topics_campaign",
    "run_breaking_campaign",
    "pick_breaking_candidate",
]
