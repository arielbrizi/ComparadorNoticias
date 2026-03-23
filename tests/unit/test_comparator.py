from app.comparator import (
    _detect_focus,
    _detect_tone,
    _extract_key_data,
    _normalize_for_compare,
    _sentence_is_in,
    _split_sentences,
    compare_group_articles,
)
from app.models import Article


class TestNormalizeForCompare:
    def test_lowercase_and_strip_accents(self):
        result = _normalize_for_compare("Inflación Económica")
        assert result == "inflacion economica"

    def test_strips_whitespace(self):
        result = _normalize_for_compare("  hello  ")
        assert result == "hello"


class TestSplitSentences:
    def test_multiple_sentences(self):
        text = (
            "La inflación fue muy alta este mes. "
            "El gobierno respondió con medidas urgentes. "
            "Los mercados reaccionaron negativamente."
        )
        result = _split_sentences(text)
        assert len(result) == 3

    def test_filters_short_fragments(self):
        text = "Sí claro. Esta es una oración que tiene más de quince caracteres."
        result = _split_sentences(text)
        assert len(result) == 1

    def test_empty_input(self):
        assert _split_sentences("") == []

    def test_single_long_sentence(self):
        text = "Una sola oración que es bastante larga y no tiene punto al final"
        result = _split_sentences(text)
        assert len(result) == 1


class TestSentenceIsIn:
    def test_exact_substring_match(self):
        assert _sentence_is_in(
            "La inflación de marzo fue del 3,5%",
            "Según el INDEC, la inflación de marzo fue del 3,5% mensual.",
        )

    def test_fuzzy_match(self):
        assert _sentence_is_in(
            "La inflación alcanzó el tres coma cinco por ciento",
            "La inflación de marzo alcanzó el 3,5 por ciento según datos oficiales del INDEC.",
        )

    def test_no_match(self):
        assert not _sentence_is_in(
            "River venció a Boca en el clásico del domingo pasado",
            "La inflación de marzo fue del 3,5%. Los precios subieron mucho.",
        )

    def test_empty_inputs(self):
        assert not _sentence_is_in("", "some text here")
        assert not _sentence_is_in("some text here", "")


class TestExtractKeyData:
    def test_extracts_percentages(self):
        text = "La inflación fue del 3,5% mensual."
        data = _extract_key_data(text)
        assert any("3,5%" in d for d in data)

    def test_extracts_currency_amounts(self):
        text = "El ajuste será de $1250 pesos por unidad."
        data = _extract_key_data(text)
        assert len(data) > 0

    def test_extracts_millions(self):
        text = "La deuda asciende a 500 millones."
        data = _extract_key_data(text)
        assert any("500 millones" in d for d in data)

    def test_extracts_quoted_text(self):
        text = 'El ministro dijo "vamos a bajar la inflación este año" en conferencia.'
        data = _extract_key_data(text)
        assert any("vamos a bajar" in d for d in data)

    def test_empty_text(self):
        assert _extract_key_data("") == []


class TestDetectTone:
    def test_alarmist(self):
        assert _detect_tone("Crisis total: derrumbe de los mercados") == "alarmista"

    def test_positive(self):
        assert _detect_tone("Récord de exportaciones en el trimestre") == "positivo"

    def test_informative(self):
        assert _detect_tone("Cómo es el nuevo plan del gobierno") == "informativo"

    def test_neutral(self):
        assert _detect_tone("Reunión entre mandatarios en la cumbre") == "neutral"


class TestDetectFocus:
    def test_political(self):
        assert _detect_focus("Milei anunció nuevas medidas para el país") == "político"

    def test_economic(self):
        assert _detect_focus("El dólar volvió a subir en la city") == "económico"

    def test_police(self):
        assert _detect_focus("Un muerto en accidente vial en autopista") == "policial"

    def test_sports(self):
        assert _detect_focus("Gol de Messi en el torneo internacional") == "deportivo"

    def test_general(self):
        assert _detect_focus("El clima será templado mañana en todo el país") == "general"


class TestCompareGroupArticles:
    def test_empty_list(self):
        result = compare_group_articles([])
        assert result["sources"] == []

    def test_single_article(self, make_article):
        art = make_article(
            title="Test title",
            summary="A long enough summary text for the analysis module to work properly.",
        )
        result = compare_group_articles([art])
        assert len(result["sources"]) == 1
        assert result["source_count"] == 1

    def test_multiple_articles_structure(self, make_article):
        a1 = make_article(
            source="Clarín",
            title="Inflación subió 3,5% según el INDEC",
            summary=(
                "El INDEC informó que la inflación de marzo alcanzó el 3,5 por ciento. "
                "Los alimentos subieron un 4,2% en el mismo período."
            ),
        )
        a2 = make_article(
            source="La Nación",
            title="Inflación de marzo: 3,5% reportó el INDEC",
            summary=(
                "La inflación interanual se ubicó en el 42 por ciento acumulado. "
                "El rubro alimentos registró un incremento del 4,2% mensual."
            ),
        )
        result = compare_group_articles([a1, a2])
        assert len(result["sources"]) == 2
        assert result["source_count"] == 2
        assert "headline_analysis" in result
        assert result["headline_analysis"]["different_framing"]

    def test_headline_analysis_same_title(self, make_article):
        a1 = make_article(source="A", title="Mismo título exacto", summary="Resumen A largo.")
        a2 = make_article(source="B", title="Mismo título exacto", summary="Resumen B largo.")
        result = compare_group_articles([a1, a2])
        assert not result["headline_analysis"]["different_framing"]
