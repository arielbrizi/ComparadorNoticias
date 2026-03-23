from app.config import (
    CATEGORIES,
    FETCH_TIMEOUT,
    MAX_ARTICLES_PER_FEED,
    SIMILARITY_THRESHOLD,
    SOURCES,
    USER_AGENT,
)


class TestSources:
    def test_sources_not_empty(self):
        assert len(SOURCES) > 0

    def test_each_source_has_required_fields(self):
        for name, cfg in SOURCES.items():
            assert "color" in cfg, f"{name} missing 'color'"
            assert "feeds" in cfg, f"{name} missing 'feeds'"
            assert isinstance(cfg["color"], str)
            assert cfg["color"].startswith("#"), f"{name} color should be a hex code"

    def test_each_source_has_portada_feed(self):
        for name, cfg in SOURCES.items():
            assert "portada" in cfg["feeds"], f"{name} missing 'portada' feed"

    def test_feed_urls_are_http(self):
        for name, cfg in SOURCES.items():
            for cat, url in cfg["feeds"].items():
                assert url.startswith("http"), (
                    f"{name}/{cat} has invalid URL: {url}"
                )

    def test_known_sources_present(self):
        expected = {"Infobae", "Clarín", "La Nación", "Página 12"}
        assert expected.issubset(set(SOURCES.keys()))


class TestCategories:
    def test_categories_is_list(self):
        assert isinstance(CATEGORIES, list)
        assert len(CATEGORIES) > 0

    def test_portada_in_categories(self):
        assert "portada" in CATEGORIES

    def test_expected_categories(self):
        for cat in ("portada", "politica", "economia", "sociedad", "deportes"):
            assert cat in CATEGORIES


class TestConstants:
    def test_similarity_threshold_range(self):
        assert 0 < SIMILARITY_THRESHOLD <= 100

    def test_max_articles_positive(self):
        assert MAX_ARTICLES_PER_FEED > 0

    def test_fetch_timeout_positive(self):
        assert FETCH_TIMEOUT > 0

    def test_user_agent_not_empty(self):
        assert len(USER_AGENT) > 10
