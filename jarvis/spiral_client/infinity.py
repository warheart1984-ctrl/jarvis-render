"""Client for Project Infinity's bounded EvolveEngine contract."""

from __future__ import annotations

from typing import Any

import httpx


class ProjectInfinityClient:
    """Thin client for the documented EvolveEngine HTTP boundary."""

    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/health", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def evolve(
        self,
        *,
        job_id: str,
        jarvis_run_id: str,
        task: str,
        initial_candidate: str,
        target_score: float = 0.95,
        max_generations: int = 3,
        max_evaluations: int = 12,
        max_wall_time_seconds: float = 30.0,
    ) -> dict[str, Any]:
        payload = {
            "job_id": job_id,
            "jarvis_run_id": jarvis_run_id,
            "task": task,
            "config": {"initial_candidate": initial_candidate, "strategy": "local_search"},
            "evaluation": {
                "mode": "forge_eval",
                "forge_eval_mode": "llm_rubric",
                "candidate_field": "program",
                "payload": {},
                "success_threshold": target_score,
            },
            "constraints": {
                "population_size": 4,
                "max_generations": max_generations,
                "max_evaluations": max_evaluations,
                "max_wall_time_seconds": max_wall_time_seconds,
                "target_score": target_score,
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/evolve", json=payload, headers=self.headers)
            response.raise_for_status()
            data = response.json()
        if data.get("ok") is not True:
            raise RuntimeError(data.get("error", {}).get("message", "Project Infinity rejected evolution request"))
        return data
