from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _install_pip_package(package_name: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])


def _ensure_runtime_packages() -> None:
    if os.getenv("AI_TRANSLATION_AUTO_INSTALL_PACKAGES", "1") == "0":
        return

    packages = {
        "aiohttp": "aiohttp",
        "fastapi": "fastapi",
        "lxml": "lxml",
        "openai": "openai",
        "openpyxl": "openpyxl",
        "pdfplumber": "pdfplumber",
        "PIL": "pillow",
        "fitz": "pymupdf",
        "docx": "python-docx",
        "dotenv": "python-dotenv",
        "pptx": "python-pptx",
        "uvicorn": "uvicorn[standard]",
        "redis": "redis",
        "requests": "requests",
        "html2text": "html2text",
        "multipart": "python-multipart",
        "jinja2": "Jinja2",
        "cryptography": "cryptography",
        "weaviate": "weaviate-client",
        "azure-storage-blob": "azure-storage-blob",
    }
    for module_name, package_name in packages.items():
        if importlib.util.find_spec(module_name) is None:
            _install_pip_package(package_name)


_ensure_runtime_packages()

import aiohttp
from dotenv import load_dotenv
from utils.stream import create_sse_response
from utils.pricing import credit_payload

try:
    from fastapi import HTTPException  # type: ignore
    from fastapi.responses import StreamingResponse # type: ignore[no-redef]
except Exception:  # pragma: no cover - SaaS 런타임에 FastAPI가 없을 때의 최소 fallback
    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str) -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    class StreamingResponse:  # type: ignore[no-redef]
        pass


URL_KEYS = (
    "presigned_url",
    "file_download_url",
    "download_url",
    "minio_url",
    "minio_address",
    "url",
)
OFFICE_PREVIEW_EXTENSIONS = {".docx", ".pptx", ".xlsx"}

if os.path.exists(BASE_DIR / ".env.local"):
    load_dotenv(BASE_DIR / ".env.local")
if os.path.exists(BASE_DIR / ".env.local.fullstack"):
    load_dotenv(BASE_DIR / ".env.local.fullstack", override=False)


_logger = logging.getLogger("uvicorn.error")
_PREVIEW_DIAG_KEY = "_GENA_PREVIEW_DIAG_LOGGED"


def _log_preview_env_diagnostics() -> None:
    """미리보기 파이프라인 환경(Azure 연결 + preview root) 을 첫 호출 시 1회 로깅.

    ``AccountKey`` 는 마스킹하고 활성 여부만 출력한다.
    """

    if os.environ.get(_PREVIEW_DIAG_KEY) == "1":
        return
    os.environ[_PREVIEW_DIAG_KEY] = "1"

    try:
        from translation_pipeline.common.azure_uploader import (
            _connection_string,
            _container_name,
            _parse_connection_string,
            _preview_prefix,
            is_azure_preview_enabled,
        )
    except Exception as exc:  # pragma: no cover
        _logger.warning("[preview-env] azure_uploader import 실패: %s", exc)
        return

    conn = _connection_string()
    parsed = _parse_connection_string(conn) if conn else {}
    account_name = parsed.get("AccountName", "").strip() or "(missing)"
    has_key = bool(parsed.get("AccountKey", "").strip())
    preview_root = os.environ.get("AI_TRANSLATION_PREVIEW_ROOT", "")
    _logger.info(
        "[preview-env] azure_enabled=%s account=%s has_key=%s container=%s prefix=%s preview_root=%s",
        is_azure_preview_enabled(),
        account_name,
        has_key,
        _container_name(),
        _preview_prefix(),
        preview_root or "(missing)",
    )


@dataclass(frozen=True)
class TranslationTarget:
    file_download_url: str | None
    file_value: str | None
    metadata: dict[str, Any]


