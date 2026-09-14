"""PKCE login against a managed IdP. Google is configured as that IdP's social connection."""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from jarvis.auth import cookie_args, cookie_name, csrf_token, require_csrf, visitor
from jarvis.core.config import settings
from jarvis.oauth import authorization_url, exchange_code, verified_login
from jarvis.routes.chat import engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _failed() -> RedirectResponse:
    return RedirectResponse("/ui/?login=failed", status_code=303)


def _safe_authorize_url(to: str) -> bool:
    expected = urlparse(settings.authorize_endpoint())
    got = urlparse(to)
    return bool(
        to
        and got.scheme == expected.scheme
        and got.netloc.lower() == expected.netloc.lower()
        and got.path == expected.path
        and got.query
        and not got.fragment
        and not got.username
        and not got.password
    )


@router.get("/auth/session")
async def auth_session(request: Request) -> dict[str, object]:
    principal = visitor(request)
    raw = request.cookies.get(cookie_name()) or ""
    return {
        "auth_mode": settings.auth_mode,
        "authenticated": principal is not None,
        "login_path": "/auth/login" if settings.oauth_enabled() else "",
        "user_id": principal.user_id if principal else "",
        "email": principal.email if principal else "",
        "tenant_id": principal.tenant_id if principal else "",
        "scopes": sorted(principal.scopes) if principal else [],
        "csrf": csrf_token(raw) if principal else "",
        "recall_configured": bool(principal and settings.recall_signing_key),
    }


@router.get("/auth/login")
@router.get("/auth/google")
async def auth_login() -> RedirectResponse:
    if not settings.oauth_enabled():
        raise HTTPException(404, "OAuth login is not enabled")
    state, browser, nonce, verifier = engine.access.start_flow()
    dest = authorization_url(state, nonce, verifier)
    response = RedirectResponse("/auth/continue?" + urlencode({"to": dest}), status_code=303)
    response.set_cookie(value=browser, **cookie_args(flow=True, max_age=300))
    return response


@router.get("/auth/continue")
async def auth_continue(to: str = "") -> RedirectResponse:
    if not settings.oauth_enabled():
        raise HTTPException(404, "OAuth login is not enabled")
    if not _safe_authorize_url(to):
        return _failed()
    return RedirectResponse(to, status_code=302)


@router.get("/auth/callback")
@router.get("/auth/google/callback")
async def auth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if not settings.oauth_enabled():
        raise HTTPException(404, "OAuth login is not enabled")
    idp_error = (request.query_params.get("error") or "").strip()
    if idp_error:
        logger.warning("OAuth callback returned error=%s", idp_error)
        return _failed()
    browser = request.cookies.get(cookie_name(flow=True)) or ""
    if not code or not state or len(code) > 2048 or len(state) > 512:
        logger.warning("OAuth callback missing code or state")
        return _failed()
    try:
        flow = engine.access.consume_flow(state, browser)
        tokens = await exchange_code(code, flow["verifier"])
        subject, email, scopes, expires = verified_login(tokens["id_token"], tokens["access_token"], flow["nonce"])
        _, session = engine.access.login(
            subject,
            email,
            request.cookies.get(cookie_name()) or "",
            scopes=scopes,
            access_token=tokens["access_token"],
            access_expires=expires,
        )
    except Exception as exc:
        logger.warning("OAuth callback failed: %s: %s", type(exc).__name__, exc)
        return _failed()
    response = RedirectResponse("/ui/?login=ok", status_code=303)
    response.delete_cookie(cookie_name(flow=True), path="/", secure=settings.public_origin.startswith("https://"))
    response.set_cookie(value=session, **cookie_args(max_age=settings.visitor_session_hours * 3600))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    raw = request.cookies.get(cookie_name()) or ""
    if visitor(request):
        require_csrf(request, raw)
        engine.access.revoke(raw)
    response = JSONResponse({"ok": True})
    response.delete_cookie(cookie_name(), path="/")
    return response
