"""
Genera datos para la nube de palabras a partir de los títulos de las noticias.
Extrae frecuencia de términos filtrando stopwords y tokens cortos.
Preserva acentos y ñ para mostrar palabras en español correcto.

Además expone ``render_png`` para convertir la nube a una imagen PNG usada
por la campaña "Nube del día" de la integración con X.
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.article_grouper import _STOPWORDS
from app.models import Article

logger = logging.getLogger(__name__)

MAX_WORDS = 80


def _strip_accents(text: str) -> str:
    """Remove diacritics (but keep ñ) for stopword matching only."""
    nfkd = unicodedata.normalize("NFD", text)
    return re.sub(r"[\u0300-\u036f]", "", nfkd)


_STOPWORDS_EXPANDED: set[str] = set()
for _sw in _STOPWORDS:
    _STOPWORDS_EXPANDED.add(_sw)
    _STOPWORDS_EXPANDED.add(_strip_accents(_sw))


def _tokenize_display(text: str) -> list[str]:
    """Lowercase, strip punctuation, filter stopwords — but keep accents and ñ."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = []
    for t in text.split():
        if len(t) <= 2:
            continue
        if t in _STOPWORDS_EXPANDED or _strip_accents(t) in _STOPWORDS_EXPANDED:
            continue
        tokens.append(t)
    return tokens


def build_wordcloud(
    articles: list[Article],
    hours: int = 24,
) -> list[list[str | int]]:
    """Return the most frequent terms from recent article titles.

    Each element is ``[word, frequency]``, sorted by frequency descending.
    Only articles published within the last *hours* are considered.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    counter: Counter[str] = Counter()
    for art in articles:
        if art.published and art.published.replace(tzinfo=art.published.tzinfo or timezone.utc) < cutoff:
            continue
        tokens = _tokenize_display(art.title)
        counter.update(tokens)

    return [[word, count] for word, count in counter.most_common(MAX_WORDS)]


def render_png(
    words: list[list],
    *,
    width: int = 1200,
    height: int = 675,
    background: str = "#0f172a",
    max_words: int = 60,
    title: str | None = None,
) -> bytes:
    """Renderiza la lista ``[[word, count], ...]`` como PNG listo para subir a X.

    Usa la librería ``wordcloud`` si está disponible; si falla el import, cae
    en un render simple con Pillow para que la campaña pueda seguir corriendo
    en entornos donde no se instaló la dependencia opcional.
    """
    if not words:
        raise ValueError("render_png got no words")

    freqs = {
        w: int(c)
        for pair in words[:max_words]
        if len(pair) >= 2
        for w, c in [(str(pair[0]), pair[1])]
        if w
    }
    if not freqs:
        raise ValueError("render_png got no usable word/freq pairs")

    try:
        from wordcloud import WordCloud  # type: ignore

        wc = WordCloud(
            width=width,
            height=height,
            background_color=background,
            colormap="viridis",
            prefer_horizontal=0.9,
            margin=8,
            collocations=False,
        ).generate_from_frequencies(freqs)
        img = wc.to_image()
    except Exception as exc:
        logger.warning("wordcloud library unavailable (%s), falling back to Pillow", exc)
        img = _pillow_fallback(freqs, width=width, height=height, background=background)

    if title:
        img = _overlay_title(img, title)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pillow_fallback(freqs: dict[str, int], *, width: int, height: int, background: str):
    """Fallback minimalista: lista las top palabras en columnas con tamaño variable.

    No es bonito pero garantiza que la campaña no quede bloqueada si el
    environment no tiene `wordcloud` instalado (ej. Railway sin imagen custom).
    """
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    img = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(img)

    sorted_words = sorted(freqs.items(), key=lambda x: x[1], reverse=True)[:30]
    if not sorted_words:
        return img

    max_f = sorted_words[0][1]
    min_f = sorted_words[-1][1]
    span = max(1, max_f - min_f)

    try:
        base_font_path = None
    except Exception:
        base_font_path = None

    y = 40
    x_cursor = 40
    row_height = 0
    for word, count in sorted_words:
        size = int(22 + (count - min_f) / span * 60)
        try:
            font = ImageFont.truetype(base_font_path, size) if base_font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), word, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if x_cursor + w + 20 > width - 40:
            x_cursor = 40
            y += row_height + 12
            row_height = 0
        if y + h > height - 40:
            break
        draw.text((x_cursor, y), word, fill="#e2e8f0", font=font)
        x_cursor += w + 24
        row_height = max(row_height, h)

    return img


def _overlay_title(img, title: str):
    """Dibuja un título chico arriba a la izquierda sin romper si Pillow falla."""
    try:
        from PIL import ImageDraw, ImageFont  # type: ignore
    except Exception:
        return img
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        return img
    draw.rectangle([(0, 0), (img.width, 36)], fill="#1e293b")
    draw.text((12, 10), title, fill="#f8fafc", font=font)
    return img
