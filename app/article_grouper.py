"""
Agrupa artículos de diferentes fuentes que tratan sobre la misma noticia.
Usa fuzzy matching sobre títulos para detectar cobertura compartida.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

from app.config import CATEGORIES, SIMILARITY_THRESHOLD
from app.models import Article, ArticleGroup

_CATEGORY_PRIORITY = {cat: i for i, cat in enumerate(CATEGORIES)}

_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
    "en", "con", "por", "para", "que", "se", "su", "sus", "es", "fue", "son",
    "como", "más", "pero", "ya", "muy", "sin", "sobre", "entre", "hasta",
    "desde", "tras", "ante", "bajo", "este", "esta", "estos", "estas", "ese",
    "esa", "esos", "esas", "hay", "ser", "han", "ha", "hoy", "qué", "cuál",
    "y", "o", "a", "e", "le", "lo", "no", "si", "mi", "me", "te", "nos",
    "buenos", "aires", "argentina", "argentino", "argentina", "gobierno",
    "lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo",
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre", "2025", "2026",
    "vivo", "hoy", "ayer", "cuanto", "cotizan", "precio",
    "hora", "dia", "noche", "semana", "minuto", "asi", "paso",
    "sera", "todas", "todos", "donde", "cuando", "quien", "quienes",
    "dijo", "segun", "tambien", "puede", "hace", "tiene", "todo",
    "cada", "otra", "otro", "otras", "otros", "nuevo", "nueva",
}


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = re.sub(r"[\u0300-\u036f]", "", text)  # strip accents
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [t for t in text.split() if t not in _STOPWORDS and len(t) > 2]
    return " ".join(tokens)


def _extract_key_tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def _titles_similar(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0

    words_a = set(na.split())
    words_b = set(nb.split())
    common = words_a & words_b

    if len(common) < 2:
        return 0.0

    jaccard = len(common) / len(words_a | words_b)
    if jaccard < 0.3:
        return 0.0

    fuzzy = fuzz.token_sort_ratio(na, nb)

    return fuzzy * (0.85 + 0.15 * jaccard)


_DAILY_QUOTE_RE = re.compile(
    r"(d[oó]lar|blue|cotiza|riesgo pa[ií]s|merval|acciones|bonos"
    r"|cripto|bitcoin|soja|ma[ií]z|trigo|pesos?|moneda)",
    re.IGNORECASE,
)


def _is_daily_quote(art: Article) -> bool:
    """Detect articles about daily prices/quotes that shouldn't cross days."""
    return bool(_DAILY_QUOTE_RE.search(art.title))


def _time_compatible(a: Article, b: Article, max_hours: int = 48) -> bool:
    if not a.published or not b.published:
        return True
    if _is_daily_quote(a) or _is_daily_quote(b):
        return abs(a.published - b.published) <= timedelta(hours=14)
    return abs(a.published - b.published) <= timedelta(hours=max_hours)


# ── Freshness decay ──────────────────────────────────────────────────────────

_FRESHNESS_MAX_AGE_H = 36
_FRESHNESS_MIN = 0.3
_ANTICIPATORY_AGE_MULT = 2.0

_ANTICIPATORY_RE = re.compile(
    r"(?:"
    r"esta noche|esta tarde|esta mañana|"
    r"a las \d{1,2}|desde las \d{1,2}|a partir de las \d{1,2}|"
    r"\bhablar[áa]\b|\bemitir[áa]\b|\banunciar[áa]\b|"
    r"\bcomenzar[áa]\b|\barrancar[áa]\b|\blanzar[áa]\b|"
    r"\bjugar[áa]n?\b|\bdisputar[áa]n?\b|\benfrentar[áa]n?\b|"
    r"\bse realizar[áa]\b|\bdar[áa] inicio\b|\btendr[áa] lugar\b|"
    r"\bse espera que\b"
    r")",
    re.IGNORECASE,
)


def _is_anticipatory(text: str) -> bool:
    """Detect text about scheduled/upcoming events (future tense + time markers)."""
    return bool(_ANTICIPATORY_RE.search(text))


def _freshness_decay(
    published: datetime | None,
    now: datetime | None = None,
    anticipatory: bool = False,
) -> float:
    """Return a multiplier in [0.3, 1.0] that decays linearly with article age.

    Anticipatory articles (scheduled events) decay 2x faster so that
    'Milei hablará esta noche' sinks once the event has passed.
    """
    if not published:
        return 0.5

    if now is None:
        now = datetime.now(timezone.utc)

    pub = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
    ref = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    age_h = max(0, (ref - pub).total_seconds() / 3600)
    if anticipatory:
        age_h *= _ANTICIPATORY_AGE_MULT

    return max(_FRESHNESS_MIN, 1.0 - age_h / _FRESHNESS_MAX_AGE_H)


# ── Event expiry detection ────────────────────────────────────────────────────

_EVENT_TIME_RE = re.compile(
    r"(?:a las|desde las|a partir de las)\s+(\d{1,2})(?:[:.](\d{2}))?",
    re.IGNORECASE,
)

_EVENT_GRACE_H = 2


