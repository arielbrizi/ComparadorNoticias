"""
Autenticación: Google OAuth + Magic Links + JWT.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jose import JWTError, jwt

from app.config import (
    BASE_URL,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    JWT_ALGORITHM,
    JWT_EXPIRE_HOURS,
    JWT_SECRET,
    MAGIC_LINK_MAX_AGE,
    RESEND_API_KEY,
)
from app.user_store import get_user_by_id, upsert_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_serializer = URLSafeTimedSerializer(JWT_SECRET)

_COOKIE_NAME = "vs_token"


# ── JWT helpers ──────────────────────────────────────────────────────────────


def _create_jwt(user: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub": user["id"],
        "email": user["email"],
        "role": user["role"],
        "exp": expire,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _set_auth_cookie(response, token: str):
    is_prod = BASE_URL.startswith("https")
    expire_dt = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    response.set_cookie(
        _COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=is_prod,
        max_age=JWT_EXPIRE_HOURS * 3600,
        expires=expire_dt,
        path="/",
    )


def _delete_auth_cookie(response):
    response.delete_cookie(_COOKIE_NAME, path="/")


def _redirect_replace(url: str) -> HTMLResponse:
    """Navigate via location.replace() so auth URLs don't stay in browser history."""
    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "</head><body><script>"
        f"window.location.replace({json.dumps(url)})"
        "</script></body></html>"
    )
    return HTMLResponse(content=html)


# ── FastAPI dependencies ─────────────────────────────────────────────────────


async def get_current_user(request: Request) -> dict | None:
    """Read JWT from cookie and return user dict, or None if not authenticated."""
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            return None
        return get_user_by_id(user_id)
    except JWTError:
        return None


async def require_login(user: dict | None = Depends(get_current_user)) -> dict:
    """Raise 401 if the request has no valid session."""
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


async def require_admin(user: dict | None = Depends(get_current_user)) -> dict:
    """Dependency that requires an admin user. Raises 403 otherwise."""
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Google OAuth ─────────────────────────────────────────────────────────────

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


@router.get("/google/login")
async def google_login():
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(501, "Google OAuth not configured")
    redirect_uri = f"{BASE_URL}/auth/google/callback"
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return _redirect_replace(f"{_GOOGLE_AUTH_URL}?{qs}")


@router.get("/google/callback")
async def google_callback(code: str = "", error: str = ""):
    if error or not code:
        logger.warning("Google OAuth error: %s", error)
        return _redirect_replace("/?auth_error=google")

    import httpx

    redirect_uri = f"{BASE_URL}/auth/google/callback"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_data = token_resp.json()

            if "access_token" not in token_data:
                logger.error("Google token exchange failed: %s", token_data)
                return _redirect_replace("/?auth_error=token")

            userinfo_resp = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            userinfo = userinfo_resp.json()
    except Exception as exc:
        logger.error("Google OAuth exchange failed: %s", exc)
        return _redirect_replace("/?auth_error=exchange")

    email = userinfo.get("email", "")
    name = userinfo.get("name", "")
    picture = userinfo.get("picture", "")

    if not email:
        return _redirect_replace("/?auth_error=no_email")

    user = upsert_user(email, name, picture)
    token = _create_jwt(user)

    response = _redirect_replace("/")
    _set_auth_cookie(response, token)
    return response


# ── Magic Links ──────────────────────────────────────────────────────────────


@router.post("/magic/request")
async def magic_request(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    email = body.get("email", "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")

    token = _serializer.dumps(email, salt="magic-link")
    verify_url = f"{BASE_URL}/auth/magic/verify?token={token}"

    if RESEND_API_KEY:
        try:
            import resend

            resend.api_key = RESEND_API_KEY
            resend.Emails.send(
                {
                    "from": "Vs News <noreply@vsnews.io>",
                    "to": [email],
                    "subject": "Tu link de acceso a Vs News",
                    "html": (
                        f"<h2>Hola!</h2>"
                        f"<p>Hacé click en el siguiente link para iniciar sesión en Vs News:</p>"
                        f'<p><a href="{verify_url}" style="font-size:18px;font-weight:bold">'
                        f"Iniciar sesión</a></p>"
                        f"<p>Este link expira en 15 minutos.</p>"
                        f"<p>Si no pediste este acceso, ignorá este email.</p>"
                    ),
                }
            )
        except Exception as exc:
            logger.error("Failed to send magic link email: %s", exc)
            raise HTTPException(500, "Failed to send email")
    else:
        logger.warning("RESEND_API_KEY not set — magic link URL: %s", verify_url)

    return {"ok": True, "message": "Si el email es válido, recibirás un link de acceso."}


@router.get("/magic/verify")
async def magic_verify(token: str = ""):
    if not token:
        return _redirect_replace("/?auth_error=no_token")

    try:
        email = _serializer.loads(token, salt="magic-link", max_age=MAGIC_LINK_MAX_AGE)
    except SignatureExpired:
        return _redirect_replace("/?auth_error=expired")
    except BadSignature:
        return _redirect_replace("/?auth_error=invalid")

    user = upsert_user(email)
    jwt_token = _create_jwt(user)

    response = _redirect_replace("/")
    _set_auth_cookie(response, jwt_token)
    return response


# ── Common endpoints ─────────────────────────────────────────────────────────


@router.get("/me")
async def me(response: Response, user: dict | None = Depends(get_current_user)):
    if not user:
        return {"user": None}
    token = _create_jwt(user)
    _set_auth_cookie(response, token)
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "picture": user["picture"],
            "role": user["role"],
        }
    }


@router.post("/logout")
async def logout():
    response = JSONResponse({"ok": True})
    _delete_auth_cookie(response)
    return response
