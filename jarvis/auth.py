"""Server-owned visitor identities, revocable sessions and resource authorization.

A managed IdP proves identity via Google social login. SQLite owns admission and
conversation authority. Neither an email nor a client-supplied user/session label
grants access to data.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException, Request

from jarvis.core.config import settings

COOKIE = "__Host-jarvis-session"
FLOW_COOKIE = "__Host-jarvis-login"
VISITOR_PREFIXES = ("google-", "oidc-")


def cookie_name(*, flow: bool = False) -> str:
    name = FLOW_COOKIE if flow else COOKIE
    return name if settings.public_origin.startswith("https://") else name.removeprefix("__Host-")


def cookie_args(*, flow: bool = False, max_age: int) -> dict[str, object]:
    return {
        "key": cookie_name(flow=flow),
        "max_age": max_age,
        "httponly": True,
        "secure": settings.public_origin.startswith("https://"),
        "samesite": "lax",
        "path": "/",
    }


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"jarvis-csrf-v1", hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    subject: str
    email: str
    session_hash: str
    scopes: frozenset[str] = frozenset()
    issuer: str = ""
    ledger_token: str = ""
    role: str = "user"

    def recall_key(self) -> str:
        return hmac.new(
            settings.recall_signing_key.encode(), ("recall:" + self.tenant_id).encode(), hashlib.sha256
        ).hexdigest()


class AccessStore:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS auth_tenants(tenant_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS auth_users(
                    user_id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
                    email TEXT NOT NULL, tenant_id TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'user',
                    disabled INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(issuer,subject));
                CREATE TABLE IF NOT EXISTS auth_sessions(
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS auth_session_user ON auth_sessions(user_id);
                CREATE TABLE IF NOT EXISTS auth_flows(
                    state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
                    nonce TEXT NOT NULL, verifier TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_conversations(
                    session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    owner_sub TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS auth_conversation_user ON auth_conversations(user_id,created DESC);
                CREATE TABLE IF NOT EXISTS auth_quota(
                    scope TEXT NOT NULL, bucket TEXT NOT NULL, window INTEGER NOT NULL,
                    used INTEGER NOT NULL, PRIMARY KEY(scope,bucket,window));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(auth_sessions)")}
            if "access_token" not in columns:
                db.execute("ALTER TABLE auth_sessions ADD COLUMN access_token TEXT NOT NULL DEFAULT ''")
            if "access_expires" not in columns:
                db.execute("ALTER TABLE auth_sessions ADD COLUMN access_expires REAL NOT NULL DEFAULT 0")
            if "scopes" not in columns:
                db.execute("ALTER TABLE auth_sessions ADD COLUMN scopes TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start_flow(self) -> tuple[str, str, str, str]:
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_flows WHERE expires<=?", (now,))
            if db.execute("SELECT COUNT(*) FROM auth_flows").fetchone()[0] >= 1000:
                raise HTTPException(429, "Sign-in is busy. Try again shortly.")
            db.execute(
                "INSERT INTO auth_flows VALUES (?,?,?,?,?)",
                (digest(state), digest(browser), nonce, verifier, now + 300),
            )
        return state, browser, nonce, verifier

    def consume_flow(self, state: str, browser: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM auth_flows WHERE state_hash=?", (digest(state),)).fetchone()
            if not row or row["expires"] <= time.time() or not secrets.compare_digest(
                row["browser_hash"], digest(browser)
            ):
                raise ValueError("Invalid or expired login")
            db.execute("DELETE FROM auth_flows WHERE state_hash=?", (digest(state),))
            return dict(row)

    def login(
        self,
        subject: str,
        email: str,
        old_token: str = "",
        *,
        scopes: frozenset[str] | None = None,
        access_token: str = "",
        access_expires: float = 0,
        issuer: str = "",
    ) -> tuple[Principal, str]:
        """Only call after cryptographic IdP verification. No public enrollment API."""
        email = email.lower()
        issuer = (issuer or settings.identity_issuer()).strip()
        granted = scopes or frozenset()
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM auth_users WHERE issuer=? AND subject=?", (issuer, subject)).fetchone()
            if row:
                if row["disabled"]:
                    raise ValueError("Account unavailable")
                user_id = row["user_id"]
                tenant_id = row["tenant_id"]
                db.execute("UPDATE auth_users SET email=? WHERE user_id=?", (email, user_id))
            else:
                # Email changes do not move identity; different subjects never merge.
                user_id = "oidc-" + uuid4().hex
                tenant_id = "t_" + uuid4().hex
                db.execute("INSERT INTO auth_tenants VALUES (?)", (tenant_id,))
                db.execute(
                    "INSERT INTO auth_users(user_id,issuer,subject,email,tenant_id) VALUES (?,?,?,?,?)",
                    (user_id, issuer, subject, email, tenant_id),
                )
            db.execute("DELETE FROM auth_sessions WHERE expires<=? OR token_hash=?", (now, digest(old_token)))
            db.execute(
                "DELETE FROM auth_sessions WHERE token_hash IN (SELECT token_hash FROM auth_sessions "
                "WHERE user_id=? ORDER BY expires DESC LIMIT -1 OFFSET 9)",
                (user_id,),
            )
            db.execute(
                "INSERT INTO auth_sessions(token_hash,user_id,expires,access_token,access_expires,scopes) "
                "VALUES (?,?,?,?,?,?)",
                (
                    digest(token),
                    user_id,
                    now + settings.visitor_session_hours * 3600,
                    access_token,
                    access_expires,
                    " ".join(sorted(granted)),
                ),
            )
        return (
            Principal(user_id, tenant_id, subject, email, digest(token), granted, issuer, access_token),
            token,
        )

    def authenticate(self, token: str) -> Principal | None:
        if not token or len(token) > 128 or not settings.oauth_enabled():
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT u.user_id,u.tenant_id,u.subject,u.email,u.disabled,u.issuer,u.role,"
                "s.token_hash,s.access_token,s.access_expires,s.scopes FROM auth_sessions s "
                "JOIN auth_users u ON u.user_id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                (digest(token), time.time()),
            ).fetchone()
        if not row or row["disabled"]:
            return None
        return Principal(
            row["user_id"],
            row["tenant_id"],
            row["subject"],
            row["email"],
            row["token_hash"],
            frozenset(row["scopes"].split()) if row["scopes"] else frozenset(),
            row["issuer"],
            row["access_token"] if row["access_expires"] > time.time() else "",
            row["role"],
        )

    def revoke(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(token),))

    def revoke_user(self, user_id: str, *, disable: bool = False) -> None:
        with self.connect() as db:
            if disable:
                db.execute("UPDATE auth_users SET disabled=1 WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))

    def new_conversation(self, principal: Principal) -> str:
        session_id = "jarvis-session-" + uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO auth_conversations VALUES (?,?,?,?,?)",
                (session_id, principal.user_id, principal.tenant_id, principal.subject, time.time()),
            )
        return session_id

    def owner(self, session_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT user_id FROM auth_conversations WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row else None

    def owns(self, principal: Principal, session_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM auth_conversations WHERE session_id=? AND tenant_id=? "
                    "AND user_id=? AND owner_sub=?",
                    (session_id, principal.tenant_id, principal.user_id, principal.subject),
                ).fetchone()
                is not None
            )

    def conversations(self, principal: Principal) -> list[dict]:
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT session_id,created FROM auth_conversations WHERE tenant_id=? AND user_id=? AND owner_sub=? "
                    "ORDER BY created DESC LIMIT 100",
                    (principal.tenant_id, principal.user_id, principal.subject),
                )
            ]

    def consume_quota(self, scope: str, bucket: str, limit: int, period: int) -> bool:
        window = int(time.time()) // period
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_quota WHERE bucket=? AND window<?", (bucket, window - 1))
            row = db.execute(
                "SELECT used FROM auth_quota WHERE scope=? AND bucket=? AND window=?",
                (scope, bucket, window),
            ).fetchone()
            if row and row[0] >= limit:
                return False
            db.execute(
                "INSERT INTO auth_quota VALUES (?,?,?,1) ON CONFLICT(scope,bucket,window) "
                "DO UPDATE SET used=used+1",
                (scope, bucket, window),
            )
        return True


def visitor(request: Request) -> Principal | None:
    if getattr(request.state, "operator", False):
        return None
    return getattr(request.state, "principal", None)


def bind_user(request: Request, claimed_user: str) -> str:
    principal = visitor(request)
    if principal:
        if claimed_user and claimed_user != principal.user_id:
            raise HTTPException(403, "User ID does not match the signed-in account")
        return principal.user_id
    if not claimed_user or claimed_user.startswith(VISITOR_PREFIXES):
        raise HTTPException(403, "A valid operator user ID is required")
    return claimed_user


def require_session(request: Request, access: AccessStore, session_id: str) -> None:
    owner = access.owner(session_id)
    principal = visitor(request)
    if (principal and not access.owns(principal, session_id)) or (not principal and owner is not None):
        raise HTTPException(404, "Session not found")


def require_operator(request: Request) -> None:
    if visitor(request) and not getattr(request.state, "operator", False):
        raise HTTPException(403, "Operator access required; visitor governed writes are disabled")


def require_csrf(request: Request, token: str) -> None:
    if request.headers.get("origin") != settings.public_origin or not secrets.compare_digest(
        request.headers.get("X-Jarvis-CSRF", ""), csrf_token(token)
    ):
        raise HTTPException(403, "Same-origin request and CSRF token required")


def guard_visitor_mutation(request: Request) -> Principal | None:
    principal = visitor(request)
    if principal and not getattr(request.state, "operator", False):
        raw = request.cookies.get(cookie_name()) or ""
        require_csrf(request, raw)
    return principal


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Locally revoke Jarvis visitor access (no tokens displayed)")
    parser.add_argument("user_id")
    parser.add_argument("--disable", action="store_true", help="Also block future logins for this identity")
    args = parser.parse_args()
    AccessStore(settings.memory_db_path).revoke_user(args.user_id, disable=args.disable)
    print("Visitor sessions revoked; account disabled." if args.disable else "Visitor sessions revoked.")


if __name__ == "__main__":
    main()
