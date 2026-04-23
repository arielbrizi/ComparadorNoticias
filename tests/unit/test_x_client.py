"""Tests for app.x_client — mocks de HTTP sobre X API v2."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import x_client, x_store


@pytest.fixture
def _init(temp_db, monkeypatch):
    x_store.init_x_tables()
    x_store.save_oauth_state(
        access_token="ACCESS",
        refresh_token="REFRESH",
    )
    monkeypatch.setenv("TWITTER_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITTER_CLIENT_SECRET", "csec")
    monkeypatch.delenv("TWITTER_API_BASE", raising=False)
    yield


def _fake_response(status: int, payload: dict | None = None, text: str = ""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = payload or {}
    resp.text = text or ""
    return resp


class _FakeClient:
    """Contextmanager que devuelve respuestas pre-programadas por (method, url)."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class TestPostTweet:
    def test_success(self, _init):
        fake = _FakeClient([_fake_response(200, {"data": {"id": "1", "text": "hi"}})])
        with patch("app.x_client.httpx.Client", return_value=fake):
            res = x_client.post_tweet("hola")
        assert res.post_id == "1"
        assert fake.calls[0]["method"] == "POST"
        assert fake.calls[0]["url"].endswith("/2/tweets")

    def test_401_triggers_refresh_and_retry(self, _init):
        fake = _FakeClient([
            _fake_response(401, {}, "unauthorized"),
            _fake_response(200, {"access_token": "NEW", "refresh_token": "NEW_R", "expires_in": 3600}),
            _fake_response(200, {"data": {"id": "2"}}),
        ])
        with patch("app.x_client.httpx.Client", return_value=fake):
            res = x_client.post_tweet("retry")
        assert res.post_id == "2"
        # 3 llamadas: primer POST 401, refresh token, retry POST 200
        assert len(fake.calls) == 3
        assert x_store.get_oauth_state()["access_token"] == "NEW"

    def test_429_raises_rate_limited(self, _init):
        fake = _FakeClient([_fake_response(429, {"error": "limit"})])
        with patch("app.x_client.httpx.Client", return_value=fake):
            with pytest.raises(x_client.XClientError) as exc:
                x_client.post_tweet("boom")
        assert exc.value.rate_limited is True

    def test_empty_text_rejected(self, _init):
        with pytest.raises(x_client.XClientError):
            x_client.post_tweet("   ")


class TestPostThread:
    def test_chains_in_reply_to(self, _init):
        fake = _FakeClient([
            _fake_response(200, {"data": {"id": "100"}}),
            _fake_response(200, {"data": {"id": "101"}}),
            _fake_response(200, {"data": {"id": "102"}}),
        ])
        with patch("app.x_client.httpx.Client", return_value=fake):
            results = x_client.post_thread(["uno", "dos", "tres"])

        assert [r.post_id for r in results] == ["100", "101", "102"]
        # La segunda llamada debe tener reply.in_reply_to_tweet_id = 100.
        assert fake.calls[1]["kwargs"]["json"]["reply"]["in_reply_to_tweet_id"] == "100"
        assert fake.calls[2]["kwargs"]["json"]["reply"]["in_reply_to_tweet_id"] == "101"


class TestUploadMedia:
    def test_success(self, _init):
        fake = _FakeClient([_fake_response(200, {"media_id_string": "abc123"})])
        with patch("app.x_client.httpx.Client", return_value=fake):
            mid = x_client.upload_media(b"png-bytes", mime="image/png")
        assert mid == "abc123"

    def test_missing_media_id_raises(self, _init):
        fake = _FakeClient([_fake_response(200, {})])
        with patch("app.x_client.httpx.Client", return_value=fake):
            with pytest.raises(x_client.XClientError):
                x_client.upload_media(b"png")


class TestIsConfigured:
    def test_with_db_state(self, _init):
        assert x_client.is_configured() is True

    def test_without_any(self, temp_db, monkeypatch):
        x_store.init_x_tables()
        monkeypatch.delenv("TWITTER_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("TWITTER_REFRESH_TOKEN", raising=False)
        assert x_client.is_configured() is False
