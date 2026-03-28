from datetime import datetime, timedelta, timezone

import pytest

from app.article_grouper import (
    _extract_event_time,
    _freshness_decay,
    _is_anticipatory,
    _is_daily_quote,
    _normalize,
    _time_compatible,
    _titles_similar,
    group_articles,
    is_event_expired,
    sort_groups,
)
from app.config import SIMILARITY_THRESHOLD
from app.models import Article, ArticleGroup


class TestNormalize:
    def test_strips_accents(self):
        assert "inflacion" in _normalize("Inflación")

    def test_removes_stopwords(self):
        result = _normalize("el de la en con por")
        assert result == ""

    def test_removes_short_tokens(self):
        result = _normalize("ya no es un ok")
        assert result == ""

    def test_keeps_significant_words(self):
        result = _normalize("inflación económica récord")
        assert "inflacion" in result
        assert "economica" in result
        assert "record" in result

    def test_strips_punctuation(self):
        result = _normalize("¿Crisis? ¡Sí!")
        assert "crisis" in result


class TestTitlesSimilar:
    def test_identical_titles(self):
        title = "La inflación de marzo fue del 3,5% según el INDEC"
        score = _titles_similar(title, title)
        assert score > 80

    def test_similar_titles_above_threshold(self):
        score = _titles_similar(
            "La inflación de marzo fue del 3,5% según el INDEC",
            "Inflación de marzo: el INDEC reportó una suba del 3,5%",
        )
        assert score >= SIMILARITY_THRESHOLD

    def test_different_titles_below_threshold(self):
        score = _titles_similar(
            "La inflación de marzo fue del 3,5%",
            "River venció 3-0 a Boca en el Superclásico",
        )
        assert score < SIMILARITY_THRESHOLD

    def test_empty_title_returns_zero(self):
        assert _titles_similar("", "Algo") == 0.0
        assert _titles_similar("Algo", "") == 0.0

    def test_both_empty_returns_zero(self):
        assert _titles_similar("", "") == 0.0


class TestIsDailyQuote:
    def test_detects_dollar(self):
        art = Article(source="X", title="Dólar blue: a cuánto cotiza hoy")
        assert _is_daily_quote(art)

    def test_detects_crypto(self):
        art = Article(source="X", title="Bitcoin superó los USD 100.000")
        assert _is_daily_quote(art)

    def test_detects_riesgo_pais(self):
        art = Article(source="X", title="El riesgo país bajó a 800 puntos")
        assert _is_daily_quote(art)

    def test_non_quote_article(self):
        art = Article(source="X", title="Milei viajó a Estados Unidos")
        assert not _is_daily_quote(art)


class TestTimeCompatible:
    def test_close_times_compatible(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="T", published=now)
        b = Article(source="B", title="T", published=now + timedelta(hours=2))
        assert _time_compatible(a, b)

    def test_far_times_incompatible(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="T", published=now)
        b = Article(source="B", title="T", published=now + timedelta(hours=72))
        assert not _time_compatible(a, b)

    def test_none_dates_are_compatible(self):
        a = Article(source="A", title="T")
        b = Article(source="B", title="T")
        assert _time_compatible(a, b)

    def test_daily_quote_short_window(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="Dólar blue hoy", published=now)
        b = Article(source="B", title="Cotización dólar", published=now + timedelta(hours=20))
        assert not _time_compatible(a, b)

    def test_daily_quote_within_window(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="Dólar blue hoy", published=now)
        b = Article(source="B", title="Cotización dólar", published=now + timedelta(hours=5))
        assert _time_compatible(a, b)


