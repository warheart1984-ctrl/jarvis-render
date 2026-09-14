"""Durable product lock on hypothesized / tool / inferred-summary memory writes.

This is a product lock, not a security audit and not OTEM. There is no demo
override and no expiring admin timer. Production cannot reopen governed writes
with ``JARVIS_GOVERNED_WRITES_ENABLED=true``.
"""

from __future__ import annotations

import hashlib

WRITE_PATH_LOCK_TUPLE = (
    "jarvis-render:write-path-lock:v1|"
    "hypothesized+tool_external+inferred_summary|"
    "memory_admission=blocked|"
    "production_env_cannot_enable_governed_writes"
)
WRITE_PATH_LOCK_SHA256 = hashlib.sha256(WRITE_PATH_LOCK_TUPLE.encode("utf-8")).hexdigest()