# SSE 페이로드에서 제거할 대용량·미소비 키. FE 는 *_html_url 만 사용하며, 슬라이드
# 수에 비례한 선형 증가가 SSE readline truncation 위험을 키운다 — streaming office
# (`publish_translation_event` chokepoint) 외에 revision / plain translation /
# job_error 의 ``result`` 이벤트 등 모든 emission 경로를 단일 지점에서 차단하기 위해
# ``_genos_event`` 에서도 한 번 더 strip 한다 (idempotent — 이미 비어 있으면 no-op).
_HEAVY_PAYLOAD_KEYS = ("document_blocks", "pairs", "translation_pairs")
_COMPLETION_RESULT_KEYS = (
    "translation_status",
    "translation_error",
    "translation_notice",
    "translation_skipped_reason",
    "preview_status",
    "preview_error",
    "preview_render_mode",
    "original_preview_html_url",
    "original_preview_status",
    "translated_preview_html_url",
    "translated_preview_status",
    "translated_file_url",
    "output_filename",
    "job_id",
    "current_slide",
    "total_slides",
    "current_page",
    "total_pages",
    "current_sheet",
    "total_sheets",
    "current_sheet_name",
    "llm_model_name",
    "llm_provider_sort",
    "created_at",
    "completed_at",
    "elapsed_ms",
)


def _genos_event(event: str, data: Any = None) -> dict[str, Any]:
    if isinstance(data, dict) and any(key in data for key in _HEAVY_PAYLOAD_KEYS):
        data = {key: value for key, value in data.items() if key not in _HEAVY_PAYLOAD_KEYS}
    return {"event": event, "data": data}


def _slim_completion_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """완료 SSE 에서 FE 가 쓰는 메타만 남긴다."""

    return {
        key: payload[key]
        for key in _COMPLETION_RESULT_KEYS
        if key in payload and payload[key] is not None
    }


def _credit_events() -> list[dict[str, Any]]:
    payload = credit_payload()
    usage_total = payload.get("usage_total", {})
    gena_credit_usage = payload.get("gena_credit_usage", 0.0)
    return [
        _genos_event("usage_total", usage_total),
        _genos_event("gena_credit_usage", gena_credit_usage),
    ]


def _agent_flow(node_label: str, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodeLabel": node_label,
        "data": {
            "output": {
                "content": _json_dumps(content),
            },
        },
    }


def _json_dumps(data: Any) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)


def _scope_label(payload: dict[str, Any]) -> str:
    current_slide = payload.get("current_slide")
    current_page = payload.get("current_page")
    current_sheet = payload.get("current_sheet")
    current_sheet_name = payload.get("current_sheet_name")

    if current_slide:
        total = payload.get("total_slides")
        return f"슬라이드 {current_slide}/{total}" if total else f"슬라이드 {current_slide}"
    if current_page:
        total = payload.get("total_pages")
        return f"페이지 {current_page}/{total}" if total else f"페이지 {current_page}"
    if current_sheet:
        total = payload.get("total_sheets")
        sheet_label = f"시트 {current_sheet}/{total}" if total else f"시트 {current_sheet}"
        return f"{sheet_label} ({current_sheet_name})" if current_sheet_name else sheet_label
    return "문서"


def _progress_text(event_name: str, payload: dict[str, Any] | Any) -> str:
    scope_label = _scope_label(payload)
    if event_name == "original_preview_ready":
        return "원본 문서 미리보기를 준비했습니다."
    if event_name == "translation_started":
        return "문서 번역을 시작했습니다."
    if event_name.endswith("_translation_started"):
        return f"{scope_label} 번역을 시작했습니다."
    if event_name in {"slide_translated", "page_translated", "sheet_translated", "blocks_translated"}:
        return f"{scope_label} 번역을 완료했습니다."
    if event_name.endswith("_injected"):
        return f"{scope_label} 번역 내용을 문서에 반영했습니다."
    if event_name.endswith("_html_ready"):
        return f"{scope_label} 미리보기를 갱신했습니다."
    if event_name == "completed":
        return "문서 번역이 완료되었습니다."
    if event_name == "job_error":
        return f"문서 번역 중 오류가 발생했습니다: {payload.get('translation_error') or '알 수 없는 오류'}"
    return str(payload.get("event_phase") or event_name)


