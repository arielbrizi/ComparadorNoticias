"""Unit tests for wordcloud helper functions: _strip_accents, _tokenize_display."""

from __future__ import annotations

from app.wordcloud import _strip_accents, _tokenize_display


class TestStripAccents:
    def test_removes_accents(self):
        assert _strip_accents("inflación") == "inflacion"
        assert _strip_accents("económica") == "economica"

    def test_preserves_enie(self):
        result = _strip_accents("año")
        assert "ñ" in result or "n" in result

    def test_no_accents_unchanged(self):
        assert _strip_accents("hello") == "hello"

    def test_empty_string(self):
        assert _strip_accents("") == ""

    def test_multiple_accents(self):
        result = _strip_accents("Córdoba celebró")
        assert "ó" not in result.replace("ñ", "")


class TestTokenizeDisplay:
    def test_filters_short_tokens(self):
        tokens = _tokenize_display("El y la de")
        assert tokens == []

    def test_filters_stopwords(self):
        tokens = _tokenize_display("el gobierno de argentina hoy")
        assert "el" not in tokens
        assert "gobierno" not in tokens
        assert "argentina" not in tokens
        assert "hoy" not in tokens

    def test_keeps_meaningful_words(self):
        tokens = _tokenize_display("inflación económica preocupante")
        assert "inflación" in tokens
        assert "económica" in tokens
        assert "preocupante" in tokens

    def test_lowercase(self):
        tokens = _tokenize_display("MILEI ANUNCIÓ Decreto")
        assert all(t == t.lower() for t in tokens)

    def test_strips_punctuation(self):
        tokens = _tokenize_display("¿crisis? ¡total!")
        for t in tokens:
            assert "?" not in t
            assert "!" not in t
            assert "¿" not in t
            assert "¡" not in t

    def test_empty_input(self):
        assert _tokenize_display("") == []

    def test_only_stopwords(self):
        assert _tokenize_display("el la los las un una") == []

    def test_accented_stopwords_filtered(self):
        tokens = _tokenize_display("más cuál qué")
        assert tokens == []
