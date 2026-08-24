"""Asynchronous author analysis worker.

Jobs read one frozen world revision and write only job state plus immutable
analysis artifacts.  No world or session mutation API is used.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from ..twp.validation.privacy import check_template, coverage_matrix
from ..twp.semantic_diff import semantic_world_diff
from ..twp.simulation import run_smoke_simulation


class AuthorJobWorker:
    def __init__(
        self,
        repository: Any,
        *,
        worker_id: str = "",
        poll_seconds: float = 1.0,
        lease_seconds: int = 180,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id or f"author-worker:{uuid.uuid4().hex}"
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.lease_seconds = max(10, int(lease_seconds))

    @staticmethod
    def _request(job: Mapping[str, Any]) -> dict[str, Any]:
        raw = job.get("request_json")
        if isinstance(raw, Mapping):
            return dict(raw)
        try:
            loaded = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            loaded = {}
        return dict(loaded) if isinstance(loaded, Mapping) else {}

    async def _world(self, job: Mapping[str, Any]) -> dict[str, Any]:
        world_id = str(job.get("world_id") or "")
        if not world_id:
            request = self._request(job)
            candidate = request.get("world")
            if isinstance(candidate, Mapping):
                return dict(candidate)
            raise ValueError("作者任务没有冻结世界来源")
        world = await self.repository.get_world(world_id)
        expected = int(job.get("world_revision") or 0)
        actual = int(world.get("revision") or 0)
        if expected != actual:
            raise RuntimeError(
                "世界修订已变化，旧任务已标记过期；请创建新的作者任务"
            )
        return world

    async def _cancel_requested(self, job_id: str) -> bool:
        state = await self.repository.author_job_worker_view(
            job_id,
            self.worker_id,
        )
        return str(state.get("status") or "") == "cancel_requested"

    async def _analyze(
        self,
        job: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        request = self._request(job)
        job_type = str(job.get("job_type") or "")
        world = await self._world(job)
        if job_type == "playtest":
            profiles = [
                dict(item)
                for item in request.get("profiles", [])
                if isinstance(item, Mapping)
            ]
            party_sizes = tuple(
                max(1, int(item.get("party_size") or 1))
                for item in profiles
            ) or (1, 4, 8)
            report = await asyncio.to_thread(
                run_smoke_simulation,
                world,
                turns=max(1, min(500, int(request.get("turns") or 30))),
                party_sizes=party_sizes,
            )
            artifact = {
                "schema": "tavern-playtest-report/1.0.0-rc10",
                "world": {
                    "slug": str(world.get("slug") or ""),
                    "revision": int(world.get("revision") or 0),
                    "content_version": str(
                        world.get("content_version") or ""
                    ),
                },
                "input": request,
                "result": report,
                "limitations": [
                    "确定性模拟不代表真实模型、AstrBot 宿主、消息平台或玩家行为。"
                ],
            }
            return (
                {
                    "ok": bool(report.get("ok")),
                    "errors": len(report.get("errors") or []),
                    "scenes_visited": len(
                        report.get("scenes_visited") or []
                    ),
                },
                [
                    {
                        "artifact_type": "playtest_report",
                        "schema_id": artifact["schema"],
                        "content": artifact,
                    }
                ],
            )
        if job_type == "semantic_diff":
            before = request.get("before")
            if not isinstance(before, Mapping):
                raise ValueError("语义差异任务缺少冻结 before 来源")
            report = semantic_world_diff(
                before,
                world,
                reviewed=bool(request.get("reviewed")),
            )
            return (
                {
                    "compatible": bool(report.get("compatible")),
                    **dict(report.get("summary") or {}),
                },
                [
                    {
                        "artifact_type": "semantic_diff",
                        "schema_id": str(report.get("schema") or ""),
                        "content": report,
                    }
                ],
            )
        if job_type == "full_preflight":
            report = await asyncio.to_thread(check_template, world)
            matrix = await asyncio.to_thread(coverage_matrix, world)
            content = {
                "schema": "tavern-full-preflight-report/1.0.0-rc10",
                "world": {
                    "slug": str(world.get("slug") or ""),
                    "revision": int(world.get("revision") or 0),
                    "content_version": str(
                        world.get("content_version") or ""
                    ),
                },
                "compatible": bool(report.get("compatible")),
                "errors": list(report.get("errors") or []),
                "warnings": list(report.get("warnings") or []),
                "suggestions": list(report.get("suggestions") or []),
                "summary": dict(report.get("summary") or {}),
                "limitations": [
                    "该预检基于冻结源码、静态契约和确定性算法，不代表真实宿主试玩。"
                ],
            }
            return (
                {
                    "compatible": content["compatible"],
                    "errors": len(content["errors"]),
                    "warnings": len(content["warnings"]),
                },
                [
                    {
                        "artifact_type": "preflight_report",
                        "schema_id": content["schema"],
                        "content": content,
                    },
                    {
                        "artifact_type": "coverage_matrix",
                        "schema_id": "tavern-profession-coverage/1.0.0-rc10",
                        "content": matrix,
                    },
                ],
            )
        raise ValueError(f"未知作者任务类型：{job_type}")

    async def run_once(self) -> int:
        jobs = await self.repository.claim_author_jobs(
            self.worker_id,
            limit=1,
            lease_seconds=self.lease_seconds,
        )
        for job in jobs:
            job_id = str(job.get("id") or "")
            try:
                started = await self.repository.start_author_job(
                    job_id,
                    self.worker_id,
                    progress_total=3,
                )
                if str(started.get("status") or "") == "cancelled":
                    continue
                await self.repository.checkpoint_author_job(
                    job_id,
                    self.worker_id,
                    progress_current=1,
                )
                if await self._cancel_requested(job_id):
                    await self.repository.finish_author_job(
                        job_id,
                        self.worker_id,
                        summary={"cancelled": True},
                        artifacts=[],
                    )
                    continue
                summary, artifacts = await self._analyze(job)
                await self.repository.checkpoint_author_job(
                    job_id,
                    self.worker_id,
                    progress_current=2,
                )
                await self.repository.finish_author_job(
                    job_id,
                    self.worker_id,
                    summary=summary,
                    artifacts=artifacts,
                )
            except Exception as exc:
                try:
                    await self.repository.fail_author_job(
                        job_id,
                        self.worker_id,
                        error_code="author.analysis_failed",
                        error_message=str(exc),
                    )
                except Exception:
                    # Lease loss/cancellation owns the final state.
                    pass
        return len(jobs)

    async def run(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            if not processed:
                await asyncio.sleep(self.poll_seconds)


__all__ = ["AuthorJobWorker"]
