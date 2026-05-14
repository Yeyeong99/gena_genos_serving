"""문서 번역 스트리밍 작업 상태/SSE 이벤트 저장소."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, AsyncIterator, Dict, List


_TRANSLATION_JOBS: Dict[str, Dict[str, Any]] = {}


# SSE/job-state 에서 제거할 대용량·미소비 키 목록.
#
# 이 세 필드는 슬라이드 수에 비례해 페이로드가 선형 증가해 SSE readline truncation·
# 네트워크·메모리 부담의 주범이지만 FE 는 더 이상 소비하지 않는다 (FE 는 *_html_url
# 만 사용하며 향후 블록 단위 편집은 SVG 인라인 HTML 의 ``<text>`` DOM 으로 처리).
# 발행 단의 단일 chokepoint 인 ``publish_translation_event`` 에서 strip 해 SSE 와
# 인메모리 job payload 양쪽에서 동시에 차단한다.
_HEAVY_PAYLOAD_KEYS = ("document_blocks", "pairs", "translation_pairs")


def _strip_heavy_payload_keys(payload: Dict[str, Any]) -> Dict[str, Any]:
    """``payload`` 에서 ``_HEAVY_PAYLOAD_KEYS`` 를 제거한 얕은 복사를 반환한다.

    원본 dict 를 in-place 수정하지 않는다 — 호출자가 같은 dict 를 다른 용도(예:
    파이프라인 내부 처리)로 계속 사용할 수 있으므로 SSE/job-state 진입 직전에만
    안전하게 분리해 차단한다.

    Heavy key 가 없으면 새 dict 를 만들지 않고 원본 ``payload`` 객체를 그대로
    반환한다 (no-op fast path) — 호출자는 항상 새 dict 를 받는다고 가정하지 말 것.
    """

    if not any(key in payload for key in _HEAVY_PAYLOAD_KEYS):
        return payload
    return {key: value for key, value in payload.items() if key not in _HEAVY_PAYLOAD_KEYS}


def create_translation_job(initial_payload: Dict[str, Any]) -> str:
    """새 번역 작업을 생성한다.

    파이프라인이 ``document_blocks`` / ``pairs`` / ``translation_pairs`` 를 포함한
    ``initial_payload`` 로 호출하더라도 인메모리 job payload 에는 heavy key 를
    누적하지 않도록 진입 시점에 strip 한다 (``publish_translation_event`` 와 동일
    원칙). 호출자가 받은 원본 dict 는 변경하지 않는다.
    """

    job_id = uuid.uuid4().hex
    created_at = time.time()
    sanitized_initial = _strip_heavy_payload_keys(initial_payload)
    _TRANSLATION_JOBS[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "payload": {
            **sanitized_initial,
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
    """작업 이벤트를 발행하고 payload를 함께 갱신한다.

    SSE 로 흘러가기 직전에 ``document_blocks`` / ``pairs`` / ``translation_pairs`` 는
    제거한다 — FE 미소비 + 슬라이드 수에 비례한 선형 증가로 SSE truncation 위험 유발.
    파이프라인 내부에서는 동일 dict 를 계속 사용할 수 있도록 in-place 가 아닌 얕은
    복사로 분리한다.
    """

    sanitized = _strip_heavy_payload_keys(data)
    update_translation_job(job_id, sanitized)
    if event == "completed":
        _TRANSLATION_JOBS[job_id]["status"] = "done"
    elif event == "job_error":
        _TRANSLATION_JOBS[job_id]["status"] = "error"
    elif event == "translation_started":
        _TRANSLATION_JOBS[job_id]["status"] = "translating"
    _append_event(job_id, event, sanitized)


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
