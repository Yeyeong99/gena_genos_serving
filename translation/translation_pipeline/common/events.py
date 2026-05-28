"""파이프라인 공통 이벤트 유틸."""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

from typing import Any, Dict

import aiohttp


SSE_EVENT: Dict[str, Dict[str, Any]] = {
    "EXTRACT_START": {"step": 1, "total": 4, "status": "start", "message": "텍스트 추출 중"},
    "EXTRACT_DONE": {"step": 1, "total": 4, "status": "done", "message": "텍스트 추출 완료"},
    "TRANSLATE_START": {"step": 2, "total": 4, "status": "start", "message": "번역 중"},
    "TRANSLATE_DONE": {"step": 2, "total": 4, "status": "done", "message": "번역 완료"},
    "INJECT_START": {"step": 3, "total": 4, "status": "start", "message": "번역 결과 적용 중"},
    "INJECT_DONE": {"step": 3, "total": 4, "status": "done", "message": "번역 결과 적용 완료"},
    "SAVE_START": {"step": 4, "total": 4, "status": "start", "message": "문서 저장 중"},
    "SAVE_DONE": {"step": 4, "total": 4, "status": "done", "message": "완료"},
    "ERROR": {"step": 0, "total": 4, "status": "error", "message": "오류 발생"},
}


async def emit_event(event_key: str, callback_url: str = "", **kwargs: Any) -> None:
    """SSE 이벤트를 출력하고 필요 시 callback URL로 전송한다.

    Args:
        event_key: 발행할 이벤트 키.
        callback_url: 이벤트를 전송할 callback URL.
        **kwargs: 이벤트 payload에 추가할 필드.

    Returns:
        없음.
    """

    data = SSE_EVENT[event_key].copy()
    data.update(kwargs)
    payload = {"event": event_key, "data": data}
    log_info(f"[SSE] {payload}")

    if callback_url:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    callback_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception as exc:
            log_info(f"[SSE 전송 실패] {event_key}: {exc}")
