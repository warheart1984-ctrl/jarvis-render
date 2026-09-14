"""OIDC client for a managed authorization server.

Google is a social connection on that provider. Jarvis never issues ledger JWTs.
Access-token checks follow persistence-memory app/oauth.py: iss, aud, sub, scopes.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from jarvis.core.config import settings
from jarvis.identity import Principal, normalized_subject

READ_SCOPE = "memory.read"
WRITE_SCOPE = "memory.write"


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(state: str, nonce: str, verifier: str) -> str:
    params = {
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.callback_url(),
        "response_type": "code",
        "scope": settings.oidc_scopes,
        "audience": settings.oidc_audience,
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if settings.oidc_connection.strip():
        params["connection"] = settings.oidc_connection.strip()
    return settings.authorize_endpoint() + "?" + urlencode(params)


@lru_cache(maxsize=4)
def _jwk_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    value = claims.get("scope", claims.get("scp", []))
    if isinstance(value, str):
        return frozenset(item for item in value.split() if item)
    if isinstance(value, list):
        return frozenset(str(item) for item in value if isinstance(item, str))
    return frozenset()


def decode_jwt(token: str, *, audience: str, nonce: str | None = None) -> dict[str, Any]:
    key = _jwk_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256", "ES256"],
        audience=audience,
        issuer=settings.identity_issuer(),
        leeway=30,
        options={"require": ["exp", "iat", "sub"]},
    )
    if nonce is not None and claims.get("nonce") != nonce:
        raise jwt.InvalidTokenError("Nonce mismatch")
    return claims


def ledger_principal(access_token: str) -> tuple[Principal, int]:
    claims = decode_jwt(access_token, audience=settings.oidc_audience)
    subject = normalized_subject(claims.get("sub"))
    if subject is None:
        raise jwt.InvalidTokenError("Access token has no valid subject")
    scopes = _scopes(claims)
    if READ_SCOPE not in scopes or WRITE_SCOPE not in scopes:
        raise jwt.InvalidTokenError("Access token lacks memory.read and memory.write")
    return Principal(subject=subject, scopes=scopes, issuer=settings.identity_issuer()), int(claims["exp"])


def identity_claims(id_token: str, nonce: str) -> dict[str, Any]:
    claims = decode_jwt(id_token, audience=settings.oidc_client_id, nonce=nonce)
    subject = normalized_subject(claims.get("sub"))
    email = str(claims.get("email") or "").strip().lower()
    if subject is None or not email or "@" not in email:
        raise jwt.InvalidTokenError("ID token is missing a stable subject or email")
    claims["sub"] = subject
    claims["email"] = email
    return claims


async def exchange_code(code: str, verifier: str) -> dict[str, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            settings.token_endpoint(),
            data={
                "grant_type": "authorization_code",
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "code": code,
                "redirect_uri": settings.callback_url(),
                "code_verifier": verifier,
            },
        )
        response.raise_for_status()
        payload = response.json()
    id_token = str(payload.get("id_token") or "")
    access_token = str(payload.get("access_token") or "")
    if not id_token or not access_token:
        raise ValueError("Authorization server did not return ID and access tokens")
    return {
        "id_token": id_token,
        "access_token": access_token,
        "expires_in": str(payload.get("expires_in") or "0"),
    }


def verified_login(id_token: str, access_token: str, nonce: str) -> tuple[str, str, frozenset[str], int]:
    identity = identity_claims(id_token, nonce)
    ledger, expires = ledger_principal(access_token)
    if identity["sub"] != ledger.subject:
        raise jwt.InvalidTokenError("ID token subject does not match access token")
    return identity["sub"], identity["email"], ledger.scopes, expires
