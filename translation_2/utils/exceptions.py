"""
GenOS translation exception definitions and error code constants.
"""

from __future__ import annotations

from typing import Any


class GenATranslationException(Exception):
    """Structured exception carrying an error code for SSE propagation."""

    def __init__(self, error_code: str, msg: str, detail: str | None = None) -> None:
        super().__init__(msg)
        self.error_code = error_code
        self.msg = msg
        self.detail = detail

    def __repr__(self) -> str:
        return f"GenATranslationException(error_code={self.error_code!r}, msg={self.msg!r})"


# ---------------------------------------------------------------------------
# Error code constants
# ---------------------------------------------------------------------------

# 입력·요청 검증 (08000xxx)
ERR_TRANSLATION_INPUT_VALIDATION = "08000001"       # 요청 payload 형식 오류
ERR_TRANSLATION_UNSUPPORTED_FORMAT = "08000002"    # 지원하지 않는 문서/번역 형식
ERR_TRANSLATION_JOB_NOT_FOUND = "08000003"         # 재시도/수정 대상 job 없음

# LLM 호출 (08001xxx)
ERR_TRANSLATION_LLM_CONTEXT_EXCEEDED = "08001001"  # 컨텍스트 길이 초과
ERR_TRANSLATION_LLM_BAD_REQUEST = "08001002"       # BadRequestError (컨텍스트 초과 외)
ERR_TRANSLATION_LLM_API_STATUS = "08001003"        # APIStatusError (4xx/5xx)
ERR_TRANSLATION_LLM_GENERAL = "08001004"           # LLM 일반 오류

# 파일·프리뷰·스토리지 (08002xxx)
ERR_TRANSLATION_FILE_DOWNLOAD_FAILED = "08002001"  # presigned_url 파일 다운로드 실패
ERR_TRANSLATION_FILE_NOT_FOUND = "08002002"        # 파일 경로/입력 파일 없음
ERR_TRANSLATION_PREVIEW_FAILED = "08002003"        # 원본/번역본 preview 생성 실패
ERR_TRANSLATION_UPLOAD_FAILED = "08002004"         # 결과 파일 업로드 실패

# 번역 파이프라인 (08003xxx)
ERR_TRANSLATION_START_FAILED = "08003001"          # 스트리밍 번역 job 시작 실패
ERR_TRANSLATION_PIPELINE_FAILED = "08003002"       # Office 번역 파이프라인 실패
ERR_TRANSLATION_REVISION_FAILED = "08003003"       # 부분 재시도/수정 번역 실패
ERR_TRANSLATION_VALIDATION_FAILED = "08003004"     # 번역 응답 검증 실패

# 알 수 없는 오류 (08099xxx)
ERR_TRANSLATION_UNKNOWN = "08099999"


def translation_error_payload(
    msg: str,
    error_code: str = ERR_TRANSLATION_UNKNOWN,
    *,
    detail: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the canonical SSE error payload used by translation."""

    payload: dict[str, Any] = {
        "error_code": str(error_code or ERR_TRANSLATION_UNKNOWN),
        "msg": str(msg or "문서 번역 중 오류가 발생했습니다."),
    }
    payload["errMsg"] = payload["msg"]
    if detail:
        payload["detail"] = str(detail)
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def normalize_translation_error(
    data: Any,
    default_code: str = ERR_TRANSLATION_UNKNOWN,
) -> dict[str, Any]:
    """Normalize arbitrary exception/error data into the GenOS error shape."""

    if isinstance(data, GenATranslationException):
        return translation_error_payload(
            data.msg,
            data.error_code,
            detail=data.detail,
        )

    if isinstance(data, dict):
        msg = (
            data.get("msg")
            or data.get("errMsg")
            or data.get("translation_error")
            or data.get("preview_error")
            or data.get("error")
            or data.get("message")
            or data.get("detail")
            or "문서 번역 중 오류가 발생했습니다."
        )
        payload = translation_error_payload(
            str(msg),
            str(data.get("error_code") or default_code or ERR_TRANSLATION_UNKNOWN),
            detail=str(data["detail"]) if data.get("detail") and data.get("detail") != msg else None,
        )
        for key in (
            "job_id",
            "current_scope",
            "failed_scope",
            "current_slide",
            "current_page",
            "current_sheet",
            "current_sheet_name",
            "total_slides",
            "total_pages",
            "total_sheets",
            "event_phase",
        ):
            if key in data and data[key] is not None:
                payload[key] = data[key]
        return payload

    detail = getattr(data, "detail", None)
    msg = detail or str(data) or "문서 번역 중 오류가 발생했습니다."
    return translation_error_payload(str(msg), default_code, detail=str(data) if detail else None)
