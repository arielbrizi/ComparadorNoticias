"""
Cliente HTTP mínimo para la API de X (ex-Twitter) v2.

Maneja la autenticación OAuth2 de cuenta única (la app postea siempre como
``@VsNewsAR`` o similar). Los tokens se bootean desde env vars y después se
persisten en ``x_oauth_state`` para sobrevivir a redeploys:

- ``TWITTER_CLIENT_ID`` / ``TWITTER_CLIENT_SECRET``: credenciales de la app.
- ``TWITTER_ACCESS_TOKEN``: token de arranque (opcional si ya hay uno en DB).
- ``TWITTER_REFRESH_TOKEN``: refresh token de arranque (ídem).
- ``TWITTER_API_BASE``: permite apuntar a un mock en tests.
- ``TWITTER_UPLOAD_BASE``: ídem para ``upload.twitter.com``.
- ``TWITTER_ACCOUNT_HANDLE``: opcional, sólo para mostrar en el admin si
  ``GET /2/users/me`` falla.

El flujo de cada request:
1. Toma el access_token de ``x_oauth_state`` (o del env si la DB está vacía).
2. Hace la llamada. Si devuelve 401, corre el refresh (con
   client credentials BasicAuth), guarda los nuevos tokens y reintenta UNA vez.
3. Cualquier otro error se propaga como ``XClientError``.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app import x_store

logger = logging.getLogger(__name__)

ART = timezone(timedelta(hours=-3))

DEFAULT_API_BASE = "https://api.twitter.com"
DEFAULT_UPLOAD_BASE = "https://upload.twitter.com"
DEFAULT_TIMEOUT = 20.0
MAX_TWEET_CHARS = 280


class XClientError(Exception):
    """Error genérico del cliente de X.

    ``status_code`` puede ser None si no llegamos a recibir respuesta HTTP.
    ``rate_limited`` es True sólo cuando X devolvió 429.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        rate_limited: bool = False,
        payload: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.rate_limited = rate_limited
        self.payload = payload


@dataclass
class PostResult:
    post_id: str
    text: str
    raw: dict[str, Any]


# ── Config helpers ────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name, default)
    return raw.strip() if raw else default