def _events_from_translation_event(item: dict[str, Any], result_data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    event_name = str(item.get("event") or "message")
    payload = item.get("data", {"translation_error": "알 수 없는 오류"}) if isinstance(item.get("data"), dict) else {}
    progress_text = _progress_text(event_name, payload)

    if event_name == "job_error":
        return [
            _genos_event(event_name, payload),
            _genos_event("agentFlowExecutedData", _agent_flow("Document Translation", {"visible_rationale": progress_text})),
            _genos_event("error", payload.get("translation_error") or progress_text),
            _genos_event("result", result_data),
        ]

    if event_name == "completed":
        completed_data = result_data or payload
        slim_completed_data = _slim_completion_payload(completed_data)
        return [
            _genos_event(event_name, slim_completed_data),
            _genos_event(
                "agentFlowExecutedData",
                _agent_flow("Document Translation Result", {"visible_rationale": progress_text}),
            ),
            _genos_event("result", slim_completed_data),
            *_credit_events(),
        ]

    # slide_translation_started / slide_translated / slide_injected / slide_html_ready /
    # original_preview_ready 등 파이프라인 raw 이벤트는 그대로 통과시킨다.
    return [
        _genos_event(event_name, payload),
        _genos_event(
            "agentFlowExecutedData",
            _agent_flow("Document Translation Progress", {"visible_rationale": progress_text}),
        )
    ]


def _is_streaming_office_payload(payload: dict[str, Any]) -> bool:
    filename = str(payload.get("filename") or payload.get("file_name") or payload.get("file") or "")
    return Path(filename).suffix.lower() in OFFICE_PREVIEW_EXTENSIONS


def _office_extension_from_payload(payload: dict[str, Any]) -> str:
    for key in ("filename", "file_name", "original_filename", "output_filename", "file"):
        value = payload.get(key)
        if not value:
            continue
        suffix = Path(str(value)).suffix.lower()
        if suffix in OFFICE_PREVIEW_EXTENSIONS:
            return suffix
    return ""


def _expects_office_preview(request_payload: dict[str, Any], result_payload: dict[str, Any]) -> bool:
    if _office_extension_from_payload(request_payload) or _office_extension_from_payload(result_payload):
        return True
    if result_payload.get("preview_render_mode") == "html":
        return True
    return any(
        key in result_payload
        for key in (
            "original_preview_status",
            "translated_preview_status",
            "original_preview_html_url",
            "translated_preview_html_url",
        )
    )


def _with_preview_status(
    result_payload: dict[str, Any],
    request_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(result_payload or {})
    request = dict(request_payload or {})

    if not _expects_office_preview(request, result):
        return result

    original_url = str(result.get("original_preview_html_url") or "")
    translated_url = str(result.get("translated_preview_html_url") or "")
    has_any_preview = bool(original_url or translated_url)
    has_translated_preview = bool(translated_url)

    if has_any_preview and result.get("preview_status") not in {"failed", "error"}:
        result.setdefault("preview_status", "done")

    if result.get("translation_status") == "done" and not has_translated_preview:
        result["preview_status"] = "failed"
        result["translated_preview_status"] = "error"
        result.setdefault(
            "preview_error",
            "번역은 완료되었지만 미리보기 파일 URL이 생성되지 않았습니다.",
        )
        _logger.warning(
            "[preview-status] mark failed — translation_status=done 인데 translated_preview_html_url 없음. "
            "job_id=%s original_present=%s translated_present=%s",
            result.get("job_id"),
            bool(original_url),
            bool(translated_url),
        )

    if result.get("original_preview_status") == "error" and not original_url:
        result.setdefault("preview_status", "failed")
        result.setdefault(
            "preview_error",
            "원본 미리보기 파일 URL이 생성되지 않았습니다.",
        )
        _logger.warning(
            "[preview-status] mark failed — original_preview_html_url 없음. job_id=%s",
            result.get("job_id"),
        )

    return result


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _is_translation_evaluation_request(payload: dict[str, Any]) -> bool:
    """번역 평가용 테스트 응답을 요청했는지 판별한다."""

    mode = str(
        payload.get("test_mode")
        or payload.get("mode")
        or payload.get("response_mode")
        or ""
    ).strip().lower()
    if mode in {
        "translation_evaluation",
        "translation_eval",
        "evaluation",
        "eval",
        "test",
        "true",
        "1",
        "yes",
        "y",
        "test_translation_units",
    }:
        return True
    return any(
        _is_truthy(payload.get(key))
        for key in (
            "translation_evaluation",
            "evaluation_mode",
            "return_translation_units",
            "return_evaluation_units",
        )
    )


def _is_revision_payload(payload: dict[str, Any]) -> bool:
    return str(payload.get("mode") or "").strip().lower() == "revise"


def _with_default_return_file(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data or {})
    if _is_revision_payload(payload) or "is_return_file" in payload:
        return payload
    if _has_sources(payload) or payload.get("file"):
        payload["is_return_file"] = True
    return payload


class DocumentTranslationSseService:
    def __init__(self, data: dict[str, Any]):
        self.data = dict(data or {})

    def log_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return event

    async def handle_error(self, exc: Exception) -> AsyncIterator[dict[str, Any]]:
        message = str(exc)
        yield self.log_event(_genos_event("error", message))
        yield self.log_event(_genos_event("result", {"success": False, "message": message}))

    async def run(self) -> AsyncIterator[dict[str, Any]]:
        try:
            if _has_sources(self.data):
                async for event in self._run_sources():
                    yield event
                return

            async for event in self._run_payload(dict(self.data)):
                yield event
        except Exception as exc:
            async for event in self.handle_error(exc):
                yield event

    async def _run_sources(self) -> AsyncIterator[dict[str, Any]]:
        targets = _extract_translation_targets(self.data)
        for index, target in enumerate(targets):
            if not target.file_download_url:
                raise HTTPException(status_code=400, detail="sources item must include presigned_url")

            filename = _file_name_from_metadata(target.metadata) or _file_name_from_url(target.file_download_url)
            yield self.log_event(
                _genos_event(
                    "agentFlowExecutedData",
                    _agent_flow(
                        "Document Download",
                        {"visible_rationale": f"{filename} 파일을 다운로드합니다."},
                    ),
                )
            )
            async with _download_to_temp_file(target.file_download_url, filename) as temp_path:
                payload = _build_payload_for_target(self.data, target, temp_path, filename)
                payload["_source_index"] = index
                async for event in self._run_payload(payload):
                    yield event

    async def _run_payload(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if _is_revision_payload(payload):
            async for event in self._run_revision_payload(payload):
                yield event
            return

        if _is_streaming_office_payload(payload):
            async for event in self._run_streaming_office_payload(payload):
                yield event
            return

        result = await _run_translation_payload(payload)
        yield self.log_event(_genos_event("result", result))
        for event in _credit_events():
            yield self.log_event(event)

    async def _run_streaming_office_payload(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        import translation_ochestration
        from translation_pipeline.common.nodes import build_download_payload
        from translation_pipeline.common.translation_jobs import (
            get_translation_job,
            stream_translation_job,
            update_translation_job,
        )

        # 로컬 작업 디렉터리만 필요 (LibreOffice 가 PNG/HTML 을 임시로 떨어뜨림).
        # 서빙은 Azure Blob 만 사용하므로 `_preview_base_url` 은 빈 문자열로 둔다.
        preview_root = os.environ.get(
            "AI_TRANSLATION_PREVIEW_ROOT",
            os.path.join(tempfile.gettempdir(), "ai_translation_previews"),
        )
        os.makedirs(preview_root, exist_ok=True)
        start_payload = dict(payload)
        start_payload["_preview_output_dir"] = preview_root
        start_payload["_preview_base_url"] = ""
        result = await translation_ochestration.start_streaming(start_payload)
        job_id = result.get("job_id")

        if not job_id:
            message = str(result.get("text") or "문서 번역 스트리밍 작업을 시작할 수 없습니다.")
            yield self.log_event(_genos_event("error", message))
            yield self.log_event(_genos_event("result", result))
            return

        yield self.log_event(
            _genos_event(
                "agentFlowExecutedData",
                _agent_flow("Document Translation", {"visible_rationale": "문서 번역 스트리밍 작업을 시작했습니다."}),
            )
        )

        async for item in stream_translation_job(str(job_id), 0):
            result_data = None
            if item.get("event") in {"completed", "job_error"}:
                result_data = dict(item.get("data") or {})
                job = get_translation_job(str(job_id)) or {}
                job_payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
                if payload.get("is_return_file") and job_payload:
                    translated_file_path = str(job_payload.get("_translated_file_path") or "")
                    if translated_file_path and os.path.exists(translated_file_path):
                        download_payload = build_download_payload(translated_file_path)
                        # download_payload 내부 file_base64 는 SSE 페이로드를 비대(수 MB)
                        # 하게 만들어 외부 GenOS 게이트웨이가 통째로 드롭하는 사례를 만든다
                        # (운영 사용자 보고 — `completed`/`result` 이벤트가 아예 사라짐).
                        # Azure SAS URL (`translated_file_url`) 로 다운로드를 대체했으므로
                        # base64 는 SSE 응답에서 제거하고 job 내부 캐시에만 남긴다.
                        update_translation_job(str(job_id), download_payload)
                        download_payload_for_sse = {
                            k: v
                            for k, v in download_payload.items()
                            if k not in {"file_base64", "file_path", "mime_type"}
                        }
                        result_data.update(download_payload_for_sse)
                result_data = _with_preview_status(result_data, payload)
                # 같은 이유로 SSE 응답에 직접 들어가는 result_data 자체에서도 base64
                # 잔재를 제거해 게이트웨이 드롭을 방지한다.
                for stripped_key in ("file_base64", "file_path", "mime_type"):
                    result_data.pop(stripped_key, None)
                update_translation_job(str(job_id), result_data)

            for event in _events_from_translation_event(item, result_data):
                yield self.log_event(event)

    async def _run_revision_payload(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield self.log_event(
            _genos_event(
                "agentFlowExecutedData",
                _agent_flow("Document Revision", {"visible_rationale": "수정 번역을 시작합니다."}),
            )
        )
        result = await _run_revision_payload(payload)
        result = _with_preview_status(result, payload)
        message = "수정 번역이 완료되었습니다."
        if result.get("translation_error"):
            message = f"수정 번역 중 오류가 발생했습니다: {result.get('translation_error')}"
            yield self.log_event(_genos_event("error", message))
        yield self.log_event(_genos_event("result", result))
        for event in _credit_events():
            yield self.log_event(event)


async def service(config: dict, data: dict) -> StreamingResponse | dict[str, Any]:
    """GenOS/SaaS document translation serving entrypoint.

    Supported input shapes:
    - Existing workflow payload:
      {"file": "<base64-or-local-path>", "filename": "example.pptx", "format": "English"}
    - Presigned URL payload:
      {"sources": [{"presigned_url": "https://...", "metadata": {"file_name": "example.pptx"}}],
       "format": "English"}

    By default this returns an SSE StreamingResponse. Pass `stream: false` in
    data to keep the legacy one-shot JSON response.
    """

    try:
        load_dotenv(BASE_DIR / ".env")
        _apply_config_to_env(config or {})

        _log_preview_env_diagnostics()

        request_data = _with_default_return_file(dict(data or {}))
        if _is_translation_evaluation_request(request_data):
            request_data["is_return_file"] = False
            if _has_sources(request_data):
                return await _run_sources_evaluation_payload(request_data)
            return await _run_translation_evaluation_payload(request_data)

        if request_data.get("stream") is False:
            if _is_revision_payload(request_data):
                return await _run_revision_payload(request_data)
            if _has_sources(request_data):
                return await _run_sources_payload(request_data)
            return await _run_translation_payload(request_data)

        stream_service = DocumentTranslationSseService(request_data)
        return await create_sse_response(stream_service.run())
    except HTTPException:
        raise
    except Exception as exc:
        error_message = f"문서 번역 처리 중 문제가 발생했습니다: {exc}"
        if isinstance(data, dict) and _is_translation_evaluation_request(data):
            return {
                "test_mode": "translation_evaluation",
                "translation_status": "error",
                "translation_error": error_message,
                "translation_units": [],
            }

        async def _error_events() -> AsyncIterator[dict[str, Any]]:
            yield _genos_event("error", error_message)
            yield _genos_event("result", {"success": False, "message": error_message})

        return await create_sse_response(_error_events())


async def _run_json_service(config: dict, data: dict) -> dict[str, Any]:
    try:
        load_dotenv(BASE_DIR / ".env")
        _apply_config_to_env(config or {})

        request_data = _with_default_return_file(dict(data or {}))
        if _is_translation_evaluation_request(request_data):
            request_data["is_return_file"] = False
            if _has_sources(request_data):
                return await _run_sources_evaluation_payload(request_data)
            return await _run_translation_evaluation_payload(request_data)

        if _is_revision_payload(request_data):
            return await _run_revision_payload(request_data)
        if _has_sources(request_data):
            return await _run_sources_payload(request_data)

        return await _run_translation_payload(request_data)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"문서 번역 처리 중 문제가 발생했습니다: {exc}") from exc


async def _run_translation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    import translation_ochestration

    result = await translation_ochestration.run(dict(payload))
    return _strip_internal_fields(_with_preview_status(result, payload))


async def _run_translation_evaluation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    import translation_ochestration

    result = await translation_ochestration.run_evaluation(dict(payload))
    return _strip_internal_fields(result)


async def _run_revision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    import translation_ochestration
    from translation_pipeline.common.nodes import build_download_payload
    from translation_pipeline.common.translation_jobs import get_translation_job

    request_payload = dict(payload)
    return_file = _is_truthy(request_payload.get("is_return_file"))

    if return_file:
        with tempfile.TemporaryDirectory(prefix="ai-translation-revision-") as preview_dir:
            request_payload["_preview_output_dir"] = preview_dir
            request_payload["_preview_base_url"] = ""
            result = await translation_ochestration.revise_translation(request_payload)
            job = get_translation_job(str(request_payload.get("job_id") or "")) or {}
            job_payload = job.get("payload", {}) if isinstance(job.get("payload"), dict) else {}
            translated_file_path = str(job_payload.get("_translated_file_path", {}) or "")
            if translated_file_path and os.path.exists(translated_file_path):
                result.update(build_download_payload(translated_file_path))
            return _strip_internal_fields(_with_preview_status(result, payload))

    result = await translation_ochestration.revise_translation(request_payload)
    return _strip_internal_fields(_with_preview_status(result, payload))


async def _run_sources_payload(data: dict[str, Any]) -> dict[str, Any]:
    targets = _extract_translation_targets(data)
    if len(targets) == 1:
        return await _run_single_target(data, targets[0])

    results = await asyncio.gather(
        *[_run_single_target(data, target, index=index) for index, target in enumerate(targets)],
        return_exceptions=True,
    )
    normalized_results: list[dict[str, Any]] = []
    success_count = 0
    for index, item in enumerate(results):
        if isinstance(item, Exception):
            normalized_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "text": f"[에러] 처리 실패: {item}",
                }
            )
            continue
        success_count += 1
        normalized_results.append({"index": index, "status": "completed", **(item if isinstance(item, dict) else {})})

    failure_count = len(normalized_results) - success_count
    if failure_count == 0:
        status = "completed"
    elif success_count == 0:
        status = "failed"
    else:
        status = "partial_success"

    return {
        "status": status,
        "results": normalized_results,
        "success_count": success_count,
        "failure_count": failure_count,
    }


async def _run_sources_evaluation_payload(data: dict[str, Any]) -> dict[str, Any]:
    targets = _extract_translation_targets(data)
    if len(targets) == 1:
        return await _run_single_target_evaluation(data, targets[0])

    results = await asyncio.gather(
        *[_run_single_target_evaluation(data, target, index=index) for index, target in enumerate(targets)],
        return_exceptions=True,
    )
    normalized_results: list[dict[str, Any]] = []
    success_count = 0
    for index, item in enumerate(results):
        if isinstance(item, Exception):
            normalized_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "test_mode": "translation_evaluation",
                    "translation_status": "error",
                    "translation_error": f"처리 실패: {item}",
                    "translation_units": [],
                }
            )
            continue
        if isinstance(item, dict) and item.get("translation_status") != "error":
            success_count += 1
        normalized_results.append({"index": index, "status": "completed", **(item if isinstance(item, dict) else {})})

    failure_count = len(normalized_results) - success_count
    if failure_count == 0:
        status = "completed"
    elif success_count == 0:
        status = "failed"
    else:
        status = "partial_success"

    return {
        "test_mode": "translation_evaluation",
        "status": status,
        "results": normalized_results,
        "success_count": success_count,
        "failure_count": failure_count,
    }


