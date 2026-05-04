from __future__ import annotations

import asyncio
import importlib.util
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
    }
    for module_name, package_name in packages.items():
        if importlib.util.find_spec(module_name) is None:
            _install_pip_package(package_name)


_ensure_runtime_packages()

import aiohttp
from dotenv import load_dotenv
from utils.stream import create_sse_response

try:
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse
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

if os.path.exists(BASE_DIR / ".env.local"):
    load_dotenv(BASE_DIR / ".env.local")

@dataclass(frozen=True)
class TranslationTarget:
    file_download_url: str | None
    metadata: dict[str, Any]


def _genos_event(event: str, data: Any = None) -> dict[str, Any]:
    return {"event": event, "data": data}


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


def _progress_text(event_name: str, payload: dict[str, Any]) -> str:
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
    payload = item.get("data") if isinstance(item.get("data"), dict) else {}
    progress_text = _progress_text(event_name, payload)

    if event_name == "job_error":
        return [
            _genos_event("agentFlowExecutedData", _agent_flow("Document Translation", {"visible_rationale": progress_text})),
            _genos_event("error", payload.get("translation_error") or progress_text),
            _genos_event("result", result_data),
        ]

    if event_name == "completed":
        return [
            _genos_event(
                "agentFlowExecutedData",
                _agent_flow("Document Translation Result", {"visible_rationale": progress_text}),
            ),
            _genos_event("token", progress_text),
            _genos_event("result", result_data or payload),
        ]

    return [
        _genos_event(
            "agentFlowExecutedData",
            _agent_flow("Document Translation Progress", {"visible_rationale": progress_text}),
        )
    ]


def _is_streaming_office_payload(payload: dict[str, Any]) -> bool:
    filename = str(payload.get("filename") or payload.get("file_name") or payload.get("file") or "")
    return Path(filename).suffix.lower() in {".docx", ".pptx", ".xlsx"}


class DocumentTranslationSseService:
    def __init__(self, data: dict[str, Any]):
        self.data = dict(data or {})

    def log_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return event

    async def handle_error(self, exc: Exception) -> AsyncIterator[dict[str, Any]]:
        message = str(exc)
        yield self.log_event(_genos_event("error", message))
        yield self.log_event(_genos_event("token", f"\n\n오류가 발생했습니다: {message}"))
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
        if _is_streaming_office_payload(payload):
            async for event in self._run_streaming_office_payload(payload):
                yield event
            return

        result = await _run_translation_payload(payload)
        text = str(result.get("text") or result.get("translated_text") or "")
        if text:
            yield self.log_event(_genos_event("token", text))
        yield self.log_event(_genos_event("result", result))

    async def _run_streaming_office_payload(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        import translation_ochestration
        from translation_pipeline.common.nodes import build_download_payload
        from translation_pipeline.common.translation_jobs import get_translation_job, stream_translation_job

        with tempfile.TemporaryDirectory(prefix="ai-translation-sse-") as preview_dir:
            start_payload = dict(payload)
            start_payload["_preview_output_dir"] = preview_dir
            start_payload["_preview_base_url"] = ""
            result = await translation_ochestration.start_streaming(start_payload)
            job_id = result.get("job_id")

            if not job_id:
                message = str(result.get("text") or "문서 번역 스트리밍 작업을 시작할 수 없습니다.")
                yield self.log_event(_genos_event("error", message))
                yield self.log_event(_genos_event("result", result))
                return

            yield self.log_event(_genos_event("token", "문서 번역을 시작합니다.\n"))
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
                            result_data.update(build_download_payload(translated_file_path))

                for event in _events_from_translation_event(item, result_data):
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
        load_dotenv(BASE_DIR / ".env.local.fullstack", override=True)
        _apply_config_to_env(config or {})

        request_data = dict(data or {})
        if request_data.get("stream") is False:
            if _has_sources(request_data):
                return await _run_sources_payload(request_data)
            return await _run_translation_payload(request_data)

        stream_service = DocumentTranslationSseService(request_data)
        return await create_sse_response(stream_service.run())
    except HTTPException:
        raise
    except Exception as exc:
        async def _error_events() -> AsyncIterator[dict[str, Any]]:
            yield _genos_event("error", f"문서 번역 처리 중 문제가 발생했습니다: {exc}")
            yield _genos_event("result", {"success": False, "message": str(exc)})

        return await create_sse_response(_error_events())


async def _run_json_service(config: dict, data: dict) -> dict[str, Any]:
    try:
        load_dotenv(BASE_DIR / ".env")
        load_dotenv(BASE_DIR / ".env.local.fullstack", override=True)
        _apply_config_to_env(config or {})

        request_data = dict(data or {})
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
    return _strip_internal_fields(result)


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
        normalized_results.append({"index": index, "status": "completed", **item})

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


async def _run_single_target(
    data: dict[str, Any],
    target: TranslationTarget,
    *,
    index: int = 0,
) -> dict[str, Any]:
    if not target.file_download_url:
        raise HTTPException(status_code=400, detail="sources item must include presigned_url")

    filename = _file_name_from_metadata(target.metadata) or _file_name_from_url(target.file_download_url)
    async with _download_to_temp_file(target.file_download_url, filename) as temp_path:
        payload = _build_payload_for_target(data, target, temp_path, filename)
        payload["_source_index"] = index
        return await _run_translation_payload(payload)


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
    metadata = item.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="each sources item metadata must be a dict")

    return TranslationTarget(
        file_download_url=str(direct_url) if direct_url else None,
        metadata=dict(metadata),
    )


def _has_sources(data: dict[str, Any]) -> bool:
    sources = data.get("sources")
    return isinstance(sources, list) and bool(sources)


def _first_url(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
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
    print(result)

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
    print(result)


if __name__ == "__main__":
    asyncio.run(test())
