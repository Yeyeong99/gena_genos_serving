"""번역 preview 후속 생성 작업 상태 저장소."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict


_PREVIEW_JOBS: Dict[str, Dict[str, Any]] = {}


def create_preview_job() -> str:
    """새 preview 작업을 생성한다."""

    job_id = uuid.uuid4().hex
    _PREVIEW_JOBS[job_id] = {
        "job_id": job_id,
        "translated_preview_job_id": job_id,
        "status": "pending",
        "translated_preview_status": "pending",
        "created_at": time.time(),
    }
    return job_id


def complete_preview_job(job_id: str, payload: Dict[str, Any]) -> None:
    """preview 작업을 완료 상태로 갱신한다."""

    _PREVIEW_JOBS[job_id] = {
        **_PREVIEW_JOBS.get(job_id, {"job_id": job_id}),
        "status": "done",
        "translated_preview_status": "done",
        **payload,
        "completed_at": time.time(),
    }


def fail_preview_job(job_id: str, message: str) -> None:
    """preview 작업을 실패 상태로 갱신한다."""

    _PREVIEW_JOBS[job_id] = {
        **_PREVIEW_JOBS.get(job_id, {"job_id": job_id}),
        "status": "error",
        "translated_preview_status": "error",
        "message": message,
        "completed_at": time.time(),
    }


def get_preview_job(job_id: str) -> Dict[str, Any] | None:
    """preview 작업 상태를 조회한다."""

    return _PREVIEW_JOBS.get(job_id)
