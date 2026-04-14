"""Unit tests for auth.py helper functions: _create_jwt, cookie helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from jose import jwt

from app.auth import _create_jwt, _delete_auth_cookie, _set_auth_cookie
from app.config import JWT_ALGORITHM, JWT_EXPIRE_HOURS, JWT_SECRET


class TestCreateJwt:
    def test_creates_valid_token(self):
        user = {"id": "u1", "email": "test@test.com", "role": "user"}
        token = _create_jwt(user)
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["sub"] == "u1"
        assert payload["email"] == "test@test.com"
        assert payload["role"] == "user"

    def test_admin_role(self):
        user = {"id": "a1", "email": "admin@test.com", "role": "admin"}
        token = _create_jwt(user)
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["role"] == "admin"

    def test_expiry_is_future(self):
        user = {"id": "u1", "email": "test@test.com", "role": "user"}
        token = _create_jwt(user)
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        assert exp > now
        assert exp < now + timedelta(hours=JWT_EXPIRE_HOURS + 1)


class TestSetAuthCookie:
    def test_sets_cookie_with_correct_name(self):
        response = MagicMock()
        _set_auth_cookie(response, "test-token")
        response.set_cookie.assert_called_once()
        call_kwargs = response.set_cookie.call_args
        assert call_kwargs[0][0] == "vs_token" or call_kwargs[1].get("key") == "vs_token"

    def test_httponly_flag(self):
        response = MagicMock()
        _set_auth_cookie(response, "test-token")
        _, kwargs = response.set_cookie.call_args
        assert kwargs.get("httponly") is True

    def test_non_secure_in_dev(self, monkeypatch):
        monkeypatch.setattr("app.auth.BASE_URL", "http://localhost:8000")
        response = MagicMock()
        _set_auth_cookie(response, "test-token")
        _, kwargs = response.set_cookie.call_args
        assert kwargs.get("secure") is False

    def test_secure_in_prod(self, monkeypatch):
        monkeypatch.setattr("app.auth.BASE_URL", "https://vsnews.io")
        response = MagicMock()
        _set_auth_cookie(response, "test-token")
        _, kwargs = response.set_cookie.call_args
        assert kwargs.get("secure") is True


class TestDeleteAuthCookie:
    def test_deletes_correct_cookie(self):
        response = MagicMock()
        _delete_auth_cookie(response)
        response.delete_cookie.assert_called_once_with("vs_token", path="/")
