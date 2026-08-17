"""Admin session handling and public-endpoint token checks."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_config
from app.db import get_db
from app.services.settings_store import get_settings_row

SESSION_SALT = "jellyttv.session"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_config().session_secret, salt=SESSION_SALT)


def issue_session(response: Response, username: str) -> None:
    cfg = get_config()
    token = _serializer().dumps({"u": username})
    response.set_cookie(
        cfg.session_cookie,
        token,
        max_age=cfg.session_max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(get_config().session_cookie, path="/")


def read_session(request: Request) -> str | None:
    cfg = get_config()
    raw = request.cookies.get(cfg.session_cookie)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=cfg.session_max_age)
    except (BadSignature, SignatureExpired):
        return None
    username = data.get("u") if isinstance(data, dict) else None
    return str(username) if username else None


async def require_admin(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> str:
    row = await get_settings_row(session)
    if not row.setup_complete or not row.admin_password_hash:
        # Pre-setup the API is open so the wizard can run; every write endpoint
        # that matters is gated behind `setup_complete` anyway.
        return "setup"
    username = read_session(request)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in required",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return username


async def require_tuner_token(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    key: Annotated[str | None, Query(description="Tuner access token")] = None,
) -> None:
    """Guard the endpoints Jellyfin (not a browser) calls."""
    row = await get_settings_row(session)
    expected = row.tuner_token
    if not expected:
        return
    provided = key or request.headers.get("x-jellyttv-key")
    if provided == expected:
        return
    # A logged-in admin can also hit these directly from the UI for debugging.
    if read_session(request):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing tuner key")


AdminUser = Annotated[str, Depends(require_admin)]
TunerAuth = Annotated[None, Depends(require_tuner_token)]
