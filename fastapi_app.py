from __future__ import annotations

import os
import tempfile
import json
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

import translation_ochestration
from translation_pipeline.common.preview_jobs import get_preview_job
from translation_pipeline.common import realtime as realtime_translate
from translation_pipeline.common.translation_jobs import get_translation_job, stream_translation_job


PREVIEW_ROOT = os.getenv(
    "AI_TRANSLATION_PREVIEW_ROOT",
    os.path.join(tempfile.gettempdir(), "ai_translation_previews"),
)
os.makedirs(PREVIEW_ROOT, exist_ok=True)

app = FastAPI(title="AI Translation Local Backend", version="0.1.0")

app.mount(
    "/preview-files",
    StaticFiles(directory=PREVIEW_ROOT),
    name="preview-files",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _read_json(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON body를 읽을 수 없습니다.") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object body만 지원합니다.")

    return dict(payload)


def _attach_preview_url_config(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    data["_preview_output_dir"] = PREVIEW_ROOT
    data["_preview_base_url"] = f"{str(request.base_url).rstrip('/')}/preview-files"
    return data


def _strip_internal_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if not key.startswith("_preview_")
    }


async def _run_document_translation(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = await translation_ochestration.run(dict(payload))
    return _strip_internal_fields(result)


async def _start_document_translation(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = await translation_ochestration.start_streaming(dict(payload))
    return _strip_internal_fields(result)


async def _revise_document_translation(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = await translation_ochestration.revise_translation(dict(payload))
    return _strip_internal_fields(result)


async def _run_realtime_translation(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    if "text" not in data and "input_text" in data:
        data["text"] = data.get("input_text", "")

    result = await realtime_translate.run(data)
    translated = result.get("translated_text", "")

    return {
        **payload,
        "input_text": payload.get("input_text", result.get("text", "")),
        "text": translated,
        "translated_text": translated,
    }


def _genos_event(event: str, data: Any = None) -> Dict[str, Any]:
    return {"event": event, "data": data}


def _genos_sse_line(event: Dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=False)
    return f"data: {payload}\n\n"


def _genos_agent_flow(node_label: str, content: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "nodeLabel": node_label,
        "data": {
            "output": {
                "content": json.dumps(content, ensure_ascii=False),
            },
        },
    }


def _genos_scope_label(payload: Dict[str, Any]) -> str:
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


def _genos_progress_text(event_name: str, payload: Dict[str, Any]) -> str:
    scope_label = _genos_scope_label(payload)
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


def _genos_visible_urls(payload: Dict[str, Any]) -> list[str]:
    urls = []
    for key in ("translated_file_url", "translated_preview_html_url", "original_preview_html_url"):
        value = payload.get(key)
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)
    return urls


def _genos_events_from_translation_event(item: Dict[str, Any]) -> list[Dict[str, Any]]:
    event_name = str(item.get("event") or "message")
    payload = item.get("data") if isinstance(item.get("data"), dict) else {}
    progress_text = _genos_progress_text(event_name, payload)

    if event_name == "job_error":
        return [
            _genos_event(
                "agentFlowExecutedData",
                _genos_agent_flow("Document Translation", {"visible_rationale": progress_text}),
            ),
            _genos_event("error", payload.get("translation_error") or progress_text),
            _genos_event("result", None),
        ]

    if event_name == "completed":
        content: Dict[str, Any] = {"visible_rationale": progress_text}
        urls = _genos_visible_urls(payload)
        if urls:
            content["visible_url"] = urls
        summary = progress_text
        if urls:
            summary = f"{summary}\n번역 문서: {urls[0]}"
        return [
            _genos_event("agentFlowExecutedData", _genos_agent_flow("Document Translation Result", content)),
            _genos_event("token", summary),
            _genos_event("result", None),
        ]

    return [
        _genos_event(
            "agentFlowExecutedData",
            _genos_agent_flow(
                "Document Translation Progress",
                {"visible_rationale": progress_text},
            ),
        )
    ]


async def _serialize_genos_events(events: AsyncIterator[Dict[str, Any]]) -> AsyncIterator[str]:
    async for event in events:
        yield _genos_sse_line(event)


class DocumentTranslationGenosStreamService:
    """GenOS chat-service style document translation stream.

    The service yields dict events and leaves SSE serialization to the FastAPI
    route, mirroring BaseChatService.run() style generators.
    """

    def __init__(self, payload: Dict[str, Any], request: Request):
        self.payload = payload
        self.request = request
        self.job_id: str | None = None

    def log_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return event

    async def handle_error(self, exc: Exception) -> AsyncIterator[Dict[str, Any]]:
        message = str(exc)
        yield self.log_event(_genos_event("error", message))
        yield self.log_event(_genos_event("token", f"\n\n오류가 발생했습니다: {message}"))
        yield self.log_event(_genos_event("result", None))

    async def _emit_initial_result(self, result: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        job_id = result.get("job_id")
        if not job_id:
            message = str(result.get("text") or "문서 번역 스트리밍 작업을 시작할 수 없습니다.")
            yield self.log_event(_genos_event("error", message))
            yield self.log_event(_genos_event("result", None))
            return

        self.job_id = str(job_id)
        yield self.log_event(_genos_event("token", "문서 번역을 시작합니다.\n"))
        yield self.log_event(
            _genos_event(
                "agentFlowExecutedData",
                _genos_agent_flow(
                    "Document Translation",
                    {"visible_rationale": "문서 번역 스트리밍 작업을 시작했습니다."},
                ),
            )
        )

        if result.get("translation_status") == "done":
            for event in _genos_events_from_translation_event(
                {"event": "completed", "data": {**result, "job_id": self.job_id}}
            ):
                yield self.log_event(event)
            return

        if result.get("translation_status") == "error":
            for event in _genos_events_from_translation_event(
                {"event": "job_error", "data": {**result, "job_id": self.job_id}}
            ):
                yield self.log_event(event)
            return

        async for event in self._stream_job_events():
            yield self.log_event(event)

    async def _stream_job_events(self) -> AsyncIterator[Dict[str, Any]]:
        if not self.job_id:
            return

        async for item in stream_translation_job(self.job_id, 0):
            if await self.request.is_disconnected():
                break
            for event in _genos_events_from_translation_event(item):
                yield event

    async def run(self) -> AsyncIterator[Dict[str, Any]]:
        try:
            result = await _start_document_translation(self.payload)
            async for event in self._emit_initial_result(result):
                yield event
        except Exception as exc:
            async for event in self.handle_error(exc):
                yield event


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/document-translation/translate")
async def document_translate(request: Request) -> JSONResponse:
    payload = await _read_json(request)
    payload = _attach_preview_url_config(request, payload)
    result = await _run_document_translation(payload)
    return JSONResponse(result)


@app.post("/api/document-translation/translate/start")
async def document_translate_start(request: Request) -> JSONResponse:
    payload = await _read_json(request)
    payload = _attach_preview_url_config(request, payload)
    result = await _start_document_translation(payload)
    return JSONResponse(result)


@app.post("/api/document-translation/translate/genos-stream")
async def document_translate_genos_stream(request: Request) -> StreamingResponse:
    payload = await _read_json(request)
    payload = _attach_preview_url_config(request, payload)
    service = DocumentTranslationGenosStreamService(payload, request)

    return StreamingResponse(
        _serialize_genos_events(service.run()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/document-translation/translate/revise")
async def document_translate_revise(request: Request) -> JSONResponse:
    payload = await _read_json(request)
    payload = _attach_preview_url_config(request, payload)
    result = await _revise_document_translation(payload)
    return JSONResponse(result)


@app.get("/api/document-translation/translate/events/{job_id}")
async def document_translate_events(job_id: str, request: Request) -> StreamingResponse:
    job = get_translation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="translation job을 찾을 수 없습니다.")

    last_event_id = request.headers.get("last-event-id", "0")
    try:
        last_id = int(last_event_id)
    except ValueError:
        last_id = 0

    async def _event_generator():
        async for item in stream_translation_job(job_id, last_id):
            payload = json.dumps(item.get("data", {}), ensure_ascii=False)
            yield f"id: {item.get('id', 0)}\nevent: {item.get('event', 'message')}\ndata: {payload}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/document-translation/preview-status/{job_id}")
async def document_preview_status(job_id: str) -> JSONResponse:
    job = get_preview_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="preview job을 찾을 수 없습니다.")
    return JSONResponse(job)


@app.post("/api/document-translation/realtime")
async def realtime_translate_endpoint(request: Request) -> JSONResponse:
    payload = await _read_json(request)
    result = await _run_realtime_translation(payload)
    return JSONResponse(result)


@app.api_route("/api/gateway/workflow/{workflow_id}/run/v2", methods=["GET", "POST"])
async def workflow_compatible_endpoint(workflow_id: int, request: Request) -> JSONResponse:
    payload = await _read_json(request)

    if workflow_id == 4304:
        payload = _attach_preview_url_config(request, payload)
        result = await _run_document_translation(payload)
    elif workflow_id == 4311:
        result = await _run_realtime_translation(payload)
    else:
        raise HTTPException(status_code=404, detail=f"지원하지 않는 workflow_id입니다: {workflow_id}")

    return JSONResponse({"data": result})