class TestGroupArticles:
    def test_empty_input(self):
        assert group_articles([]) == []

    def test_single_article(self):
        art = Article(
            source="A", title="Test noticia",
            published=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        groups = group_articles([art])
        assert len(groups) == 1
        assert len(groups[0].articles) == 1

    def test_similar_articles_grouped(self, sample_articles):
        groups = group_articles(sample_articles)
        inflation_group = None
        for g in groups:
            titles_lower = [a.title.lower() for a in g.articles]
            if any("inflación" in t or "inflacion" in t for t in titles_lower):
                inflation_group = g
                break
        assert inflation_group is not None
        assert len(inflation_group.articles) == 2
        sources = {a.source for a in inflation_group.articles}
        assert "Clarín" in sources
        assert "La Nación" in sources

    def test_different_articles_separate(self, sample_articles):
        groups = group_articles(sample_articles)
        assert len(groups) >= 3

    def test_same_source_not_grouped_together(self, make_article):
        articles = [
            make_article(source="Clarín", title="Inflación subió 3,5% según el INDEC publicó hoy"),
            make_article(source="Clarín", title="La inflación de marzo fue del 3,5% reportó INDEC oficialmente"),
        ]
        groups = group_articles(articles)
        assert len(groups) == 2

    def test_groups_sorted_by_source_count_desc(self, sample_articles):
        groups = group_articles(sample_articles)
        source_counts = [g.source_count for g in groups]
        assert source_counts == sorted(source_counts, reverse=True)

    def test_group_has_representative_title(self, sample_articles):
        groups = group_articles(sample_articles)
        for g in groups:
            assert g.representative_title
            assert g.group_id


class TestIsAnticipatory:
    def test_detects_future_verb_hablara(self):
        assert _is_anticipatory("Milei hablará esta noche por cadena nacional")

    def test_detects_esta_noche(self):
        assert _is_anticipatory("Cadena nacional esta noche")

    def test_detects_time_marker(self):
        assert _is_anticipatory("Conferencia de prensa a las 19")

    def test_detects_se_espera_que(self):
        assert _is_anticipatory("Se espera que el presidente hable hoy")

    def test_detects_comenzara(self):
        assert _is_anticipatory("El acto comenzará a las 17 en Plaza de Mayo")

    def test_detects_jugaran(self):
        assert _is_anticipatory("Argentina y Brasil jugarán en el Monumental")

    def test_past_tense_not_detected(self):
        assert not _is_anticipatory("Milei habló sobre la economía")

    def test_normal_news_not_detected(self):
        assert not _is_anticipatory("La inflación de marzo fue del 3,5%")

    def test_goal_past_not_detected(self):
        assert not _is_anticipatory("River goleó 3-0 a Boca en el Superclásico")

    def test_empty_string(self):
        assert not _is_anticipatory("")


class TestFreshnessDecay:
    def test_fresh_article_no_decay(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        pub = now - timedelta(minutes=30)
        assert _freshness_decay(pub, now=now) > 0.95

    def test_moderate_age_moderate_decay(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        pub = now - timedelta(hours=12)
        decay = _freshness_decay(pub, now=now)
        assert 0.5 < decay < 0.8

    def test_old_article_heavy_decay(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        pub = now - timedelta(hours=30)
        decay = _freshness_decay(pub, now=now)
        assert decay < 0.4

    def test_very_old_article_clamped_to_minimum(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        pub = now - timedelta(hours=72)
        assert _freshness_decay(pub, now=now) == pytest.approx(0.3, abs=0.01)

    def test_anticipatory_decays_faster(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        pub = now - timedelta(hours=6)
        normal = _freshness_decay(pub, now=now, anticipatory=False)
        antic = _freshness_decay(pub, now=now, anticipatory=True)
        assert antic < normal

    def test_none_published_returns_half(self):
        assert _freshness_decay(None) == 0.5

    def test_handles_naive_datetime(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        pub = datetime(2025, 6, 15, 10, 0)  # naive
        decay = _freshness_decay(pub, now=now)
        assert 0.9 < decay < 1.0


class TestExtractEventTime:
    def test_a_las_hh(self):
        pub = datetime(2025, 3, 27, 15, 0, tzinfo=timezone.utc)
        event = _extract_event_time("Conferencia a las 19", pub)
        assert event is not None
        assert event.hour == 19 and event.minute == 0

    def test_a_las_hh_mm(self):
        pub = datetime(2025, 3, 27, 15, 0, tzinfo=timezone.utc)
        event = _extract_event_time("Emisión a las 21:30 por TV", pub)
        assert event is not None
        assert event.hour == 21 and event.minute == 30

    def test_desde_las(self):
        pub = datetime(2025, 3, 27, 10, 0, tzinfo=timezone.utc)
        event = _extract_event_time("Marcha desde las 17 en Plaza de Mayo", pub)
        assert event is not None
        assert event.hour == 17

    def test_vague_phrases_ignored(self):
        """'esta noche' / 'esta tarde' without explicit hour → None."""
        pub = datetime(2025, 3, 27, 18, 0, tzinfo=timezone.utc)
        assert _extract_event_time("Cadena nacional esta noche", pub) is None
        assert _extract_event_time("Anuncio esta tarde en Casa Rosada", pub) is None
        assert _extract_event_time("Conferencia esta mañana en el Congreso", pub) is None

    def test_vague_plus_explicit_uses_explicit(self):
        """'esta noche a las 19' → uses 19:00, ignores vague part."""
        pub = datetime(2025, 3, 27, 15, 0, tzinfo=timezone.utc)
        event = _extract_event_time("Hablará esta noche a las 19 por cadena", pub)
        assert event is not None
        assert event.hour == 19

    def test_no_time_found(self):
        pub = datetime(2025, 3, 27, 15, 0, tzinfo=timezone.utc)
        assert _extract_event_time("La inflación fue del 3,5%", pub) is None

    def test_event_before_publish_wraps_to_next_day(self):
        """Published at 23:00, 'a las 10' → next day at 10:00."""
        pub = datetime(2025, 3, 27, 23, 0, tzinfo=timezone.utc)
        event = _extract_event_time("Reunión a las 10 en el Congreso", pub)
        assert event is not None
        assert event.day == 28 and event.hour == 10

    def test_same_hour_as_publish_stays_same_day(self):
        """Published at 19:18, 'a las 19' → same day (event just started)."""
        pub = datetime(2025, 3, 27, 19, 18, tzinfo=timezone.utc)
        event = _extract_event_time("Se espera que se emita a las 19", pub)
        assert event is not None
        assert event.day == 27 and event.hour == 19


class TestIsEventExpired:
    def test_user_scenario_midnight_after_7pm_event(self, make_article):
        """The exact case: 'hablará esta noche' published 19:18, now 00:58 next day."""
        pub = datetime(2025, 3, 27, 22, 18, tzinfo=timezone.utc)  # 19:18 ART
        now = datetime(2025, 3, 28, 3, 58, tzinfo=timezone.utc)   # 00:58 ART

        group = ArticleGroup(
            group_id="ypf",
            representative_title=(
                "Cadena nacional: Milei hablará esta noche para "
                "celebrar el fallo favorable en la causa YPF"
            ),
            category="portada",
            published=pub,
            articles=[
                make_article(
                    source="Perfil",
                    title="Cadena nacional: Milei hablará esta noche",
                    summary=(
                        "El Presidente graba un mensaje en el Salón Blanco "
                        "de la Casa Rosada y se espera que se emita a las 19."
                    ),
                    published=pub,
                ),
            ],
        )
        assert is_event_expired(group, now)

    def test_not_expired_before_event(self, make_article):
        """Same article but it's only 18:00 — event hasn't happened yet."""
        pub = datetime(2025, 3, 27, 18, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 27, 20, 30, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="pre",
            representative_title="Milei hablará esta noche a las 19",
            category="portada",
            published=pub,
            articles=[
                make_article(source="A", title="Milei hablará esta noche a las 19",
                             published=pub),
            ],
        )
        assert not is_event_expired(group, now)

    def test_not_expired_within_grace_period(self, make_article):
        """Event at 19:00, now 20:30 — within the 2h grace period."""
        pub = datetime(2025, 3, 27, 15, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 27, 20, 30, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="grace",
            representative_title="Conferencia a las 19 en Casa Rosada",
            category="portada",
            published=pub,
            articles=[
                make_article(source="A",
                             title="Conferencia a las 19 en Casa Rosada",
                             published=pub),
            ],
        )
        assert not is_event_expired(group, now)

    def test_past_tense_never_expired(self, make_article):
        """Past-tense titles are not anticipatory → never expired."""
        pub = datetime(2025, 3, 27, 22, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 28, 12, 0, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="past",
            representative_title="Milei habló por cadena nacional sobre YPF",
            category="portada",
            published=pub,
            articles=[
                make_article(source="A",
                             title="Milei habló por cadena nacional sobre YPF",
                             published=pub),
            ],
        )
        assert not is_event_expired(group, now)

    def test_no_time_extractable_not_expired(self, make_article):
        """Anticipatory language but no discernible event time → not expired."""
        pub = datetime(2025, 3, 27, 15, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 28, 12, 0, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="notime",
            representative_title="Se espera que el Congreso debata la ley",
            category="portada",
            published=pub,
            articles=[
                make_article(source="A",
                             title="Se espera que el Congreso debata la ley",
                             published=pub),
            ],
        )
        assert not is_event_expired(group, now)

    def test_esta_noche_expired_next_day(self, make_article):
        """'esta noche' without explicit hour → expired once it's the next day."""
        pub = datetime(2025, 3, 27, 18, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 28, 3, 0, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="noche",
            representative_title="Anunciarán medidas esta noche",
            category="portada",
            published=pub,
            articles=[
                make_article(source="A",
                             title="Anunciarán medidas esta noche",
                             published=pub),
            ],
        )
        assert is_event_expired(group, now)

    def test_esta_noche_not_expired_same_day(self, make_article):
        """'esta noche' on the same day → not expired yet."""
        pub = datetime(2025, 3, 27, 18, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 27, 23, 30, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="noche2",
            representative_title="Anunciarán medidas esta noche",
            category="portada",
            published=pub,
            articles=[
                make_article(source="A",
                             title="Anunciarán medidas esta noche",
                             published=pub),
            ],
        )
        assert not is_event_expired(group, now)

    def test_esta_tarde_expired_next_day(self, make_article):
        """'esta tarde' → expired once it's the next day."""
        pub = datetime(2025, 3, 27, 10, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 28, 2, 0, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="tarde",
            representative_title="Comenzará la marcha esta tarde",
            category="portada",
            published=pub,
            articles=[
                make_article(source="A",
                             title="Comenzará la marcha esta tarde",
                             published=pub),
            ],
        )
        assert is_event_expired(group, now)

    def test_match_event_expired(self, make_article):
        """'Argentina jugará a las 21' — at 00:30 next day → expired."""
        pub = datetime(2025, 3, 27, 15, 0, tzinfo=timezone.utc)
        now = datetime(2025, 3, 28, 0, 30, tzinfo=timezone.utc)

        group = ArticleGroup(
            group_id="partido",
            representative_title="Argentina jugará ante Brasil a las 21",
            category="deportes",
            published=pub,
            articles=[
                make_article(source="A",
                             title="Argentina jugará ante Brasil a las 21",
                             category="deportes", published=pub),
            ],
        )
        assert is_event_expired(group, now)


class TestSortGroupsFreshness:
    def test_fresh_articles_rank_above_stale(self, make_article):
        """With equal source count, a newer article should rank higher."""
        now = datetime(2025, 6, 16, 12, 0, tzinfo=timezone.utc)

        old_group = ArticleGroup(
            group_id="old",
            representative_title="Noticia importante de ayer",
            category="portada",
            published=now - timedelta(hours=18),
            articles=[make_article(source="A", title="Noticia importante de ayer",
                                   published=now - timedelta(hours=18))],
        )
        new_group = ArticleGroup(
            group_id="new",
            representative_title="Noticia importante de ahora",
            category="portada",
            published=now - timedelta(minutes=30),
            articles=[make_article(source="B", title="Noticia importante de ahora",
                                   published=now - timedelta(minutes=30))],
        )

        groups = sort_groups([old_group, new_group], now=now)
        assert groups[0].group_id == "new"

    def test_non_anticipatory_portada_keeps_tier0(self, make_article):
        """A non-anticipatory portada+3 stays tier 0 even when old."""
        now = datetime(2025, 6, 16, 12, 0, tzinfo=timezone.utc)

        arts = [
            make_article(source="A", title="Crisis económica se agravó en todo el país",
                         category="portada", published=now - timedelta(hours=8)),
            make_article(source="B", title="La crisis económica golpea a todo el país",
                         category="portada", published=now - timedelta(hours=7)),
            make_article(source="C", title="Crisis económica: impacto nacional",
                         category="portada", published=now - timedelta(hours=7)),
        ]
        group = ArticleGroup(
            group_id="crisis",
            representative_title="Crisis económica se agravó en todo el país",
            category="portada",
            published=max(a.published for a in arts),
            articles=arts,
        )

        single = ArticleGroup(
            group_id="single",
            representative_title="Noticias menores del día",
            category="portada",
            published=now - timedelta(minutes=10),
            articles=[make_article(source="D", title="Noticias menores del día",
                                   category="portada", published=now - timedelta(minutes=10))],
        )

        groups = sort_groups([single, group], now=now)
        assert groups[0].group_id == "crisis"
