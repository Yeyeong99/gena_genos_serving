"""문서 번역 스트리밍 작업 상태/SSE 이벤트 저장소."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, AsyncIterator, Dict, List


_TRANSLATION_JOBS: Dict[str, Dict[str, Any]] = {}


def create_translation_job(initial_payload: Dict[str, Any]) -> str:
    """새 번역 작업을 생성한다."""

    job_id = uuid.uuid4().hex
    created_at = time.time()
    _TRANSLATION_JOBS[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "payload": {
            **initial_payload,
            "job_id": job_id,
            "created_at": created_at,
        },
        "events": [],
        "subscribers": [],
        "created_at": created_at,
    }
    return job_id


def get_translation_job(job_id: str) -> Dict[str, Any] | None:
    """번역 작업 상태를 조회한다."""

    return _TRANSLATION_JOBS.get(job_id)


def update_translation_job(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """작업 payload를 병합 갱신한다."""

    job = _TRANSLATION_JOBS.setdefault(
        job_id,
        {"job_id": job_id, "status": "pending", "payload": {"job_id": job_id}, "events": [], "subscribers": []},
    )
    job["payload"] = {
        **job.get("payload", {}),
        **payload,
        "job_id": job_id,
    }
    return job["payload"]


def _append_event(job_id: str, event: str, data: Dict[str, Any]) -> None:
    job = _TRANSLATION_JOBS.setdefault(
        job_id,
        {"job_id": job_id, "status": "pending", "payload": {"job_id": job_id}, "events": [], "subscribers": []},
    )
    event_id = len(job["events"]) + 1
    item = {
        "id": event_id,
        "event": event,
        "data": {
            **data,
            "job_id": job_id,
        },
    }
    job["events"].append(item)
    for queue in list(job.get("subscribers", [])):
        queue.put_nowait(item)


def publish_translation_event(job_id: str, event: str, data: Dict[str, Any]) -> None:
    """작업 이벤트를 발행하고 payload를 함께 갱신한다."""

    update_translation_job(job_id, data)
    if event == "completed":
        _TRANSLATION_JOBS[job_id]["status"] = "done"
    elif event == "job_error":
        _TRANSLATION_JOBS[job_id]["status"] = "error"
    elif event == "translation_started":
        _TRANSLATION_JOBS[job_id]["status"] = "translating"
    _append_event(job_id, event, data)


def complete_translation_job(job_id: str, payload: Dict[str, Any]) -> None:
    """번역 작업을 완료 상태로 갱신한다."""

    job = _TRANSLATION_JOBS.get(job_id)
    completed_at = time.time()
    created_at = float(job.get("created_at", completed_at)) if job else completed_at
    enriched_payload = {
        **payload,
        "created_at": created_at,
        "completed_at": completed_at,
        "elapsed_ms": int(max(0.0, completed_at - created_at) * 1000),
    }
    publish_translation_event(job_id, "completed", enriched_payload)
    _TRANSLATION_JOBS[job_id]["completed_at"] = completed_at


def fail_translation_job(job_id: str, message: str, payload: Dict[str, Any] | None = None) -> None:
    """번역 작업을 실패 상태로 갱신한다."""

    job = _TRANSLATION_JOBS.get(job_id)
    completed_at = time.time()
    created_at = float(job.get("created_at", completed_at)) if job else completed_at
    merged = {
        "translation_error": message,
        "created_at": created_at,
        "completed_at": completed_at,
        "elapsed_ms": int(max(0.0, completed_at - created_at) * 1000),
        **(payload or {}),
    }
    publish_translation_event(job_id, "job_error", merged)
    _TRANSLATION_JOBS[job_id]["completed_at"] = completed_at


async def stream_translation_job(job_id: str, last_event_id: int = 0) -> AsyncIterator[Dict[str, Any]]:
    """SSE endpoint용 이벤트 스트림을 제공한다."""

    job = _TRANSLATION_JOBS.get(job_id)
    if not job:
        raise KeyError(job_id)

    for item in job.get("events", []):
        if int(item.get("id", 0)) > last_event_id:
            yield item

    if job.get("status") in {"done", "error"}:
        return

    queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    job.setdefault("subscribers", []).append(queue)
    try:
        while True:
            item = await queue.get()
            yield item
            if item.get("event") in {"completed", "job_error"}:
                break
    finally:
        subscribers: List[asyncio.Queue[Dict[str, Any]]] = job.get("subscribers", [])
        if queue in subscribers:
            subscribers.remove(queue)
