from __future__ import annotations

import json

import httpx
import pytest

from jarvis.spiral_client.infinity import ProjectInfinityClient


@pytest.mark.asyncio
async def test_infinity_evolve_uses_documented_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "job_id": "j1", "task": "x", "result": {}})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    client = ProjectInfinityClient("http://infinity")
    result = await client.evolve(job_id="j1", jarvis_run_id="r1", task="x", initial_candidate="draft")
    assert result["ok"] is True
    assert captured["job_id"] == "j1"
    assert captured["jarvis_run_id"] == "r1"
    assert captured["evaluation"]["mode"] == "forge_eval"
    assert captured["constraints"]["max_evaluations"] == 12


@pytest.mark.asyncio
async def test_infinity_rejected_response_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": {"message": "law blocked"}})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    with pytest.raises(RuntimeError, match="law blocked"):
        await ProjectInfinityClient("http://infinity").evolve(
            job_id="j1", jarvis_run_id="r1", task="x", initial_candidate="draft"
        )
