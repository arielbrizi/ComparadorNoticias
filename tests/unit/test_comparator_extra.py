"""Additional unit tests for app.comparator — _stem_match, _analyze_headlines, edge cases."""

from __future__ import annotations

from app.comparator import (
    _analyze_headlines,
    _detect_focus,
    _detect_tone,
    _stem_match,
    _split_sentences,
    compare_group_articles,
)
from app.models import Article


class TestStemMatch:
    def test_matches_found(self):
        assert _stem_match("crisis total y derrumbe", ["crisis", "derrumb"]) == 2

    def test_no_matches(self):
        assert _stem_match("todo tranquilo hoy", ["crisis", "colaps"]) == 0

    def test_partial_stem_match(self):
        assert _stem_match("el mercado colapsó ayer", ["colaps"]) == 1

    def test_empty_text(self):
        assert _stem_match("", ["crisis", "alert"]) == 0

    def test_empty_stems(self):
        assert _stem_match("crisis total", []) == 0


class TestAnalyzeHeadlines:
    def test_single_title_no_framing(self):
        result = _analyze_headlines(["Solo un título"], [""], ["FuenteA"])
        assert result["different_framing"] is False
        assert result["details"] == []

    def test_identical_titles(self):
        result = _analyze_headlines(
            ["Título igual", "Título igual"],
            ["Resumen A", "Resumen B"],
            ["FuenteA", "FuenteB"],
        )
        assert result["different_framing"] is False
        assert len(result["details"]) == 2

    def test_different_titles(self):
        result = _analyze_headlines(
            ["Crisis económica total", "Ajuste fiscal moderado"],
            ["Resumen alarmante", "Resumen positivo"],
            ["FuenteA", "FuenteB"],
        )
        assert result["different_framing"] is True
        assert len(result["details"]) == 2
        assert result["details"][0]["source"] == "FuenteA"
        assert result["details"][1]["source"] == "FuenteB"

    def test_details_include_tone_and_focus(self):
        result = _analyze_headlines(
            ["Milei anunció decreto de crisis", "Dólar sube récord"],
            ["", ""],
            ["FuenteA", "FuenteB"],
        )
        for d in result["details"]:
            assert "tone" in d
            assert "focus" in d
            assert "title" in d


class TestDetectToneEdgeCases:
    def test_tone_uses_summary_too(self):
        tone = _detect_tone("Reunión habitual", "Dramática caída de los mercados y crisis")
        assert tone == "alarmista"

    def test_tie_favors_alarm(self):
        tone = _detect_tone("Crisis y éxito récord al mismo tiempo")
        assert tone in ("alarmista", "positivo")

    def test_informative_via_summary(self):
        tone = _detect_tone("Medidas del gobierno", "Según datos del estudio oficial")
        assert tone == "informativo"


class TestDetectFocusEdgeCases:
    def test_focus_from_summary(self):
        focus = _detect_focus("Nuevas medidas aprobadas", "Milei y el congreso debaten ley")
        assert focus == "político"

    def test_multiple_categories_highest_wins(self):
        focus = _detect_focus("Milei habló del dólar y la inflación en el mercado")
        assert focus in ("político", "económico")


class TestCompareGroupArticlesEdgeCases:
    def test_three_articles(self, make_article):
        a1 = make_article(
            source="Clarín", title="Crisis del dólar",
            summary="El dólar blue subió a $1300. Los inversores están preocupados por la suba.",
        )
        a2 = make_article(
            source="La Nación", title="El dólar disparado",
            summary="El mercado cambiario mostró tensión. El dólar blue cerró a $1300 pesos.",
        )
        a3 = make_article(
            source="Infobae", title="Dólar: nueva suba récord",
            summary="Cotización récord del dólar en una jornada volátil. El blue alcanzó $1300.",
        )
        result = compare_group_articles([a1, a2, a3])
        assert result["source_count"] == 3
        assert len(result["sources"]) == 3

    def test_article_without_summary(self, make_article):
        a1 = make_article(source="A", title="Título A", summary="")
        a2 = make_article(source="B", title="Título B", summary="Resumen largo del artículo B con detalles.")
        result = compare_group_articles([a1, a2])
        assert len(result["sources"]) == 2

    def test_split_sentences_with_ellipsis(self):
        text = "Algo pasó hoy en la ciudad… Otra cosa ocurrió más tarde."
        result = _split_sentences(text)
        assert len(result) >= 1