def _extract_event_time(text: str, published: datetime) -> datetime | None:
    """Extract a concrete event time from article text.

    Only returns a time when the text contains an explicit hour
    (e.g. "a las 19", "desde las 21:30").  Vague phrases like
    "esta noche" are ignored — we only act when we're sure.
    """
    pub = published if published.tzinfo else published.replace(tzinfo=timezone.utc)

    m = _EVENT_TIME_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            event = pub.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if (pub - event).total_seconds() > 12 * 3600:
                event += timedelta(days=1)
            return event

    return None


_SAME_DAY_RE = re.compile(
    r"esta noche|esta tarde|esta mañana|esta manana",
    re.IGNORECASE,
)


def _is_next_day(published: datetime, now: datetime) -> bool:
    """True when *now* is already on a later calendar day than *published*."""
    pub = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
    ref = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return ref.date() > pub.date()


def is_event_expired(
    group: ArticleGroup,
    now: datetime | None = None,
) -> bool:
    """Check if a group's displayed title announces an event that already passed.

    Two independent checks (either one is enough to expire):

    A) Explicit hour: the text says "a las 19" and now > 19:00 + grace.
    B) Same-day phrase: the text says "esta noche" / "esta tarde" and
       we are already on the next calendar day (the event was today,
       and today is over).
    """
    if not _is_anticipatory(group.representative_title):
        return False
    if not group.published:
        return False

    if now is None:
        now = datetime.now(timezone.utc)
    ref = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    texts = [group.representative_title]
    for a in group.articles:
        texts.append(a.title)
        if a.summary:
            texts.append(a.summary)
    combined = " ".join(texts)

    # Check A: explicit hour
    event_time = _extract_event_time(combined, group.published)
    if event_time is not None:
        evt = event_time if event_time.tzinfo else event_time.replace(tzinfo=timezone.utc)
        if ref > evt + timedelta(hours=_EVENT_GRACE_H):
            return True

    # Check B: "esta noche" / "esta tarde" → expired once we're on the next day
    if _SAME_DAY_RE.search(combined) and _is_next_day(group.published, ref):
        return True

    return False


# ── Sorting ──────────────────────────────────────────────────────────────────

_CAT_BOOST: dict[str, float] = {"politica": 0.5}
_HIGHLIGHT_KEYWORDS = {
    "seleccion", "selección", "mundial", "copa america",
    "copa américa", "eliminatorias", "messi", "scaloni",
}


def sort_groups(
    groups: list[ArticleGroup],
    now: datetime | None = None,
) -> list[ArticleGroup]:
    """Sort groups by editorial importance with freshness decay.

    Anticipatory stories (about scheduled events) lose ranking faster
    so stale 'upcoming event' articles sink below fresh news.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    def _is_group_anticipatory(g: ArticleGroup) -> bool:
        if _is_anticipatory(g.representative_title):
            return True
        return any(_is_anticipatory(a.title) for a in g.articles)

    def _sort_tier(g: ArticleGroup) -> int:
        """Tier 0 = portada with 3+ sources (always first), tier 1 = rest."""
        if g.category == "portada" and g.source_count >= 3:
            return 0
        return 1

    def _sort_score(g: ArticleGroup) -> float:
        boost = _CAT_BOOST.get(g.category, 0)
        if not boost and g.category == "deportes":
            title_lower = g.representative_title.lower()
            if any(kw in title_lower for kw in _HIGHLIGHT_KEYWORDS):
                boost = 1
        base = g.source_count + boost
        antic = _is_group_anticipatory(g)
        decay = _freshness_decay(g.published, now=now, anticipatory=antic)
        return base * decay

    groups.sort(
        key=lambda g: (
            _sort_tier(g),
            -_sort_score(g),
            _CATEGORY_PRIORITY.get(g.category, 99),
            -(g.published.timestamp() if g.published else 0),
        )
    )
    return groups


def group_articles(articles: list[Article]) -> list[ArticleGroup]:
    """
    Group articles covering the same news story using fuzzy title matching.
    Returns groups sorted by number of sources (most coverage first), then by date.
    """
    if not articles:
        return []

    groups: list[list[Article]] = []
    assigned: set[int] = set()

    for i, art_a in enumerate(articles):
        if i in assigned:
            continue

        group = [art_a]
        assigned.add(i)
        sources_in_group = {art_a.source}

        for j, art_b in enumerate(articles):
            if j in assigned:
                continue
            if art_b.source in sources_in_group:
                continue
            if not _time_compatible(art_a, art_b):
                continue

            scores = [
                _titles_similar(member.title, art_b.title)
                for member in group
            ]
            avg_score = sum(scores) / len(scores)
            if avg_score >= SIMILARITY_THRESHOLD:
                group.append(art_b)
                assigned.add(j)
                sources_in_group.add(art_b.source)

        groups.append(group)

    result: list[ArticleGroup] = []
    for group in groups:
        representative = max(group, key=lambda a: len(a.summary))
        most_recent = max(
            (a.published for a in group if a.published),
            default=None,
        )
        gid = hashlib.md5(
            representative.title.encode()
        ).hexdigest()[:10]

        result.append(
            ArticleGroup(
                group_id=gid,
                representative_title=representative.title,
                representative_image=next(
                    (a.image for a in group if a.image), ""
                ),
                category=representative.category,
                published=most_recent,
                articles=sorted(group, key=lambda a: a.source),
            )
        )

    return sort_groups(result)
