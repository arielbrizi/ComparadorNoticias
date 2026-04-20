from app.search_utils import (
    build_fallback_summary,
    extract_keywords,
    group_matches_keywords,
    normalized_query_key,
    prioritize_groups_by_keywords,
)


class TestExtractKeywords:
    def test_empty_query_returns_empty(self):
        assert extract_keywords("") == []
        assert extract_keywords("   ") == []

    def test_single_keyword(self):
        assert extract_keywords("guerra") == ["guerra"]

    def test_strips_spanish_stopwords(self):
        assert extract_keywords("últimos detalles de la guerra") == ["guerra"]

    def test_strips_command_phrasing(self):
        assert extract_keywords("dame el status del dólar hoy") == ["dólar"]

    def test_strips_interrogative(self):
        assert extract_keywords("qué pasó con el partido de Boca River") == [
            "partido", "boca", "river",
        ]

    def test_preserves_multiple_content_words(self):
        result = extract_keywords("guerra Israel Irán")
        assert "guerra" in result
        assert "israel" in result
        assert "irán" in result

    def test_deduplicates(self):
        result = extract_keywords("dólar dólar blue dólar")
        assert result == ["dólar", "blue"]

    def test_ignores_short_tokens(self):
        assert "de" not in extract_keywords("economía de")
        assert extract_keywords("economía de") == ["economía"]

    def test_accented_stopwords_are_filtered(self):
        # "últimos" with accent and "ultimos" without should both be filtered.
        assert extract_keywords("ultimos datos de la economía") == ["datos", "economía"]
        assert extract_keywords("últimos datos de la economía") == ["datos", "economía"]

    def test_empty_when_all_stopwords(self):
        assert extract_keywords("qué pasa hoy") == []

    def test_empty_when_only_short_stopwords(self):
        assert extract_keywords("de la el") == []

    def test_punctuation_is_stripped(self):
        assert extract_keywords("¿qué pasó con Boca-River?") == ["boca", "river"]


class TestNormalizedQueryKey:
    def test_lowercase_strip_accents(self):
        assert normalized_query_key("Guerra Israel-Irán") == "guerra israel-iran"

    def test_trims_whitespace(self):
        assert normalized_query_key("  GUERRA  ") == "guerra"

    def test_empty(self):
        assert normalized_query_key("") == ""


class TestGroupMatchesKeywords:
    def test_matches_title(self, sample_groups):
        assert group_matches_keywords(sample_groups[0], ["inflación"])

    def test_matches_accent_insensitive(self, sample_groups):
        # Query without accent should still match "inflación"
        assert group_matches_keywords(sample_groups[0], ["inflacion"])

    def test_no_match(self, sample_groups):
        assert not group_matches_keywords(sample_groups[0], ["bitcoin"])

    def test_empty_keywords(self, sample_groups):
        assert not group_matches_keywords(sample_groups[0], [])

    def test_matches_summary(self, sample_groups):
        # Inflation group summary mentions "alimentos"
        assert group_matches_keywords(sample_groups[0], ["alimentos"])


class TestPrioritizeGroupsByKeywords:
    def test_matching_groups_first(self, sample_groups):
        # sample_groups[0] is inflation; others are sports/economy
        reordered = prioritize_groups_by_keywords(sample_groups, ["river"])
        # River group should be first
        assert "river" in reordered[0].representative_title.lower()

    def test_preserves_order_within_partitions(self, sample_groups):
        reordered = prioritize_groups_by_keywords(sample_groups, ["zzz-no-match"])
        # When nothing matches, everything goes to "others" preserving input order
        assert reordered == sample_groups

    def test_empty_keywords_returns_copy(self, sample_groups):
        result = prioritize_groups_by_keywords(sample_groups, [])
        assert result == sample_groups
        assert result is not sample_groups  # must be a copy


class TestBuildFallbackSummary:
    def test_empty_titles_returns_empty(self):
        assert build_fallback_summary([], ["guerra"]) == ""

    def test_single_match(self):
        s = build_fallback_summary(["Israel ataca Irán"], ["guerra", "iran"])
        assert "1 noticia" in s
        assert "guerra" in s
        assert "iran" in s
        assert "Israel ataca Irán" in s

    def test_plural_form(self):
        s = build_fallback_summary(
            ["Title A", "Title B"], ["guerra"],
        )
        assert "2 noticias" in s

    def test_limits_to_three_titles(self):
        titles = [f"Title {i}" for i in range(10)]
        s = build_fallback_summary(titles, ["tema"], total=10)
        assert "10 noticias" in s
        assert "Title 0" in s
        assert "Title 2" in s
        assert "Title 3" not in s

    def test_uses_total_when_provided(self):
        s = build_fallback_summary(
            ["A", "B"], ["x"], total=5,
        )
        assert "5 noticias" in s

    def test_works_without_keywords(self):
        s = build_fallback_summary(["Algo pasó"], [])
        assert "1 noticia" in s
        assert "Algo pasó" in s
        assert "sobre" not in s  # no "sobre <keywords>" clause
