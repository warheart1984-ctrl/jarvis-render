"""Managed-IdP PKCE login, session cookies, and ledger access-token checks."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient

from jarvis.auth import cookie_args, cookie_name, csrf_token
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.core.config import settings
from jarvis.oauth import authorization_url, decode_jwt, verified_login
from jarvis.persistence import JarvisStore

KEY = "test-service-token"
SCOPES = frozenset({"memory.read", "memory.write"})


def enable_oauth(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_mode", "oauth")
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.example.com/")
    monkeypatch.setattr(settings, "oidc_audience", "https://ledger.example.com")
    monkeypatch.setattr(settings, "oidc_jwks_url", "https://auth.example.com/.well-known/jwks.json")
    monkeypatch.setattr(settings, "oidc_client_id", "web-client")
    monkeypatch.setattr(settings, "oidc_client_secret", "client-secret")
    monkeypatch.setattr(settings, "oidc_connection", "google-oauth2")
    monkeypatch.setattr(settings, "public_origin", "http://testserver")
    monkeypatch.setattr(settings, "recall_signing_key", "tenant-master-key")
    monkeypatch.setattr(settings, "service_token", KEY)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")


@pytest.fixture
def oauth_client(monkeypatch, tmp_path):
    enable_oauth(monkeypatch)
    import jarvis.main as main
    import jarvis.routes.auth as auth
    import jarvis.routes.chat as chat
    import jarvis.routes.voice as voice

    engine = JarvisEngine(store=JarvisStore(tmp_path / "oauth.sqlite3"))
    for mod in (main, chat, voice, auth):
        monkeypatch.setattr(mod, "engine", engine)
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult("Hello from tenant.", "test", "text-model", 1)),
    )
    main._rate.clear()
    with TestClient(main.app) as client:
        yield client, engine


def visitor_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-Jarvis-CSRF": csrf_token(token)}


def sign_in(engine: JarvisEngine, subject: str, email: str) -> tuple[str, str]:
    principal, token = engine.access.login(
        subject,
        email,
        scopes=SCOPES,
        access_token=f"ledger-token-{subject}",
        access_expires=time.time() + 3600,
        issuer="https://auth.example.com",
    )
    return principal.user_id, token


def test_authorization_url_uses_google_connection_and_ledger_audience(monkeypatch):
    enable_oauth(monkeypatch)
    url = authorization_url("state", "nonce", "verifier")
    parts = urlparse(url)
    query = parse_qs(parts.query)
    assert parts.path.endswith("/authorize")
    assert query["connection"] == ["google-oauth2"]
    assert query["audience"] == ["https://ledger.example.com"]
    assert query["code_challenge_method"] == ["S256"]
    assert "client-secret" not in url
    assert "memory.read" in query["scope"][0]
    assert "memory.write" in query["scope"][0]


def test_verified_login_requires_ledger_audience_scopes_and_matching_subject(monkeypatch):
    enable_oauth(monkeypatch)

    def decode(token: str, *, audience: str, nonce: str | None = None) -> dict:
        if token == "id":
            assert audience == "web-client"
            assert nonce == "n"
            return {"sub": "google-oauth2|alice", "email": "Alice@Gmail.com", "nonce": "n"}
        assert token == "access"
        assert audience == "https://ledger.example.com"
        return {
            "sub": "google-oauth2|alice",
            "scope": "memory.read memory.write",
            "exp": int(time.time()) + 60,
        }

    monkeypatch.setattr("jarvis.oauth.decode_jwt", decode)
    subject, email, scopes, expires = verified_login("id", "access", "n")
    assert subject == "google-oauth2|alice"
    assert email == "alice@gmail.com"
    assert scopes == SCOPES
    assert expires > time.time()

    def decode_permissions(token: str, *, audience: str, nonce: str | None = None) -> dict:
        if token == "id":
            return {"sub": "google-oauth2|alice", "email": "alice@gmail.com", "nonce": "n"}
        return {
            "sub": "google-oauth2|alice",
            "permissions": ["memory.read", "memory.write"],
            "exp": int(time.time()) + 60,
        }

    monkeypatch.setattr("jarvis.oauth.decode_jwt", decode_permissions)
    _, _, permission_scopes, _ = verified_login("id", "access", "n")
    assert permission_scopes == SCOPES

    def decode_read_only(token: str, *, audience: str, nonce: str | None = None) -> dict:
        if token == "id":
            return {"sub": "google-oauth2|alice", "email": "alice@gmail.com", "nonce": "n"}
        return {"sub": "google-oauth2|alice", "scope": "memory.read", "exp": int(time.time()) + 60}

    monkeypatch.setattr("jarvis.oauth.decode_jwt", decode_read_only)
    with pytest.raises(jwt.InvalidTokenError, match="memory.read and memory.write"):
        verified_login("id", "access", "n")

    def decode_mismatch(token: str, *, audience: str, nonce: str | None = None) -> dict:
        if token == "id":
            return {"sub": "google-oauth2|alice", "email": "alice@gmail.com", "nonce": "n"}
        return {
            "sub": "google-oauth2|bob",
            "scope": "memory.read memory.write",
            "exp": int(time.time()) + 60,
        }

    monkeypatch.setattr("jarvis.oauth.decode_jwt", decode_mismatch)
    with pytest.raises(jwt.InvalidTokenError, match="does not match"):
        verified_login("id", "access", "n")


def start_login(client):
    started = client.get("/auth/login", follow_redirects=False)
    assert started.status_code == 303
    hop = started.headers["location"]
    assert hop.startswith("/auth/continue?")
    continued = client.get(hop, follow_redirects=False)
    assert continued.status_code == 302
    location = continued.headers["location"]
    assert "client-secret" not in location
    return location


def test_identity_issuers_accept_auth0_trailing_slash(monkeypatch):
    enable_oauth(monkeypatch)
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.example.com")
    assert settings.identity_issuer() == "https://auth.example.com"
    assert settings.identity_issuers() == ("https://auth.example.com", "https://auth.example.com/")
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.example.com/")
    assert settings.identity_issuer() == "https://auth.example.com"
    assert "https://auth.example.com/" in settings.identity_issuers()


def test_decode_jwt_allows_issuer_with_or_without_trailing_slash(monkeypatch):
    enable_oauth(monkeypatch)
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.example.com")
    captured: dict[str, object] = {}

    class _Key:
        key = "public-key"

    class _Client:
        def get_signing_key_from_jwt(self, token: str) -> _Key:
            return _Key()

    def fake_decode(token, key, algorithms, audience, issuer, leeway, options):
        captured["issuer"] = issuer
        return {"sub": "google-oauth2|alice", "exp": int(time.time()) + 60, "iat": int(time.time())}

    monkeypatch.setattr("jarvis.oauth._jwk_client", lambda url: _Client())
    monkeypatch.setattr("jarvis.oauth.jwt.decode", fake_decode)
    decode_jwt("token", audience="web-client")
    assert captured["issuer"] == ("https://auth.example.com", "https://auth.example.com/")


def test_https_flow_cookie_is_host_prefixed(monkeypatch):
    enable_oauth(monkeypatch)
    monkeypatch.setattr(settings, "public_origin", "https://jarvis-avfy.onrender.com")
    assert cookie_name(flow=True) == "__Host-jarvis-login"
    args = cookie_args(flow=True, max_age=300)
    assert args["secure"] is True
    assert args["samesite"] == "lax"
    assert args["path"] == "/"
    assert "domain" not in args


def test_login_sets_flow_cookie_on_same_origin_hop(oauth_client):
    client, _engine = oauth_client
    started = client.get("/auth/login", follow_redirects=False)
    assert started.status_code == 303
    assert cookie_name(flow=True) in started.headers.get("set-cookie", "")


def test_pkce_callback_sets_session_cookie_not_tokens(oauth_client, monkeypatch):
    client, engine = oauth_client
    monkeypatch.setattr(
        "jarvis.routes.auth.exchange_code",
        AsyncMock(return_value={"id_token": "id", "access_token": "access", "expires_in": "3600"}),
    )
    monkeypatch.setattr(
        "jarvis.routes.auth.verified_login",
        lambda *args, **kwargs: ("google-oauth2|alice", "alice@gmail.com", SCOPES, int(time.time()) + 3600),
    )
    location = start_login(client)
    state = parse_qs(urlparse(location).query)["state"][0]
    finished = client.get(f"/auth/callback?code=auth-code&state={state}", follow_redirects=False)
    assert finished.status_code == 303
    assert finished.headers["location"] == "/ui/?login=ok"
    cookie = client.cookies.get(cookie_name())
    assert cookie
    assert cookie != "access"
    me = client.get("/auth/session").json()
    assert me["authenticated"] is True
    assert me["user_id"].startswith("oidc-")
    assert "memory.read" in me["scopes"]
    assert me["csrf"]
    assert "access" not in str(me)


def test_callback_without_flow_cookie_fails(oauth_client, monkeypatch):
    client, _engine = oauth_client
    location = start_login(client)
    state = parse_qs(urlparse(location).query)["state"][0]
    client.cookies.clear()
    finished = client.get(f"/auth/callback?code=auth-code&state={state}", follow_redirects=False)
    assert finished.status_code == 303
    assert finished.headers["location"] == "/ui/?login=failed"


def test_callback_idp_error_fails_without_token_exchange(oauth_client, monkeypatch):
    client, _engine = oauth_client
    exchange = AsyncMock()
    monkeypatch.setattr("jarvis.routes.auth.exchange_code", exchange)
    finished = client.get("/auth/callback?error=access_denied&state=abc", follow_redirects=False)
    assert finished.status_code == 303
    assert finished.headers["location"] == "/ui/?login=failed"
    exchange.assert_not_called()


def test_continue_rejects_open_redirect(oauth_client):
    client, _engine = oauth_client
    evil = client.get(
        "/auth/continue?to=https://evil.example/authorize?client_id=web-client",
        follow_redirects=False,
    )
    assert evil.status_code == 303
    assert evil.headers["location"] == "/ui/?login=failed"


def test_oauth_visitors_are_isolated_and_operator_token_still_works(oauth_client):
    client, engine = oauth_client
    alice_id, alice_token = sign_in(engine, "google-oauth2|alice", "alice@gmail.com")
    bob_id, bob_token = sign_in(engine, "google-oauth2|bob", "bob@gmail.com")
    client.cookies.set(cookie_name(), alice_token)
    first = client.post(
        "/chat",
        headers=visitor_headers(alice_token),
        json={"user_id": alice_id, "message": "ALICE_PRIVATE_FACT"},
    )
    assert first.status_code == 200, first.text
    alice_session = first.json()["session_id"]
    client.cookies.set(cookie_name(), bob_token)
    stolen = client.get(f"/sessions/{alice_session}/audit/verify", headers=visitor_headers(bob_token))
    assert stolen.status_code == 404
    replay = client.post(
        "/chat",
        headers=visitor_headers(bob_token),
        json={"user_id": alice_id, "session_id": alice_session, "message": "steal"},
    )
    assert replay.status_code in {403, 404}
    client.cookies.clear()
    operator = client.post(
        "/chat",
        headers={"X-Jarvis-Service-Token": KEY},
        json={"user_id": "owner", "message": "operator break-glass"},
    )
    assert operator.status_code == 200, operator.text
    assert operator.json()["session_id"] != alice_session
    headers = {"X-Jarvis-Service-Token": KEY}
    assert client.get(f"/state/{alice_session}", headers=headers).status_code == 404
    assert client.get(f"/sessions/{alice_session}/audit", headers=headers).status_code == 404
    assert client.get(f"/sessions/{alice_session}/trace", headers=headers).status_code == 404
    hijack = client.post(
        "/chat",
        headers=headers,
        json={"user_id": "owner", "session_id": alice_session, "message": "operator hijack"},
    )
    assert hijack.status_code == 404
    resume = client.post(
        "/sessions/resume",
        headers=headers,
        json={"user_id": "owner", "session_id": alice_session},
    )
    assert resume.status_code == 404
    inspect = client.get(
        f"/sessions/{alice_session}/memory-inspection?user_id=owner",
        headers=headers,
    )
    assert inspect.status_code == 404


def test_login_disabled_in_operator_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "service_token", KEY)
    monkeypatch.setattr(settings, "environment", "production")
    import jarvis.main as main
    import jarvis.routes.chat as chat
    import jarvis.routes.voice as voice

    engine = JarvisEngine(store=JarvisStore(tmp_path / "op.sqlite3"))
    for mod in (main, chat, voice):
        monkeypatch.setattr(mod, "engine", engine)
    main._rate.clear()
    with TestClient(main.app) as client:
        assert client.get("/auth/login").status_code == 404
        assert client.get("/auth/session").json()["auth_mode"] == "operator"
        assert client.get("/capabilities").status_code == 401
