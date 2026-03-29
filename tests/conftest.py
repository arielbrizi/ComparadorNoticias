from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.models import Article, ArticleGroup


@pytest.fixture
def make_article():
    """Factory fixture to create Article instances with sensible defaults."""
    def _factory(
        source="Clarín",
        title="Artículo de prueba",
        summary="Resumen del artículo de prueba.",
        link=None,
        category="portada",
        published=None,
        source_color="#1a73e8",
        image="",
        id=None,
    ):
        if link is None:
            link = f"https://example.com/{hashlib.md5(title.encode()).hexdigest()[:8]}"
        if published is None:
            published = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        if id is None:
            id = hashlib.md5(f"{source}:{link}".encode()).hexdigest()[:12]
        return Article(
            id=id,
            source=source,
            source_color=source_color,
            title=title,
            summary=summary,
            link=link,
            image=image,
            category=category,
            published=published,
        )
    return _factory


@pytest.fixture
def sample_articles(make_article):
    """Four articles: two about inflation (should group), one sports, one economy."""
    return [
        make_article(
            source="Clarín",
            title="La inflación de marzo fue del 3,5% según el INDEC",
            summary=(
                "El INDEC informó que la inflación de marzo alcanzó el 3,5 por ciento. "
                "Los alimentos subieron un 4,2%. El acumulado anual llega al 42%."
            ),
            source_color="#1a73e8",
        ),
        make_article(
            source="La Nación",
            title="Inflación de marzo: el INDEC reportó una suba del 3,5%",
            summary=(
                "La inflación interanual se ubicó en el 42%. "
                "El rubro alimentos registró un incremento del 4,2% mensual."
            ),
            source_color="#2d6a4f",
        ),
        make_article(
            source="Infobae",
            title="River Plate goleó 3-0 a Boca en el Superclásico",
            summary=(
                "River Plate venció 3-0 a Boca Juniors en el estadio Monumental. "
                "Los goles fueron de Borja, Solari y Echeverri."
            ),
            source_color="#e63946",
            category="deportes",
        ),
        make_article(
            source="Página 12",
            title="El dólar blue cerró a $1250 en una jornada volátil",
            summary=(
                "El dólar blue operó con alta volatilidad y cerró a $1250. "
                "El MEP quedó en $1180 y el CCL en $1200."
            ),
            source_color="#e76f51",
            category="economia",
        ),
    ]


@pytest.fixture
def sample_groups(sample_articles):
    """Groups built from sample_articles: one multi-source + two single-source."""
    inflation_articles = sample_articles[:2]
    groups = [
        ArticleGroup(
            group_id="abc1234567",
            representative_title=inflation_articles[0].title,
            category="portada",
            published=inflation_articles[0].published,
            articles=inflation_articles,
        ),
    ]
    for art in sample_articles[2:]:
        groups.append(
            ArticleGroup(
                group_id=hashlib.md5(art.title.encode()).hexdigest()[:10],
                representative_title=art.title,
                category=art.category,
                published=art.published,
                articles=[art],
            )
        )
    return groups


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Redirect SQLite to a temp directory for integration tests."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.db._SQLITE_PATH", db_path)
    monkeypatch.setattr("app.db._use_pg", False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return db_path


@pytest.fixture
def make_jwt():
    """Factory fixture to create signed JWT tokens for testing."""
    def _factory(user_id="test-user", email="test@test.com", role="user"):
        expire = datetime.now(timezone.utc) + timedelta(hours=1)
        payload = {"sub": user_id, "email": email, "role": role, "exp": expire}
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return _factory
