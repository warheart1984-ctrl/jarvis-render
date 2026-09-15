"""Tests for observe-only nx_search backend."""

from __future__ import annotations

from jarvis.brain.tools.envelope import ToolCallStatus, ToolName
from jarvis.brain.tools.nx_search import FakeNxSearchBackend, run_nx_search
from jarvis.core.config import settings


def test_nx_search_fake_backend_returns_accepted():
    backend = FakeNxSearchBackend(
        hits=[
            {"path": "/docs/a.md", "excerpt": "first hit"},
            {"path": "/docs/b.md", "excerpt": "second hit"},
        ]
    )
    rec = run_nx_search(
        query="test",
        backend=backend,  # type: ignore[arg-type]
        transaction_id="tx-1",
        correlation_id="cor-1",
    )
    assert rec.tool_name == ToolName.NX_SEARCH.value
    assert rec.status == ToolCallStatus.ACCEPTED
    assert rec.observe_only is True
    assert rec.memory_written is False
    assert rec.memory_eligible is False
    assert rec.authority is False
    assert len(rec.sources) == 2
    assert rec.citations == [s.source_id for s in rec.sources]


def test_nx_search_no_config_unavailable():
    # Temporarily clear bin path
    original = settings.nx_search_bin
    try:
        settings.nx_search_bin = ""
        rec = run_nx_search(
            query="test",
            transaction_id="tx-2",
            correlation_id="cor-2",
        )
        assert rec.status == ToolCallStatus.UNAVAILABLE
        assert rec.error is not None
        assert rec.observe_only is True
    finally:
        settings.nx_search_bin = original


def test_nx_search_empty_backend_degraded():
    backend = FakeNxSearchBackend(hits=[])
    rec = run_nx_search(
        query="nothing",
        backend=backend,  # type: ignore[arg-type]
        transaction_id="tx-3",
        correlation_id="cor-3",
    )
    assert rec.status == ToolCallStatus.DEGRADED
    assert rec.sources == []


def test_nx_search_cli_missing_binary():
    # Force a backend that raises RuntimeError
    class BadBackend:
        def run(self, query: str, *, timeout_seconds: float | None = None):
            raise RuntimeError("binary not found")

    rec = run_nx_search(
        query="x",
        backend=BadBackend(),  # type: ignore[arg-type]
        transaction_id="tx-4",
        correlation_id="cor-4",
    )
    assert rec.status == ToolCallStatus.UNAVAILABLE
    assert "binary not found" in (rec.error or "")


def test_nx_search_never_admits_memory():
    backend = FakeNxSearchBackend(hits=[{"path": "/a", "excerpt": "x"}])
    rec = run_nx_search(
        query="q",
        backend=backend,  # type: ignore[arg-type]
        transaction_id="tx-5",
        correlation_id="cor-5",
    )
    # Validate field validators enforce observe-only / no memory
    assert rec.memory_written is False
    assert rec.memory_eligible is False
    assert rec.authority is False