async def _run_single_target(
    data: dict[str, Any],
    target: TranslationTarget,
    *,
    index: int = 0,
) -> dict[str, Any]:
    if not target.file_download_url:
        raise HTTPException(status_code=400, detail="sources item must include presigned_url")

    filename = (
        _file_name_from_metadata(target.metadata)
        or (_file_name_from_url(target.file_download_url) if target.file_download_url else None)
        or "translation-input.bin"
    )
    async with _download_to_temp_file(target.file_download_url, filename) as temp_path:
        payload = _build_payload_for_target(data, target, temp_path, filename)
        payload["_source_index"] = index
        return await _run_translation_payload(payload)


async def _run_single_target_evaluation(
    data: dict[str, Any],
    target: TranslationTarget,
    *,
    index: int = 0,
) -> dict[str, Any]:
    filename = _file_name_from_metadata(target.metadata) or _file_name_from_url(target.file_download_url)
    if target.file_value:
        payload = _build_payload_for_target(data, target, target.file_value, filename)
        payload["_source_index"] = index
        payload["is_return_file"] = False
        return await _run_translation_evaluation_payload(payload)

    if not target.file_download_url:
        raise HTTPException(
            status_code=400,
            detail="evaluation sources item must include presigned_url or file",
        )

    async with _download_to_temp_file(target.file_download_url, filename) as temp_path:
        payload = _build_payload_for_target(data, target, temp_path, filename)
        payload["_source_index"] = index
        payload["is_return_file"] = False
        return await _run_translation_evaluation_payload(payload)


