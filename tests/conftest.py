"""Tests use disposable storage and never inherit production provider credentials."""

import os
import tempfile

import pytest

os.environ["JARVIS_MEMORY_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="jarvis-tests-"), "bootstrap.sqlite3")

from jarvis.core.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "nvidia_api_key", "")
