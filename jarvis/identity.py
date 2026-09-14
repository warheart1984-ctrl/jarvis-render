"""Identity contract shared with persistence-memory@app/identity.py.

Adapted from 97fab34b555c412f86138b66fcfc7f93a0fb7abd; see THIRD_PARTY_NOTICES.md.
The ledger namespace is issuer + immutable subject, never email or a client label.
"""

from __future__ import annotations

import contextvars
import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    issuer: str


_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("jarvis_principal", default=None)


def set_principal(principal: Principal):
    return _principal.set(principal)


def reset_principal(token) -> None:
    _principal.reset(token)


def current_principal() -> Principal | None:
    return _principal.get()


def tenant_key(principal: Principal) -> str:
    return hashlib.sha256(f"{principal.issuer}\x1f{principal.subject}".encode("utf-8")).hexdigest()


def current_tenant_key() -> str | None:
    principal = current_principal()
    return tenant_key(principal) if principal is not None else None


def normalized_subject(value: object) -> str | None:
    subject = str(value or "").strip()
    if not subject or len(subject) > 512 or not re.fullmatch(r"[^\x00-\x1f]+", subject):
        return None
    return subject