def _build_payload_for_target(
    data: dict[str, Any],
    target: TranslationTarget,
    temp_path: str,
    filename: str,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in data.items()
        if key not in {"sources", *URL_KEYS}
    }
    metadata = dict(target.metadata)
    payload.update(metadata)
    payload["file"] = temp_path
    payload["filename"] = payload.get("filename") or payload.get("file_name") or filename
    return payload


@asynccontextmanager
async def _download_to_temp_file(url: str, filename: str) -> AsyncIterator[str]:
    suffix = Path(filename).suffix or ".bin"
    fd, temp_path = tempfile.mkstemp(prefix="ai-translation-", suffix=suffix)
    os.close(fd)
    try:
        timeout = aiohttp.ClientTimeout(total=float(os.getenv("AI_TRANSLATION_DOWNLOAD_TIMEOUT", "120")))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status >= 400:
                    raise HTTPException(
                        status_code=400,
                        detail=f"파일 다운로드 실패: HTTP {response.status}",
                    )
                with open(temp_path, "wb") as output:
                    async for chunk in response.content.iter_chunked(1024 * 512):
                        output.write(chunk)
        yield temp_path
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _extract_translation_targets(data: dict[str, Any]) -> list[TranslationTarget]:
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise HTTPException(status_code=400, detail="sources must be a non-empty list")
    return [_extract_target_from_item(item) for item in sources]