def _api_base() -> str:
    return _env("TWITTER_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE


def _upload_base() -> str:
    return _env("TWITTER_UPLOAD_BASE", DEFAULT_UPLOAD_BASE) or DEFAULT_UPLOAD_BASE


def _client_id() -> str:
    return _env("TWITTER_CLIENT_ID")


def _client_secret() -> str:
    return _env("TWITTER_CLIENT_SECRET")


def _env_access_token() -> str:
    return _env("TWITTER_ACCESS_TOKEN")


def _env_refresh_token() -> str:
    return _env("TWITTER_REFRESH_TOKEN")


def is_configured() -> bool:
    """True si al menos hay un access token (env o DB) con que hacer requests.

    No exigimos client_id/secret para ``is_configured`` porque un access token
    todavía válido alcanza para postear; el refresh sí los necesita y loggea
    cuando faltan.
    """
    state = x_store.get_oauth_state()
    if state.get("access_token"):
        return True
    return bool(_env_access_token())


def _current_access_token() -> str:
    state = x_store.get_oauth_state()
    if state.get("access_token"):
        return state["access_token"]
    return _env_access_token()


def _current_refresh_token() -> str:
    state = x_store.get_oauth_state()
    if state.get("refresh_token"):
        return state["refresh_token"]
    return _env_refresh_token()


# ── OAuth2 refresh ────────────────────────────────────────────────────────────


def _refresh_access_token() -> str:
    """Intercambia el refresh_token por un nuevo access_token. Persiste y devuelve el nuevo access."""
    refresh = _current_refresh_token()
    client_id = _client_id()
    client_secret = _client_secret()
    if not refresh:
        raise XClientError("No refresh token available to refresh the access token")
    if not client_id:
        raise XClientError("TWITTER_CLIENT_ID is required to refresh tokens")

    url = f"{_api_base()}/2/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }
    headers: dict[str, str] = {"Content-Type": "application/x-www-form-urlencoded"}

    # Cuentas "confidential" (con client_secret) usan BasicAuth; las "public"
    # (PKCE) mandan sólo el client_id en el body.
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as cli:
            resp = cli.post(url, data=data, headers=headers)
    except httpx.HTTPError as exc:
        raise XClientError(f"Network error during token refresh: {exc}") from exc

    if resp.status_code != 200:
        raise XClientError(
            f"Token refresh failed ({resp.status_code}): {resp.text[:300]}",
            status_code=resp.status_code,
            payload=_safe_json(resp),
        )

    payload = _safe_json(resp) or {}
    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token") or refresh
    expires_in = payload.get("expires_in")

    if not new_access:
        raise XClientError("Token refresh returned no access_token", payload=payload)

    expires_at_iso: str | None = None
    if isinstance(expires_in, (int, float)):
        expires_at_iso = (
            datetime.now(ART) + timedelta(seconds=int(expires_in))
        ).strftime("%Y-%m-%dT%H:%M:%S")

    x_store.save_oauth_state(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=expires_at_iso,
    )
    logger.info("X access_token refreshed (expires_at=%s)", expires_at_iso)
    return new_access


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def _authed_request(
    method: str,
    url: str,
    *,
    json: Any = None,
    data: Any = None,
    files: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    _retried: bool = False,
) -> httpx.Response:
    """Hace un request autenticado; si devuelve 401, refresca y reintenta una vez."""
    token = _current_access_token()
    if not token:
        raise XClientError("No access token configured")

    h = dict(headers or {})
    h["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=timeout) as cli:
            resp = cli.request(method, url, json=json, data=data, files=files, headers=h)
    except httpx.HTTPError as exc:
        raise XClientError(f"Network error on {method} {url}: {exc}") from exc

    if resp.status_code == 401 and not _retried:
        logger.info("X request got 401; refreshing token and retrying once")
        try:
            _refresh_access_token()
        except XClientError:
            raise
        return _authed_request(
            method, url,
            json=json, data=data, files=files, headers=headers,
            timeout=timeout, _retried=True,
        )

    if resp.status_code == 429:
        raise XClientError(
            f"X rate limit hit on {method} {url}",
            status_code=resp.status_code,
            rate_limited=True,
            payload=_safe_json(resp),
        )

    if resp.status_code >= 400:
        raise XClientError(
            f"X API error {resp.status_code} on {method} {url}: {resp.text[:300]}",
            status_code=resp.status_code,
            payload=_safe_json(resp),
        )

    return resp


# ── Public API ───────────────────────────────────────────────────────────────


def get_me() -> dict[str, Any]:
    """GET /2/users/me → devuelve ``{id, username, name}``.

    Persiste el handle en ``x_oauth_state`` para mostrarlo en el admin incluso
    si después la API está caída.
    """
    url = f"{_api_base()}/2/users/me"
    resp = _authed_request("GET", url)
    data = (_safe_json(resp) or {}).get("data") or {}
    username = data.get("username")
    if username:
        x_store.save_oauth_state(handle=f"@{username}")
    return data


def post_tweet(text: str, media_ids: list[str] | None = None) -> PostResult:
    """POST /2/tweets. Devuelve un ``PostResult`` con el id y el texto posteado."""
    return _post_tweet_internal(text=text, media_ids=media_ids, in_reply_to=None)


def post_thread(posts: list[str]) -> list[PostResult]:
    """Postea un hilo: cada post es reply del anterior. Devuelve lista ordenada.

    Si alguno falla, levanta ``XClientError`` inmediatamente (los posts previos
    ya están publicados — el runner los loggea en ``log_x_post`` por separado).
    """
    if not posts:
        raise XClientError("post_thread requires at least one post")

    results: list[PostResult] = []
    parent_id: str | None = None
    for idx, text in enumerate(posts):
        res = _post_tweet_internal(text=text, media_ids=None, in_reply_to=parent_id)
        results.append(res)
        parent_id = res.post_id
    return results


def _post_tweet_internal(
    *, text: str, media_ids: list[str] | None, in_reply_to: str | None,
) -> PostResult:
    trimmed = (text or "").strip()
    if not trimmed:
        raise XClientError("Tweet text is empty")
    if len(trimmed) > MAX_TWEET_CHARS:
        # X cuenta caracteres con reglas específicas (URLs, emojis); el check
        # estricto se hace server-side. Acá sólo cortamos situaciones obvias
        # para no gastar un request a 280+ chars planos.
        trimmed = trimmed[: MAX_TWEET_CHARS]

    body: dict[str, Any] = {"text": trimmed}
    if media_ids:
        body["media"] = {"media_ids": list(media_ids)}
    if in_reply_to:
        body["reply"] = {"in_reply_to_tweet_id": in_reply_to}

    url = f"{_api_base()}/2/tweets"
    resp = _authed_request("POST", url, json=body, headers={"Content-Type": "application/json"})
    data = (_safe_json(resp) or {}).get("data") or {}
    post_id = data.get("id")
    if not post_id:
        raise XClientError("X response missing tweet id", payload=_safe_json(resp))
    return PostResult(post_id=str(post_id), text=trimmed, raw=data)


def upload_media(image_bytes: bytes, *, mime: str = "image/png") -> str:
    """Sube una imagen usando el endpoint simple de media/upload v1.1.

    Devuelve el ``media_id_string`` listo para pasar a ``post_tweet``.
    Para PNG/JPG normales (<5MB) el upload simple alcanza; si en el futuro
    necesitamos videos o GIFs grandes se cambia a chunked (INIT/APPEND/FINALIZE).
    """
    if not image_bytes:
        raise XClientError("upload_media got empty bytes")

    url = f"{_upload_base()}/1.1/media/upload.json"
    files = {"media": ("image.png", io.BytesIO(image_bytes), mime)}
    resp = _authed_request("POST", url, files=files)
    payload = _safe_json(resp) or {}
    media_id = payload.get("media_id_string") or payload.get("media_id")
    if not media_id:
        raise XClientError("X media upload missing media_id_string", payload=payload)
    return str(media_id)