def _extract_target_from_item(item: Any) -> TranslationTarget:
    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail="each sources item must be an object")

    direct_url = _first_url(item, URL_KEYS)
    direct_file = _first_value(
        item,
        ("file", "file_base64", "base64", "content", "local_path", "path"),
    )
    metadata = item.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="each sources item metadata must be a dict")
    metadata = dict(metadata)
    for source_key, metadata_key in (
        ("name", "file_name"),
        ("filename", "filename"),
        ("original_filename", "original_filename"),
        ("type", "file_type"),
    ):
        value = item.get(source_key)
        if value and metadata_key not in metadata:
            metadata[metadata_key] = str(value)

    return TranslationTarget(
        file_download_url=str(direct_url) if direct_url else None,
        file_value=str(direct_file) if direct_file else None,
        metadata=metadata,
    )


def _has_sources(data: dict[str, Any]) -> bool:
    sources = data.get("sources")
    return isinstance(sources, list) and bool(sources)


def _first_url(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value = _first_value(data, keys)
    return str(value) if value else None


def _first_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


def _file_name_from_metadata(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("file_name") or metadata.get("filename") or metadata.get("original_filename")
    return str(value) if value else None


def _file_name_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name
    return name or "document.bin"


def _apply_config_to_env(config: dict[str, Any]) -> None:
    for key, value in config.items():
        if value is None:
            continue
        os.environ[str(key)] = str(value)


def _strip_internal_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if not key.startswith("_preview_")
    }


async def plain_text_test() -> None:
    result = await service(
        {},
        {
            "input_text": "Hello world.",
            "format": "Korean",
        },
    )
    log_info(result)

async def test() -> None:
    result = await service(
        {},
        {
            "file": "/workspace/38/test/resources/ex_sheet_2.xlsx",
            "filename": "예시.pptx",
            "format": "Korean",
            "is_return_file": False,
        },
    )
    log_info(result)


if __name__ == "__main__":
    asyncio.run(test())
