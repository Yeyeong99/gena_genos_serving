"""Office 문서 파이프라인 orchestration 모듈."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from translation_pipeline.common.azure_uploader import upload_office_to_azure
from translation_pipeline.common.llm import get_last_llm_error
from translation_pipeline.common.language_detection import (
    build_same_language_skip_notice,
    has_text_requiring_translation,
)
from translation_pipeline.common.preview_jobs import (
    complete_preview_job,
    create_preview_job,
    fail_preview_job,
)
from translation_pipeline.common.translation_jobs import (
    complete_translation_job,
    create_translation_job,
    fail_translation_job,
    get_translation_job,
    publish_translation_event,
    update_translation_job,
)
from translation_pipeline.common.llm import Config

from .extract import load_office_document
from .preview import (
    append_preview_version,
    build_docx_html_preview_url,
    build_pptx_html_preview_url,
    build_xlsx_html_preview_url,
)
from .save import (
    _save_office_document,
    apply_edited_pairs_to_pairs,
    inject_edited_office_document,
    inject_translated_office_document,
    save_edited_office_document,
    save_translated_office_document,
)

_logger = logging.getLogger("uvicorn.error")
from .translate import translate_office_nodes
from .types import OfficePipelineDeps
from .units import build_injection_units, build_translation_units

_OFFICE_STREAM_LLM_CONCURRENCY = int(os.getenv("AI_TRANSLATION_OFFICE_STREAM_LLM_CONCURRENCY", "20"))
_PPTX_STREAM_LLM_CONCURRENCY = int(os.getenv("AI_TRANSLATION_PPTX_STREAM_LLM_CONCURRENCY", "4"))
_PPTX_STREAM_PREVIEW_FLUSH_SLIDES = int(os.getenv("AI_TRANSLATION_PPTX_STREAM_PREVIEW_FLUSH_SLIDES", "1"))
_DOCX_PROGRESSIVE_CHAR_THRESHOLD = int(os.getenv("AI_TRANSLATION_DOCX_PROGRESSIVE_CHAR_THRESHOLD", "18000"))
_DOCX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_DOCX_MAX_ITEMS_PER_BATCH", "12"))
_DOCX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_DOCX_MAX_CHARS_PER_BATCH", "6000"))


def _elapsed(start: float) -> str:
    """경과 시간을 사람이 읽기 쉬운 문자열로 변환한다.

    Args:
        start: 기준 시각.

    Returns:
        초 단위 문자열.
    """

    return f"{time.perf_counter() - start:.2f}s"


def _clone_nodes(nodes: list[dict]) -> list[dict]:
    return [dict(node) for node in nodes]


def _prepare_preview_nodes(nodes: list[dict]) -> list[dict]:
    preview_nodes = _clone_nodes(nodes)
    for node in preview_nodes:
        if isinstance(node.get("bbox"), list) and len(node["bbox"]) >= 4:
            node["original_bbox"] = list(node["bbox"][:4])
            node["translated_bbox"] = list(node["bbox"][:4])
        if node.get("page_num") is not None:
            node["original_page_num"] = node.get("page_num")
            node["translated_page_num"] = node.get("page_num")
    return preview_nodes


def _docx_total_chars(nodes: list[dict]) -> int:
    return sum(len(str(node.get("text", "")).strip()) for node in nodes)


def _assign_docx_translation_batches(nodes: list[dict]) -> int:
    """DOCX 실제 번역 배치와 사용자 수정 구간을 같은 번호로 맞춘다."""

    if not nodes:
        return 0

    for node in nodes:
        node.pop("page_num", None)
        node.pop("original_page_num", None)
        node.pop("translated_page_num", None)

    translation_units = build_translation_units(build_injection_units(nodes))
    batches: list[list[Any]] = []
    current: list[Any] = []
    current_chars = 0
    for unit in translation_units:
        if not str(unit.text).strip():
            continue
        estimated_chars = len(unit.text) + len(unit.context_text) + 80
        if current and (
            len(current) >= _DOCX_CONTEXT_MAX_ITEMS_PER_BATCH
            or current_chars + estimated_chars > _DOCX_CONTEXT_MAX_CHARS_PER_BATCH
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += estimated_chars
    if current:
        batches.append(current)

    for batch_index, batch in enumerate(batches, start=1):
        for unit in batch:
            for target in unit.targets:
                if 0 <= target.injection_unit_id < len(nodes):
                    node = nodes[target.injection_unit_id]
                    node["page_num"] = batch_index
                    node["original_page_num"] = batch_index
                    node["translated_page_num"] = batch_index

    for node in nodes:
        if node.get("page_num") is None:
            fallback_batch = max(1, len(batches))
            node["page_num"] = fallback_batch
            node["original_page_num"] = fallback_batch
            node["translated_page_num"] = fallback_batch

    return max(1, len(batches))


def _scope_preview_suffix(scope: str) -> str:
    """SSE scope 문자열을 preview 디렉터리 suffix로 변환한다."""

    return scope.replace(":", "-")


def _scope_slide_number(scope: str) -> int | None:
    if not scope.startswith("pptx:slide:"):
        return None
    try:
        return int(scope.split(":")[-1])
    except ValueError:
        return None


def _scope_page_number(scope: str) -> int | None:
    if not scope.startswith("docx:page:"):
        return None
    try:
        return int(scope.split(":")[-1])
    except ValueError:
        return None


def _scope_sheet_name(scope: str) -> str:
    if not scope.startswith("xlsx:sheet:"):
        return ""
    return scope.split(":", 2)[-1]


def _log_stream_event(event_name: str, payload: dict[str, Any]) -> None:
    progress_parts = []
    if payload.get("current_slide") is not None or payload.get("total_slides") is not None:
        progress_parts.append(f"slide={payload.get('current_slide')}/{payload.get('total_slides')}")
    if payload.get("current_page") is not None or payload.get("total_pages") is not None:
        progress_parts.append(f"page={payload.get('current_page')}/{payload.get('total_pages')}")
    if payload.get("current_sheet") is not None or payload.get("total_sheets") is not None:
        sheet_name = payload.get("current_sheet_name")
        sheet_label = f" sheet_name={sheet_name}" if sheet_name else ""
        progress_parts.append(f"sheet={payload.get('current_sheet')}/{payload.get('total_sheets')}{sheet_label}")
    if payload.get("translated_preview_html_url"):
        progress_parts.append("html=ready")
    suffix = f" ({', '.join(progress_parts)})" if progress_parts else ""
    _logger.info("[Office SSE] %s%s", event_name, suffix)


def _publish_translation_event(job_id: str, event_name: str, payload: dict[str, Any]) -> None:
    _log_stream_event(event_name, payload)
    publish_translation_event(job_id, event_name, payload)


def _build_html_preview_url(
    ext: str,
    file_path: str,
    preview_output_dir: str,
    preview_base_url: str,
    *,
    job_token: str | None = None,
    subdir: str | None = None,
    visible_slides: int | None = None,
    visible_sheets: int | None = None,
) -> str | None:
    """확장자에 맞는 HTML preview URL 생성 함수를 호출한다."""

    if ext == ".pptx":
        return build_pptx_html_preview_url(
            file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_token,
            subdir=subdir or _default_html_preview_subdir(ext),
            visible_slides=visible_slides,
        )
    if ext == ".docx":
        return build_docx_html_preview_url(
            file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_token,
            subdir=subdir or _default_html_preview_subdir(ext),
        )
    if ext == ".xlsx":
        return build_xlsx_html_preview_url(
            file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_token,
            subdir=subdir or _default_html_preview_subdir(ext),
            visible_sheets=visible_sheets,
        )
    return None


def _default_html_preview_subdir(ext: str) -> str:
    """문서 타입별 HTML preview 엔진 이름을 subdir에 반영한다."""

    # PPTX 는 PDF→SVG 인라인 HTML 로 전환 — 텍스트 선택·검색·블록 편집 토대 확보.
    return "libreoffice-svg-html" if ext == ".pptx" else "libreoffice-html"


def _translated_html_preview_subdir(ext: str, *, version: str | None = None) -> str:
    engine = "libreoffice-svg" if ext == ".pptx" else "libreoffice"
    base = f"translated-{engine}-html-live"
    return f"{base}/{version}" if version else base


def _translated_html_preview_job_subdir(ext: str) -> str:
    engine = "libreoffice-svg" if ext == ".pptx" else "libreoffice"
    return f"translated-{engine}-html-preview-job"


def _html_only_preview_payload() -> dict[str, Any]:
    """HTML iframe preview를 사용할 때 이미지/PDF preview 생성을 생략한다."""

    return {
        "original_preview_images": [],
        "translated_preview_images": [],
        "preview_page_sizes": [],
        "preview_render_mode": "html",
    }


def _build_pairs_from_nodes(nodes: list[dict]) -> list[dict]:
    pairs: list[dict] = []
    for node in nodes:
        original = str(node.get("text", ""))
        translated = str(node.get("translated_text", original))
        pairs.append(
            {
                "id": node.get("node_id"),
                "original": original,
                "translated": translated,
                "type": node.get("type", ""),
                "source": node.get("source", ""),
                "group": node.get("group", ""),
            }
        )
    return pairs


def _build_revision_context_payload(
    *,
    ext: str,
    office_obj: object,
    nodes: list[dict],
    target_lang: str,
    style_options: dict[str, Any] | None,
    preview_output_dir: str,
    preview_base_url: str,
) -> dict[str, Any]:
    return {
        "_revision_ext": ext,
        "_revision_office_obj": office_obj,
        "_revision_nodes": nodes,
        "_revision_target_lang": target_lang,
        "_revision_style_options": dict(style_options or {}),
        "_revision_preview_output_dir": preview_output_dir,
        "_revision_preview_base_url": preview_base_url,
    }


def _persist_docx_revision_source(
    *,
    office_obj: object,
    preview_output_dir: str,
    job_id: str,
) -> None:
    if not isinstance(office_obj, dict) or not preview_output_dir:
        return

    source_path = str(office_obj.get("file_path") or "")
    if not source_path or not os.path.exists(source_path):
        return

    revision_dir = os.path.join(preview_output_dir, job_id, "revision-source")
    os.makedirs(revision_dir, exist_ok=True)
    persistent_path = os.path.join(revision_dir, "source.docx")
    if os.path.abspath(source_path) != os.path.abspath(persistent_path):
        shutil.copy2(source_path, persistent_path)
    office_obj["file_path"] = persistent_path


def _llm_debug_payload() -> dict[str, Any]:
    return {
        "llm_model_name": Config.DEFAULT_TRANSLATION_MODEL,
        "llm_provider_sort": Config.LLM_API_PROVIDER_SORT or None,
    }


def _build_edited_style_by_id(edited_pairs: list[dict]) -> dict[int, dict[str, Any]]:
    edited_style_by_id: dict[int, dict[str, Any]] = {}
    for item in edited_pairs:
        if not isinstance(item, dict) or "id" not in item:
            continue
        try:
            node_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        style: dict[str, Any] = {}
        if item.get("font_size") is not None:
            try:
                style["font_size"] = float(item["font_size"])
            except (TypeError, ValueError):
                pass
        if item.get("line_break") is not None:
            style["line_break"] = bool(item.get("line_break"))
        if style:
            edited_style_by_id[node_id] = style
    return edited_style_by_id


def _apply_edited_styles_to_nodes(nodes: list[dict], edited_style_by_id: dict[int, dict[str, Any]]) -> None:
    if not edited_style_by_id:
        return
    for node in nodes:
        style = edited_style_by_id.get(int(node.get("node_id", -1)))
        if not style:
            continue
        if style.get("font_size") is not None:
            node["font_size"] = style["font_size"]
            node["edited_font_size"] = style["font_size"]
        if style.get("line_break") is not None:
            node["edited_line_break"] = style["line_break"]


async def start_office_pipeline_job(
    file_path: str,
    ext: str,
    target_lang: str,
    deps: OfficePipelineDeps,
    translator_mode: str | None = None,
    style_options: dict[str, Any] | None = None,
    preview_output_dir: str = "",
    preview_base_url: str = "",
    cleanup_path: str | None = None,
) -> dict:
    """원본 preview를 먼저 반환하고 번역/번역본 preview는 백그라운드 SSE job으로 수행한다."""

    stage_start = time.perf_counter()
    bundle = load_office_document(file_path, ext, deps)
    print(f"[Office start] 추출/초기 bbox: {_elapsed(stage_start)} (nodes={len(bundle.nodes)})")
    original_preview_html_url = None
    total_slides = 0
    total_sheets = 0
    total_pages = 0
    docx_total_chars = _docx_total_chars(bundle.nodes) if ext == ".docx" else 0
    docx_progressive_preview = (
        ext == ".docx" and docx_total_chars > _DOCX_PROGRESSIVE_CHAR_THRESHOLD
    )
    sheet_index_by_scope: dict[str, int] = {}
    if ext in {".pptx", ".docx", ".xlsx"}:
        total_slides = len(getattr(bundle.obj, "slides", []) or [])
        if ext == ".xlsx":
            sheet_names = list(getattr(bundle.obj, "sheetnames", []) or [])
            total_sheets = len(sheet_names)
            sheet_index_by_scope = {
                f"xlsx:sheet:{sheet_name}": index + 1
                for index, sheet_name in enumerate(sheet_names)
            }
        if ext == ".docx":
            total_pages = _assign_docx_translation_batches(bundle.nodes)
    if not bundle.nodes:
        initial_payload = {
            "text": "",
            "pairs": [],
            "translation_pairs": [],
            "document_blocks": [],
            "original_preview_images": [],
            "translated_preview_images": [],
            "preview_page_sizes": [],
            "preview_render_mode": "html",
            "translated_preview_status": "done",
            "translation_status": "done",
            "original_preview_html_url": original_preview_html_url,
            "translated_preview_html_url": original_preview_html_url,
            "total_pages": total_pages or None,
            "total_sheets": total_sheets or None,
            "debug_page_timings": [],
            **_llm_debug_payload(),
        }
        job_id = create_translation_job(initial_payload)
        return {
            **initial_payload,
            "job_id": job_id,
        }

    original_preview_nodes = _prepare_preview_nodes(bundle.nodes)
    original_preview_payload = _html_only_preview_payload()
    print("[Office start] 원본 preview 생성: HTML iframe route (background)")
    same_language_skip_notice = None
    if not has_text_requiring_translation((node.get("text", "") for node in bundle.nodes), target_lang):
        same_language_skip_notice = build_same_language_skip_notice(target_lang)
        print(f"[Office start] 같은 언어로 판단되어 번역 생략 예정: {same_language_skip_notice}")

    initial_payload = {
        "text": "",
        "pairs": [],
        "translation_pairs": [],
        "document_blocks": deps.build_document_layout(original_preview_nodes),
        "original_preview_images": original_preview_payload.get("original_preview_images", []),
        "translated_preview_images": [],
        "preview_page_sizes": original_preview_payload.get("preview_page_sizes", []),
        "preview_render_mode": original_preview_payload.get("preview_render_mode", "synthetic"),
        "translated_preview_status": "pending",
        "translation_status": "pending",
        "format": target_lang,
        "style_options": style_options or {},
        "original_preview_html_url": original_preview_html_url,
        "original_preview_status": "pending" if ext in {".pptx", ".docx", ".xlsx"} else "done",
        "total_slides": total_slides or None,
        "total_pages": total_pages or None,
        "total_sheets": total_sheets or None,
        "debug_page_timings": [],
        **_llm_debug_payload(),
    }
    job_id = create_translation_job(initial_payload)
    if ext == ".docx":
        _persist_docx_revision_source(
            office_obj=bundle.obj,
            preview_output_dir=preview_output_dir,
            job_id=job_id,
        )

    async def _run_job() -> None:
        nonlocal original_preview_html_url
        try:
            job_start = time.perf_counter()
            debug_page_timings: list[dict[str, Any]] = []

            async def _build_original_preview() -> str | None:
                nonlocal original_preview_html_url
                if ext not in {".pptx", ".docx", ".xlsx"}:
                    return None
                html_stage_start = time.perf_counter()
                original_preview_html_url = await asyncio.to_thread(
                    _build_html_preview_url,
                    ext,
                    file_path,
                    preview_output_dir,
                    preview_base_url,
                    job_token=job_id,
                    subdir=_default_html_preview_subdir(ext),
                )
                print(f"[Office start] 원본 HTML 변환: {_elapsed(html_stage_start)}")
                _publish_translation_event(
                    job_id,
                    "original_preview_ready",
                    {
                        "document_blocks": deps.build_document_layout(_prepare_preview_nodes(bundle.nodes)),
                        "original_preview_html_url": original_preview_html_url,
                        "original_preview_status": "done" if original_preview_html_url else "error",
                        "translated_preview_status": "pending",
                        "translation_status": "pending",
                        "total_slides": total_slides or None,
                        "total_pages": total_pages or None,
                        "total_sheets": total_sheets or None,
                        "event_phase": "original_preview_ready",
                        "debug_page_timings": debug_page_timings,
                        **_llm_debug_payload(),
                    },
                )
                return original_preview_html_url

            original_preview_task = (
                asyncio.create_task(_build_original_preview())
                if ext in {".pptx", ".docx", ".xlsx"}
                else None
            )

            if same_language_skip_notice:
                if original_preview_task is not None:
                    original_preview_html_url = await original_preview_task
                if preview_output_dir:
                    download_dir = os.path.join(preview_output_dir, job_id, "download")
                    os.makedirs(download_dir, exist_ok=True)
                    skipped_file_path = os.path.join(download_dir, f"translated-final{ext}")
                    _save_office_document(bundle.obj, ext, skipped_file_path, deps)
                    update_translation_job(
                        job_id,
                        {
                            "_translated_file_path": skipped_file_path,
                            "_translated_file_ext": ext,
                            **_build_revision_context_payload(
                                ext=ext,
                                office_obj=bundle.obj,
                                nodes=bundle.nodes,
                                target_lang=target_lang,
                                style_options=style_options,
                                preview_output_dir=preview_output_dir,
                                preview_base_url=preview_base_url,
                            ),
                        },
                    )
                skipped_payload = {
                    "text": "\n".join(
                        str(node.get("text", ""))
                        for node in bundle.nodes
                        if str(node.get("text", "")).strip()
                    ),
                    "pairs": deps.build_translation_pairs(bundle.nodes, {}),
                    "translation_pairs": deps.build_translation_pairs(bundle.nodes, {}),
                    "document_blocks": deps.build_document_layout(original_preview_nodes),
                    "original_preview_images": original_preview_payload.get("original_preview_images", []),
                    "translated_preview_images": [],
                    "preview_page_sizes": original_preview_payload.get("preview_page_sizes", []),
                    "preview_render_mode": original_preview_payload.get("preview_render_mode", "synthetic"),
                    "original_preview_status": "done" if original_preview_html_url else "error",
                    "translated_preview_status": "done",
                    "translation_status": "done",
                    "translation_notice": same_language_skip_notice,
                    "translation_skipped_reason": "same_language",
                    "original_preview_html_url": original_preview_html_url,
                    "translated_preview_html_url": original_preview_html_url,
                    "total_slides": total_slides or None,
                    "total_pages": total_pages or None,
                    "total_sheets": total_sheets or None,
                    "event_phase": "completed",
                    "debug_page_timings": debug_page_timings,
                    **_llm_debug_payload(),
                }
                complete_translation_job(job_id, skipped_payload)
                return

            _publish_translation_event(
                job_id,
                "translation_started",
                {
                    "document_blocks": deps.build_document_layout(_prepare_preview_nodes(bundle.nodes)),
                    "original_preview_html_url": original_preview_html_url,
                    "original_preview_status": (
                        "done"
                        if original_preview_html_url
                        else "pending"
                        if original_preview_task is not None
                        else "error"
                    ),
                    "translation_status": "translating",
                    "translated_preview_status": "pending",
                    "total_slides": total_slides or None,
                    "total_pages": total_pages or None,
                    "total_sheets": total_sheets or None,
                    "docx_total_chars": docx_total_chars or None,
                    "docx_progressive_preview": docx_progressive_preview or None,
                    "event_phase": "translation_started",
                    "debug_page_timings": debug_page_timings,
                    **_llm_debug_payload(),
                },
            )
            # PPTX still streams slide-by-slide, but each slide can contain many
            # independent text boxes. Keep slide order while allowing bounded
            # intra-slide batch parallelism to avoid large-request timeouts.
            sem = asyncio.Semaphore(
                _PPTX_STREAM_LLM_CONCURRENCY if ext == ".pptx" else _OFFICE_STREAM_LLM_CONCURRENCY
            )
            async with aiohttp.ClientSession() as session:
                working_nodes = _clone_nodes(bundle.nodes)
                preview_nodes = _prepare_preview_nodes(working_nodes)
                cumulative_text_by_node_id: dict[int, str] = {}
                pptx_last_preview_slide = 0
                preview_tmpdir_ctx = tempfile.TemporaryDirectory(prefix="office-preview-stream-")
                preview_tmpdir = preview_tmpdir_ctx.__enter__()
                if ext == ".pptx" and preview_output_dir:
                    working_preview_dir = os.path.join(preview_output_dir, job_id, "working")
                    os.makedirs(working_preview_dir, exist_ok=True)
                    translated_stream_preview_path = os.path.join(
                        working_preview_dir,
                        f"translated-working{ext}",
                    )
                elif ext == ".xlsx" and preview_output_dir:
                    working_preview_dir = os.path.join(preview_output_dir, job_id, "working")
                    os.makedirs(working_preview_dir, exist_ok=True)
                    translated_stream_preview_path = os.path.join(
                        working_preview_dir,
                        f"translated-working{ext}",
                    )
                else:
                    translated_stream_preview_path = os.path.join(preview_tmpdir, f"translated-preview{ext}")
                try:
                    async def _emit_scope_started(scope: str) -> None:
                        current_slide = _scope_slide_number(scope)
                        current_page = _scope_page_number(scope)
                        current_sheet = sheet_index_by_scope.get(scope)
                        current_sheet_name = _scope_sheet_name(scope)
                        if scope.startswith("pptx:slide:"):
                            event_name = "slide_translation_started"
                        elif scope.startswith("docx:page:"):
                            event_name = "page_translation_started"
                        elif scope.startswith("xlsx:sheet:"):
                            event_name = "sheet_translation_started"
                        else:
                            event_name = "scope_translation_started"

                        _publish_translation_event(
                            job_id,
                            event_name,
                            {
                                "translation_status": "translating",
                                "translated_preview_status": "pending",
                                "current_scope": scope,
                                "current_slide": current_slide,
                                "current_page": current_page,
                                "current_sheet": current_sheet,
                                "current_sheet_name": current_sheet_name or None,
                                "total_slides": total_slides or None,
                                "total_pages": total_pages or None,
                                "total_sheets": total_sheets or None,
                                "event_phase": event_name,
                                "debug_page_timings": debug_page_timings,
                                **_llm_debug_payload(),
                            },
                        )

                    async def _emit_scope(scope: str, resolved_injections: list[Any]) -> None:
                        nonlocal pptx_last_preview_slide
                        current_slide = _scope_slide_number(scope)
                        current_page = _scope_page_number(scope)
                        current_sheet = sheet_index_by_scope.get(scope)
                        current_sheet_name = _scope_sheet_name(scope)
                        resolved_text_by_node_id = {
                            item.node_id: item.translated_text
                            for item in resolved_injections
                        }
                        cumulative_text_by_node_id.update(resolved_text_by_node_id)
                        deps.apply_node_translations(
                            working_nodes,
                            edited_text_by_id=cumulative_text_by_node_id,
                        )
                        deps.apply_node_translations(
                            preview_nodes,
                            edited_text_by_id=cumulative_text_by_node_id,
                        )
                        current_blocks = deps.build_document_layout(preview_nodes)
                        if scope.startswith("pptx:slide:"):
                            event_name = "slide_translated"
                        elif scope.startswith("docx:page:"):
                            event_name = "page_translated"
                        elif scope.startswith("xlsx:sheet:"):
                            event_name = "sheet_translated"
                        else:
                            event_name = "blocks_translated"
                        _publish_translation_event(
                            job_id,
                            event_name,
                            {
                                "document_blocks": current_blocks,
                                "translation_status": "translating",
                                "translated_preview_status": "pending",
                                "current_scope": scope,
                                "current_slide": current_slide,
                                "current_page": current_page,
                                "current_sheet": current_sheet,
                                "current_sheet_name": current_sheet_name or None,
                                "total_slides": total_slides or None,
                                "total_pages": total_pages or None,
                                "total_sheets": total_sheets or None,
                                "event_phase": event_name,
                                "debug_page_timings": debug_page_timings,
                                **_llm_debug_payload(),
                            },
                        )
                        if ext == ".pptx" and scope.startswith("pptx:slide:"):
                            if current_slide is None:
                                return
                            flush_interval = max(1, _PPTX_STREAM_PREVIEW_FLUSH_SLIDES)
                            should_flush_pptx_preview = (
                                current_slide == 1
                                or current_slide >= (total_slides or current_slide)
                                or current_slide - pptx_last_preview_slide >= flush_interval
                            )
                            if not should_flush_pptx_preview:
                                return
                            pptx_last_preview_slide = current_slide
                            preview_version = f"{_scope_preview_suffix(scope)}-{int(time.time() * 1000)}"
                            inject_edited_office_document(
                                ext,
                                bundle.obj,
                                working_nodes,
                                {},
                                deps,
                            )
                            _publish_translation_event(
                                job_id,
                                "slide_injected",
                                {
                                    "document_blocks": current_blocks,
                                    "translation_status": "translating",
                                    "translated_preview_status": "pending",
                                        "current_scope": scope,
                                        "current_slide": current_slide,
                                        "total_slides": total_slides or None,
                                        "total_pages": total_pages or None,
                                        "total_sheets": total_sheets or None,
                                        "event_phase": "slide_injected",
                                        "debug_page_timings": debug_page_timings,
                                        **_llm_debug_payload(),
                                    },
                                )
                            _save_office_document(
                                bundle.obj,
                                ext,
                                translated_stream_preview_path,
                                deps,
                            )
                            update_translation_job(
                                job_id,
                                {
                                    "_translated_file_path": translated_stream_preview_path,
                                    "_translated_file_ext": ext,
                                    **_build_revision_context_payload(
                                        ext=ext,
                                        office_obj=bundle.obj,
                                        nodes=working_nodes,
                                        target_lang=target_lang,
                                        style_options=style_options,
                                        preview_output_dir=preview_output_dir,
                                        preview_base_url=preview_base_url,
                                    ),
                                },
                            )
                            html_stage_start = time.perf_counter()
                            translated_preview_html_url = build_pptx_html_preview_url(
                                translated_stream_preview_path,
                                preview_output_dir,
                                preview_base_url,
                                job_token=job_id,
                                subdir=_translated_html_preview_subdir(ext, version=preview_version),
                                visible_slides=current_slide,
                            )
                            if translated_preview_html_url:
                                html_ready_elapsed_ms = int(max(0.0, time.perf_counter() - job_start) * 1000)
                                html_render_ms = int(max(0.0, time.perf_counter() - html_stage_start) * 1000)
                                debug_page_timings.append(
                                    {
                                        "kind": "slide",
                                        "index": current_slide,
                                        "label": f"{current_slide} 슬라이드",
                                        "scope": scope,
                                        "html_ready_elapsed_ms": html_ready_elapsed_ms,
                                        "html_render_ms": html_render_ms,
                                    }
                                )
                                _publish_translation_event(
                                    job_id,
                                    "slide_html_ready",
                                    {
                                        "document_blocks": current_blocks,
                                        "translation_status": "translating",
                                        "translated_preview_status": "pending",
                                        "translated_preview_html_url": append_preview_version(
                                            translated_preview_html_url,
                                            preview_version,
                                        ),
                                        "current_scope": scope,
                                        "current_slide": current_slide,
                                        "total_slides": total_slides or None,
                                        "total_pages": total_pages or None,
                                        "total_sheets": total_sheets or None,
                                        "event_phase": "slide_html_ready",
                                        "debug_page_timings": debug_page_timings,
                                        **_llm_debug_payload(),
                                    },
                                )
                        elif ext == ".docx" and scope.startswith("docx:page:") and preview_output_dir:
                            if current_page is None:
                                return
                            preview_version = f"{_scope_preview_suffix(scope)}-{int(time.time() * 1000)}"
                            inject_edited_office_document(
                                ext,
                                bundle.obj,
                                working_nodes,
                                {},
                                deps,
                            )
                            _publish_translation_event(
                                job_id,
                                "page_injected",
                                {
                                    "document_blocks": current_blocks,
                                    "translation_status": "translating",
                                    "translated_preview_status": "pending",
                                    "current_scope": scope,
                                    "current_page": current_page,
                                    "total_pages": total_pages or None,
                                    "event_phase": "page_injected",
                                    "debug_page_timings": debug_page_timings,
                                    **_llm_debug_payload(),
                                },
                            )
                            _save_office_document(
                                bundle.obj,
                                ext,
                                translated_stream_preview_path,
                                deps,
                            )
                            update_translation_job(
                                job_id,
                                {
                                    "_translated_file_path": translated_stream_preview_path,
                                    "_translated_file_ext": ext,
                                    **_build_revision_context_payload(
                                        ext=ext,
                                        office_obj=bundle.obj,
                                        nodes=working_nodes,
                                        target_lang=target_lang,
                                        style_options=style_options,
                                        preview_output_dir=preview_output_dir,
                                        preview_base_url=preview_base_url,
                                    ),
                                },
                            )
                            html_stage_start = time.perf_counter()
                            translated_preview_html_url = _build_html_preview_url(
                                ext,
                                translated_stream_preview_path,
                                preview_output_dir,
                                preview_base_url,
                                job_token=job_id,
                                subdir=_translated_html_preview_subdir(ext, version=preview_version),
                            )
                            if translated_preview_html_url:
                                html_ready_elapsed_ms = int(max(0.0, time.perf_counter() - job_start) * 1000)
                                html_render_ms = int(max(0.0, time.perf_counter() - html_stage_start) * 1000)
                                debug_page_timings.append(
                                    {
                                        "kind": "page",
                                        "index": current_page,
                                        "label": f"{current_page} 페이지",
                                        "scope": scope,
                                        "html_ready_elapsed_ms": html_ready_elapsed_ms,
                                        "html_render_ms": html_render_ms,
                                    }
                                )
                                _publish_translation_event(
                                    job_id,
                                    "page_html_ready",
                                    {
                                        "document_blocks": current_blocks,
                                        "translation_status": "translating",
                                        "translated_preview_status": "pending",
                                        "translated_preview_html_url": append_preview_version(
                                            translated_preview_html_url,
                                            preview_version,
                                        ),
                                        "current_scope": scope,
                                        "current_page": current_page,
                                        "total_pages": total_pages or None,
                                        "event_phase": "page_html_ready",
                                        "debug_page_timings": debug_page_timings,
                                        **_llm_debug_payload(),
                                    },
                                )
                        elif ext == ".xlsx" and scope.startswith("xlsx:sheet:") and preview_output_dir:
                            preview_version = f"{_scope_preview_suffix(scope)}-{int(time.time() * 1000)}"
                            inject_edited_office_document(
                                ext,
                                bundle.obj,
                                working_nodes,
                                {},
                                deps,
                            )
                            _publish_translation_event(
                                job_id,
                                "sheet_injected",
                                {
                                    "document_blocks": current_blocks,
                                    "translation_status": "translating",
                                    "translated_preview_status": "pending",
                                    "current_scope": scope,
                                    "current_sheet": current_sheet,
                                    "current_sheet_name": current_sheet_name or None,
                                    "total_sheets": total_sheets or None,
                                    "event_phase": "sheet_injected",
                                    "debug_page_timings": debug_page_timings,
                                    **_llm_debug_payload(),
                                },
                            )
                            _save_office_document(
                                bundle.obj,
                                ext,
                                translated_stream_preview_path,
                                deps,
                            )
                            update_translation_job(
                                job_id,
                                {
                                    "_translated_file_path": translated_stream_preview_path,
                                    "_translated_file_ext": ext,
                                    **_build_revision_context_payload(
                                        ext=ext,
                                        office_obj=bundle.obj,
                                        nodes=working_nodes,
                                        target_lang=target_lang,
                                        style_options=style_options,
                                        preview_output_dir=preview_output_dir,
                                        preview_base_url=preview_base_url,
                                    ),
                                },
                            )
                            html_stage_start = time.perf_counter()
                            translated_preview_html_url = _build_html_preview_url(
                                ext,
                                translated_stream_preview_path,
                                preview_output_dir,
                                preview_base_url,
                                job_token=job_id,
                                subdir=_translated_html_preview_subdir(ext, version=preview_version),
                                visible_sheets=current_sheet,
                            )
                            if translated_preview_html_url:
                                html_ready_elapsed_ms = int(max(0.0, time.perf_counter() - job_start) * 1000)
                                html_render_ms = int(max(0.0, time.perf_counter() - html_stage_start) * 1000)
                                debug_page_timings.append(
                                    {
                                        "kind": "sheet",
                                        "index": current_sheet,
                                        "label": current_sheet_name or f"{current_sheet} 시트",
                                        "scope": scope,
                                        "html_ready_elapsed_ms": html_ready_elapsed_ms,
                                        "html_render_ms": html_render_ms,
                                    }
                                )
                                _publish_translation_event(
                                    job_id,
                                    "sheet_html_ready",
                                    {
                                        "document_blocks": current_blocks,
                                        "translation_status": "translating",
                                        "translated_preview_status": "pending",
                                        "translated_preview_html_url": append_preview_version(
                                            translated_preview_html_url,
                                            preview_version,
                                        ),
                                        "current_scope": scope,
                                        "current_sheet": current_sheet,
                                        "current_sheet_name": current_sheet_name or None,
                                        "total_sheets": total_sheets or None,
                                        "event_phase": "sheet_html_ready",
                                        "debug_page_timings": debug_page_timings,
                                        **_llm_debug_payload(),
                                    },
                                )

                    stage_start = time.perf_counter()
                    artifacts = await translate_office_nodes(
                        sem,
                        session,
                        working_nodes,
                        target_lang,
                        deps,
                        translator_mode=translator_mode,
                        style_options=style_options,
                        on_scope_started=(
                            _emit_scope_started
                            if ext != ".docx" or docx_progressive_preview
                            else None
                        ),
                        on_scope_translated=(
                            _emit_scope
                            if ext != ".docx" or docx_progressive_preview
                            else None
                        ),
                    )
                    print(f"[Office start] LLM 번역 완료: {_elapsed(stage_start)}")

                    if artifacts.translation_error:
                        print(f"[Office start] LLM 번역 실패 감지: {artifacts.translation_error}")
                        fail_translation_job(
                            job_id,
                            artifacts.translation_error,
                            {
                                "pairs": artifacts.pairs,
                                "translation_pairs": artifacts.pairs,
                                "text": artifacts.text,
                                "document_blocks": deps.build_document_layout(preview_nodes),
                                "translation_status": "error",
                                "translated_preview_status": "error",
                                "translated_preview_html_url": original_preview_html_url,
                                "total_slides": total_slides or None,
                                "total_pages": total_pages or None,
                                "total_sheets": total_sheets or None,
                                "event_phase": "job_error",
                                "debug_page_timings": debug_page_timings,
                                **_llm_debug_payload(),
                            },
                        )
                        return

                    stage_start = time.perf_counter()
                    resolved_text_by_node_id = {
                        item.node_id: item.translated_text
                        for item in artifacts.resolved_injections
                    }
                    deps.apply_node_translations(
                        working_nodes,
                        edited_text_by_id=resolved_text_by_node_id,
                    )
                    deps.apply_node_translations(
                        preview_nodes,
                        edited_text_by_id=resolved_text_by_node_id,
                    )
                    _publish_translation_event(
                        job_id,
                        "blocks_translated",
                        {
                            "pairs": artifacts.pairs,
                            "translation_pairs": artifacts.pairs,
                            "text": artifacts.text,
                            "document_blocks": deps.build_document_layout(preview_nodes),
                            "translation_status": "translated",
                            "translated_preview_status": "pending",
                            "translation_error": artifacts.translation_error or None,
                            "total_slides": total_slides or None,
                            "total_pages": total_pages or None,
                            "total_sheets": total_sheets or None,
                            "event_phase": "blocks_translated",
                            "debug_page_timings": debug_page_timings,
                            **_llm_debug_payload(),
                        },
                    )
                    print(f"[Office start] 번역 이벤트/블록 준비: {_elapsed(stage_start)}")

                    should_build_translated_preview = (
                        ext in {".pptx", ".docx", ".xlsx"}
                        or (
                            original_preview_payload.get("preview_render_mode") == "actual"
                            and any(
                                pair.get("translated", "") != pair.get("original", "")
                                for pair in artifacts.pairs
                            )
                        )
                    )

                    if not should_build_translated_preview:
                        complete_translation_job(
                            job_id,
                            {
                                "pairs": artifacts.pairs,
                                "translation_pairs": artifacts.pairs,
                                "text": artifacts.text,
                                "document_blocks": deps.build_document_layout(preview_nodes),
                                "translated_preview_images": original_preview_payload.get("original_preview_images", []),
                                "preview_page_sizes": original_preview_payload.get("preview_page_sizes", []),
                                "preview_render_mode": original_preview_payload.get("preview_render_mode", "synthetic"),
                                "translated_preview_status": "done",
                                "translation_status": "done",
                                "translation_error": artifacts.translation_error or None,
                                "total_slides": total_slides or None,
                                "total_pages": total_pages or None,
                                "total_sheets": total_sheets or None,
                                "event_phase": "completed",
                                "debug_page_timings": debug_page_timings,
                                **_llm_debug_payload(),
                            },
                        )
                        return

                    stage_start = time.perf_counter()
                    inject_translated_office_document(
                        ext,
                        bundle.obj,
                        working_nodes,
                        artifacts.trans_map,
                        deps,
                    )
                    print(f"[Office start] 번역 주입: {_elapsed(stage_start)}")
                    if preview_output_dir:
                        download_dir = os.path.join(preview_output_dir, job_id, "download")
                        os.makedirs(download_dir, exist_ok=True)
                        translated_preview_path = os.path.join(download_dir, f"translated-final{ext}")
                        # 정적 서빙은 제거됐다 — 다운로드 URL 은 Azure SAS URL 만 사용한다.
                        translated_file_url = None
                        stage_start = time.perf_counter()
                        _save_office_document(bundle.obj, ext, translated_preview_path, deps)
                        print(f"[Office start] 번역 파일 저장: {_elapsed(stage_start)}")
                        stage_start = time.perf_counter()
                        translated_file_url = upload_office_to_azure(
                            Path(translated_preview_path),
                            job_token=job_id,
                            download_filename=f"translated-final{ext}",
                        )
                        print(f"[Office start] 번역 파일 Azure 업로드: {_elapsed(stage_start)} -> {bool(translated_file_url)}")
                        stage_start = time.perf_counter()
                        translated_preview_html_url = _build_html_preview_url(
                            ext,
                            translated_preview_path,
                            preview_output_dir,
                            preview_base_url,
                            job_token=job_id,
                            subdir=_translated_html_preview_subdir(ext),
                        )
                        print(f"[Office start] 번역 HTML 변환: {_elapsed(stage_start)}")
                        translated_preview_payload = _html_only_preview_payload()
                    else:
                        translated_file_url = None
                        with tempfile.TemporaryDirectory(prefix="office-preview-stream-") as tmpdir:
                            translated_preview_path = os.path.join(tmpdir, f"translated-preview{ext}")
                            stage_start = time.perf_counter()
                            _save_office_document(bundle.obj, ext, translated_preview_path, deps)
                            print(f"[Office start] 번역 파일 저장: {_elapsed(stage_start)}")
                            stage_start = time.perf_counter()
                            translated_file_url = upload_office_to_azure(
                                Path(translated_preview_path),
                                job_token=job_id,
                                download_filename=f"translated-final{ext}",
                            )
                            print(f"[Office start] 번역 파일 Azure 업로드: {_elapsed(stage_start)} -> {bool(translated_file_url)}")
                            stage_start = time.perf_counter()
                            translated_preview_html_url = _build_html_preview_url(
                                ext,
                                translated_preview_path,
                                preview_output_dir,
                                preview_base_url,
                                job_token=job_id,
                                subdir=_translated_html_preview_subdir(ext),
                            )
                            print(f"[Office start] 번역 HTML 변환: {_elapsed(stage_start)}")
                            translated_preview_payload = _html_only_preview_payload()

                    update_translation_job(
                        job_id,
                        {
                            "_translated_file_path": translated_preview_path,
                            "_translated_file_ext": ext,
                            **_build_revision_context_payload(
                                ext=ext,
                                office_obj=bundle.obj,
                                nodes=working_nodes,
                                target_lang=target_lang,
                                style_options=style_options,
                                preview_output_dir=preview_output_dir,
                                preview_base_url=preview_base_url,
                            ),
                        },
                    )

                    for source_node, translated_node in zip(preview_nodes, preview_nodes):
                        if isinstance(translated_node.get("bbox"), list) and len(translated_node["bbox"]) >= 4:
                            source_node["translated_bbox"] = list(translated_node["bbox"][:4])
                        if translated_node.get("page_num") is not None:
                            source_node["translated_page_num"] = translated_node.get("page_num")

                    final_translated_preview_html_url = translated_preview_html_url or original_preview_html_url
                    complete_translation_job(
                        job_id,
                        {
                            "pairs": artifacts.pairs,
                            "translation_pairs": artifacts.pairs,
                            "text": artifacts.text,
                            "document_blocks": deps.build_document_layout(preview_nodes),
                            "translated_preview_images": translated_preview_payload.get("original_preview_images", []),
                            "preview_page_sizes": translated_preview_payload.get("preview_page_sizes", []),
                            "preview_render_mode": translated_preview_payload.get("preview_render_mode", "synthetic"),
                            "translated_preview_html_url": append_preview_version(
                                final_translated_preview_html_url,
                                f"final-{int(time.time() * 1000)}",
                            ),
                            "translated_file_url": translated_file_url,
                            "translated_preview_status": "done",
                            "translation_status": "done",
                            "translation_error": artifacts.translation_error or None,
                            "current_slide": total_slides or None,
                            "total_slides": total_slides or None,
                            "current_page": total_pages or None,
                            "total_pages": total_pages or None,
                            "current_sheet": total_sheets or None,
                            "total_sheets": total_sheets or None,
                            "event_phase": "completed",
                            "debug_page_timings": debug_page_timings,
                            **_llm_debug_payload(),
                        },
                    )
                    print(f"[Office start] SSE job 전체: {_elapsed(job_start)}")
                finally:
                    preview_tmpdir_ctx.__exit__(None, None, None)
        except Exception as exc:
            print(f"[Office start] SSE job 실패: {exc}")
            fail_translation_job(
                job_id,
                str(exc),
                {
                    "translation_status": "error",
                    "translated_preview_status": "done" if original_preview_html_url else "error",
                    "translated_preview_html_url": original_preview_html_url,
                    "total_slides": total_slides or None,
                    "total_pages": total_pages or None,
                    "total_sheets": total_sheets or None,
                    "event_phase": "job_error",
                    **_llm_debug_payload(),
                },
            )
        finally:
            if cleanup_path and os.path.exists(cleanup_path):
                try:
                    os.remove(cleanup_path)
                except Exception:
                    pass

    asyncio.create_task(_run_job())
    return {
        **initial_payload,
        "job_id": job_id,
    }


def _xlsx_sheet_names_from_nodes(nodes: list[dict]) -> list[str]:
    sheet_names: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        sheet_name = str(node.get("sheet_name") or "").strip()
        if not sheet_name or sheet_name in seen:
            continue
        sheet_names.append(sheet_name)
        seen.add(sheet_name)
    return sheet_names


def _normalize_revision_scope(scope: dict[str, Any] | None) -> tuple[str, int | str | None]:
    if not isinstance(scope, dict) or not scope:
        return "document", None

    scope_type = str(scope.get("type") or scope.get("kind") or "").strip().lower()
    raw_index = scope.get("index")
    if raw_index is None:
        raw_index = scope.get("slide")
    if scope_type in {"slide", "pptx:slide"}:
        try:
            return "slide", int(raw_index)
        except (TypeError, ValueError):
            raise ValueError("수정할 슬라이드 번호가 올바르지 않습니다.")
    if scope_type in {"sheet", "xlsx:sheet"}:
        try:
            return "sheet", int(raw_index)
        except (TypeError, ValueError):
            raw_sheet = scope.get("sheet") or scope.get("name") or scope.get("label")
            if raw_sheet:
                sheet_name = re.sub(r"\s+\(\d+\)\s*$", "", str(raw_sheet)).strip()
                return "sheet", sheet_name
            raise ValueError("수정할 시트 정보가 올바르지 않습니다.")
    if scope_type in {"batch", "section", "page", "docx:page"}:
        try:
            return "batch", int(raw_index)
        except (TypeError, ValueError):
            raise ValueError("수정할 구간 정보가 올바르지 않습니다.")
    return scope_type or "document", None


async def revise_office_translation_job(
    job_id: str,
    scope: dict[str, Any] | None,
    target_lang: str,
    deps: OfficePipelineDeps,
    *,
    translator_mode: str | None = None,
    style_options: dict[str, Any] | None = None,
    instruction: str = "",
    preview_output_dir: str = "",
    preview_base_url: str = "",
) -> dict[str, Any]:
    """완료된 Office translation job을 기준으로 수정 번역을 수행한다.

    현재는 PPTX 슬라이드, XLSX 시트, DOCX 구간 단위와 전체 재번역을 지원한다.
    """

    job = get_translation_job(job_id)
    if not job:
        raise ValueError("translation job을 찾을 수 없습니다.")
    payload = job.get("payload", {})
    ext = str(payload.get("_revision_ext") or payload.get("_translated_file_ext") or "")
    if ext not in {".pptx", ".xlsx", ".docx"}:
        raise ValueError("현재 수정 번역은 PPTX/XLSX/DOCX 문서를 지원합니다.")

    office_obj = payload.get("_revision_office_obj")
    revision_nodes = payload.get("_revision_nodes")
    if office_obj is None or not isinstance(revision_nodes, list) or not revision_nodes:
        raise ValueError("수정에 필요한 번역 job context가 없습니다. 문서를 다시 번역해 주세요.")

    scope_type, scope_index = _normalize_revision_scope(scope)
    if ext == ".pptx":
        allowed_scope_types = {"document", "slide"}
    elif ext == ".xlsx":
        allowed_scope_types = {"document", "sheet"}
    else:
        allowed_scope_types = {"document", "batch"}
    if scope_type not in allowed_scope_types:
        raise ValueError("현재 문서 종류에서 지원하지 않는 수정 단위입니다.")

    if scope_type == "slide":
        target_nodes = [
            node
            for node in revision_nodes
            if int(node.get("slide_index") or 0) == scope_index
        ]
        if not target_nodes:
            raise ValueError(f"{scope_index}번 슬라이드에서 수정할 텍스트를 찾지 못했습니다.")
    elif scope_type == "sheet":
        sheet_names = _xlsx_sheet_names_from_nodes(revision_nodes)
        if isinstance(scope_index, int):
            if scope_index < 1 or scope_index > len(sheet_names):
                raise ValueError(f"{scope_index}번 시트를 찾지 못했습니다.")
            sheet_name = sheet_names[scope_index - 1]
        else:
            sheet_name = str(scope_index or "").strip()
        target_nodes = [
            node
            for node in revision_nodes
            if str(node.get("sheet_name") or "") == sheet_name
        ]
        if not target_nodes:
            raise ValueError(f"{sheet_name or '선택한'} 시트에서 수정할 텍스트를 찾지 못했습니다.")
    elif scope_type == "batch":
        target_nodes = [
            node
            for node in revision_nodes
            if int(node.get("page_num") or 0) == scope_index
        ]
        if not target_nodes:
            raise ValueError(f"{scope_index}번 구간에서 수정할 텍스트를 찾지 못했습니다.")
    else:
        target_nodes = list(revision_nodes)

    previous_by_node_id = {
        int(node.get("node_id")): str(node.get("translated_text", node.get("text", "")))
        for node in target_nodes
        if node.get("node_id") is not None
    }
    target_lang = target_lang or str(payload.get("_revision_target_lang") or payload.get("format") or "")
    effective_style_options = {
        **dict(payload.get("_revision_style_options") or {}),
        **dict(style_options or {}),
        "_previous_translation_by_node_id": previous_by_node_id,
    }
    if instruction.strip():
        effective_style_options["_revision_instruction"] = instruction.strip()

    sem = asyncio.Semaphore(
        _PPTX_STREAM_LLM_CONCURRENCY if ext == ".pptx" else _OFFICE_STREAM_LLM_CONCURRENCY
    )
    async with aiohttp.ClientSession() as session:
        artifacts = await translate_office_nodes(
            sem,
            session,
            target_nodes,
            target_lang,
            deps,
            translator_mode=translator_mode,
            style_options=effective_style_options,
        )

    revised_text_by_node_id = {
        item.node_id: item.translated_text
        for item in artifacts.resolved_injections
    }
    deps.apply_node_translations(
        revision_nodes,
        edited_text_by_id=revised_text_by_node_id,
    )
    revised_nodes_for_injection = [
        node
        for node in revision_nodes
        if int(node.get("node_id", -1)) in revised_text_by_node_id
    ]
    inject_edited_office_document(
        ext,
        office_obj,
        revised_nodes_for_injection,
        {},
        deps,
    )

    preview_output_dir = preview_output_dir or str(payload.get("_revision_preview_output_dir") or "")
    preview_base_url = preview_base_url or str(payload.get("_revision_preview_base_url") or "")
    translated_preview_html_url = payload.get("translated_preview_html_url")
    translated_file_path = payload.get("_translated_file_path")
    if preview_output_dir:
        download_dir = os.path.join(preview_output_dir, job_id, "download")
        os.makedirs(download_dir, exist_ok=True)
        translated_file_path = os.path.join(download_dir, f"translated-revised{ext}")
        _save_office_document(office_obj, ext, translated_file_path, deps)
        version = f"revision-{int(time.time() * 1000)}"
        translated_preview_html_url = _build_html_preview_url(
            ext,
            translated_file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_id,
            subdir=_translated_html_preview_subdir(ext, version=version),
        )
        translated_preview_html_url = append_preview_version(translated_preview_html_url, version)

    pairs = _build_pairs_from_nodes(revision_nodes)
    text = "\n".join(pair["translated"] for pair in pairs if str(pair.get("translated", "")).strip())
    public_style_options = {
        key: value
        for key, value in effective_style_options.items()
        if not str(key).startswith("_")
    }
    result = {
        "job_id": job_id,
        "format": target_lang,
        "style_options": public_style_options,
        "pairs": pairs,
        "translation_pairs": pairs,
        "text": text,
        "document_blocks": deps.build_document_layout(revision_nodes),
        "translated_preview_html_url": translated_preview_html_url,
        "translated_preview_status": "done",
        "translation_status": "done",
        "translation_error": artifacts.translation_error or None,
        "current_scope": (
            f"pptx:slide:{scope_index}"
            if scope_type == "slide"
            else f"xlsx:sheet:{scope_index}"
            if scope_type == "sheet"
            else f"docx:page:{scope_index}"
            if scope_type == "batch"
            else None
        ),
        "current_slide": scope_index if scope_type == "slide" else payload.get("total_slides"),
        "current_page": scope_index if scope_type == "batch" else payload.get("total_pages"),
        "current_sheet": (
            scope_index
            if scope_type == "sheet" and isinstance(scope_index, int)
            else None
        ),
        "current_sheet_name": (
            str(scope_index)
            if scope_type == "sheet" and not isinstance(scope_index, int)
            else None
        ),
        "total_slides": payload.get("total_slides"),
        "total_pages": payload.get("total_pages"),
        "total_sheets": payload.get("total_sheets"),
        "event_phase": "completed",
        "revision_status": "done",
        "revision_scope": scope or None,
        **_llm_debug_payload(),
    }
    update_translation_job(
        job_id,
        {
            **result,
            "_translated_file_path": translated_file_path,
            "_translated_file_ext": ext,
            **_build_revision_context_payload(
                ext=ext,
                office_obj=office_obj,
                nodes=revision_nodes,
                target_lang=target_lang,
                style_options=public_style_options,
                preview_output_dir=preview_output_dir,
                preview_base_url=preview_base_url,
            ),
        },
    )
    return result


async def _build_translated_preview_job(
    job_id: str,
    ext: str,
    source_nodes: list[dict],
    translated_preview_nodes: list[dict],
    office_obj: object,
    preview_output_dir: str,
    preview_base_url: str,
    deps: OfficePipelineDeps,
    original_preview_html_url: str | None = None,
) -> None:
    """번역본 actual preview를 백그라운드에서 생성한다."""

    try:
        with tempfile.TemporaryDirectory(prefix="office-preview-translated-") as tmpdir:
            translated_preview_path = os.path.join(tmpdir, f"translated-preview{ext}")
            _save_office_document(office_obj, ext, translated_preview_path, deps)
            translated_preview_html_url = _build_html_preview_url(
                ext,
                translated_preview_path,
                preview_output_dir,
                preview_base_url,
                job_token=job_id,
                subdir=_translated_html_preview_job_subdir(ext),
            )
            translated_preview_payload = _html_only_preview_payload()

        updated_nodes = [dict(node) for node in source_nodes]
        for source_node, translated_node in zip(updated_nodes, translated_preview_nodes):
            if isinstance(translated_node.get("bbox"), list) and len(translated_node["bbox"]) >= 4:
                source_node["translated_bbox"] = list(translated_node["bbox"][:4])
            if translated_node.get("page_num") is not None:
                source_node["translated_page_num"] = translated_node.get("page_num")

        final_translated_preview_html_url = translated_preview_html_url or original_preview_html_url
        complete_preview_job(
            job_id,
            {
                "translated_preview_job_id": job_id,
                "translated_preview_status": "done",
                "translated_preview_images": translated_preview_payload.get("original_preview_images", []),
                "translated_preview_html_url": append_preview_version(
                    final_translated_preview_html_url,
                    f"preview-job-{int(time.time() * 1000)}",
                ),
                "preview_page_sizes": translated_preview_payload.get("preview_page_sizes", []),
                "preview_render_mode": translated_preview_payload.get("preview_render_mode", "synthetic"),
                "document_blocks": deps.build_document_layout(updated_nodes),
            },
        )
    except Exception as exc:
        print(f"[Office 파이프라인] 비동기 번역 preview 생성 실패: {exc}")
        if original_preview_html_url:
            complete_preview_job(
                job_id,
                {
                    "translated_preview_job_id": job_id,
                    "translated_preview_status": "done",
                    "translated_preview_html_url": append_preview_version(
                        original_preview_html_url,
                        f"preview-job-fallback-{int(time.time() * 1000)}",
                    ),
                    "preview_render_mode": "html",
                    "document_blocks": deps.build_document_layout(source_nodes),
                    "translation_error": f"번역본 미리보기 생성에 실패해 원본 미리보기를 표시합니다: {exc}",
                },
            )
            return
        fail_preview_job(job_id, str(exc))


async def run_office_pipeline(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    file_path: str,
    ext: str,
    target_lang: str,
    deps: OfficePipelineDeps,
    translator_mode: str | None = None,
    style_options: dict[str, Any] | None = None,
    is_return_file: bool = False,
    callback_url: str = "",
    preview_output_dir: str = "",
    preview_base_url: str = "",
) -> dict:
    """Office 문서 번역 파이프라인을 단계별로 실행한다.

    Args:
        sem: LLM 동시성 제어 세마포어.
        session: 번역 API 호출 세션.
        file_path: 입력 문서 경로.
        ext: 파일 확장자.
        target_lang: 대상 언어.
        deps: 단계별 의존성 묶음.
        translator_mode: 번역기 모드(`llm`/`mock`/`noop`).
        is_return_file: 번역 파일 저장 여부.
        callback_url: 진행 상태 전송용 callback URL.
        preview_output_dir: preview 파일 저장 디렉터리.
        preview_base_url: preview 파일 접근 base URL.

    Returns:
        프런트 응답에 바로 사용할 결과 딕셔너리.
    """

    pipeline_start = time.perf_counter()
    print(
        f"[Office 파이프라인] 시작: {file_path} ({ext}), "
        f"is_return_file={is_return_file}, translator_mode={translator_mode or 'env/default'}"
    )

    await deps.emit_event("EXTRACT_START", callback_url)
    stage_start = time.perf_counter()
    bundle = load_office_document(file_path, ext, deps)
    if ext == ".docx":
        _assign_docx_translation_batches(bundle.nodes)
    print(f"[Pipeline timing] 추출/초기 bbox: {_elapsed(stage_start)} (nodes={len(bundle.nodes)})")

    if not bundle.nodes:
        print("[Office 파이프라인] 번역할 텍스트 없음")
        await deps.emit_event("SAVE_DONE", callback_url)
        result = {
            "pairs": [],
            "input_text": "",
            "text": "",
            "document_blocks": [],
            "original_preview_images": [],
            "translated_preview_images": [],
            "preview_page_sizes": [],
            "preview_render_mode": "html",
            "original_preview_html_url": _build_html_preview_url(
                ext,
                file_path,
                preview_output_dir,
                preview_base_url,
            ),
        }
        if is_return_file:
            result.update(save_translated_office_document(file_path, ext, bundle.obj, deps))
        return result

    unique_texts = list({node["text"] for node in bundle.nodes})
    await deps.emit_event("EXTRACT_DONE", callback_url, nodes=len(bundle.nodes), unique=len(unique_texts))

    if not has_text_requiring_translation((node.get("text", "") for node in bundle.nodes), target_lang):
        print("[Office 파이프라인] 같은 언어로 판단되어 번역 생략")
        original_preview_html_url = _build_html_preview_url(
            ext,
            file_path,
            preview_output_dir,
            preview_base_url,
        )
        original_preview_payload = _html_only_preview_payload()
        pairs = deps.build_translation_pairs(bundle.nodes, {})
        for node in bundle.nodes:
            if isinstance(node.get("bbox"), list) and len(node["bbox"]) >= 4:
                node["original_bbox"] = list(node["bbox"][:4])
                node["translated_bbox"] = list(node["bbox"][:4])
            if node.get("page_num") is not None:
                node["original_page_num"] = node.get("page_num")
                node["translated_page_num"] = node.get("page_num")
        result = {
            "pairs": pairs,
            "translation_pairs": pairs,
            "text": "\n".join(pair["translated"] for pair in pairs if pair.get("translated", "").strip()),
            "document_blocks": deps.build_document_layout(bundle.nodes),
            "original_preview_images": original_preview_payload.get("original_preview_images", []),
            "translated_preview_images": [],
            "original_preview_html_url": original_preview_html_url,
            "translated_preview_html_url": original_preview_html_url,
            "translated_preview_status": "done",
            "translation_status": "done",
            "translation_notice": build_same_language_skip_notice(target_lang),
            "translation_skipped_reason": "same_language",
            "preview_page_sizes": original_preview_payload.get("preview_page_sizes", []),
            "preview_render_mode": original_preview_payload.get("preview_render_mode", "synthetic"),
        }
        if is_return_file:
            result.update(save_translated_office_document(file_path, ext, bundle.obj, deps))
        await deps.emit_event("SAVE_DONE", callback_url)
        return result

    await deps.emit_event("TRANSLATE_START", callback_url, unique=len(unique_texts))
    stage_start = time.perf_counter()
    artifacts = await translate_office_nodes(
        sem,
        session,
        bundle.nodes,
        target_lang,
        deps,
        translator_mode=translator_mode,
        style_options=style_options,
    )
    print(f"[Pipeline timing] LLM 배치 번역: {_elapsed(stage_start)} (unique={len(unique_texts)})")
    await deps.emit_event("TRANSLATE_DONE", callback_url)

    resolved_text_by_node_id = {
        item.node_id: item.translated_text
        for item in artifacts.resolved_injections
    }
    if resolved_text_by_node_id:
        deps.apply_node_translations(
            bundle.nodes,
            edited_text_by_id=resolved_text_by_node_id,
        )
    else:
        deps.apply_node_translations(bundle.nodes, trans_map=artifacts.trans_map)
    stage_start = time.perf_counter()
    original_preview_html_url = _build_html_preview_url(
        ext,
        file_path,
        preview_output_dir,
        preview_base_url,
    )
    original_preview_payload = _html_only_preview_payload()
    for node in bundle.nodes:
        if isinstance(node.get("bbox"), list) and len(node["bbox"]) >= 4:
            node["original_bbox"] = list(node["bbox"][:4])
        if node.get("page_num") is not None:
            node["original_page_num"] = node.get("page_num")

    for node in bundle.nodes:
        if isinstance(node.get("original_bbox"), list):
            node["translated_bbox"] = list(node["original_bbox"])
        if node.get("original_page_num") is not None:
            node["translated_page_num"] = node.get("original_page_num")

    translated_preview_job_id: str | None = None
    translated_preview_status: str | None = None
    should_build_translated_preview = (
        not is_return_file
        and (
            ext in {".pptx", ".docx", ".xlsx"}
            or (
                original_preview_payload.get("preview_render_mode") == "actual"
                and any(
                    pair.get("translated", "") != pair.get("original", "")
                    for pair in artifacts.pairs
                )
            )
        )
    )
    if should_build_translated_preview:
        translated_preview_job_id = create_preview_job()
        translated_preview_status = "pending"
        translated_preview_nodes = [dict(node) for node in bundle.nodes]
        for translated_node in translated_preview_nodes:
            translated_node["text"] = str(
                translated_node.get("translated_text", translated_node.get("text", ""))
            )
        inject_translated_office_document(
            ext,
            bundle.obj,
            bundle.nodes,
            artifacts.trans_map,
            deps,
        )
        asyncio.create_task(
            _build_translated_preview_job(
                translated_preview_job_id,
                ext,
                [dict(node) for node in bundle.nodes],
                translated_preview_nodes,
                bundle.obj,
                preview_output_dir,
                preview_base_url,
                deps,
                original_preview_html_url,
            )
        )

    print(
        f"[Pipeline timing] HTML preview URL 생성: {_elapsed(stage_start)}"
    )

    preview_payload = {
        "original_preview_images": original_preview_payload.get("original_preview_images", []),
        "translated_preview_images": original_preview_payload.get("original_preview_images", []),
        "original_preview_html_url": original_preview_html_url,
        "translated_preview_html_url": None,
        "preview_page_sizes": original_preview_payload.get("preview_page_sizes", []),
        "preview_render_mode": original_preview_payload.get("preview_render_mode", "synthetic"),
    }

    result = {
        "pairs": artifacts.pairs,
        "text": artifacts.text,
        "document_blocks": deps.build_document_layout(bundle.nodes),
        **preview_payload,
    }
    if translated_preview_job_id:
        result["translated_preview_job_id"] = translated_preview_job_id
        result["translated_preview_status"] = translated_preview_status
    translation_error = artifacts.translation_error
    if (
        not translation_error
        and artifacts.pairs
        and all(pair.get("translated", "") == pair.get("original", "") for pair in artifacts.pairs)
    ):
        translation_error = get_last_llm_error() or "번역 API 호출에 실패해 원문이 그대로 표시되고 있습니다."
    if translation_error:
        result["translation_error"] = translation_error

    if is_return_file:
        await deps.emit_event("INJECT_START", callback_url)
        stage_start = time.perf_counter()
        inject_translated_office_document(
            ext,
            bundle.obj,
            bundle.nodes,
            artifacts.trans_map,
            deps,
        )
        await deps.emit_event("INJECT_DONE", callback_url)
        print(f"[Pipeline timing] 번역 주입: {_elapsed(stage_start)}")

        await deps.emit_event("SAVE_START", callback_url)
        save_start = time.perf_counter()
        download_payload = save_translated_office_document(file_path, ext, bundle.obj, deps)
        await deps.emit_event("SAVE_DONE", callback_url)
        print(f"[Office 파이프라인] 저장 완료: {download_payload['file_path']}")
        print(f"[Pipeline timing] 파일 저장/다운로드 payload: {_elapsed(save_start)}")
        result.update(download_payload)
    else:
        print("[Office 파이프라인] is_return_file=False, 인젝션/저장 스킵")

    print(f"[Pipeline timing] Office 전체: {_elapsed(pipeline_start)}")
    return result


async def save_edited_office_file(
    file_path: str,
    ext: str,
    edited_pairs: list[dict],
    deps: OfficePipelineDeps,
    callback_url: str = "",
    preview_output_dir: str = "",
    preview_base_url: str = "",
    include_preview: bool = True,
) -> dict:
    """사용자 수정본을 반영한 Office 문서를 저장한다.

    Args:
        file_path: 입력 문서 경로.
        ext: 파일 확장자.
        edited_pairs: 사용자 수정 결과 목록.
        deps: 단계별 의존성 묶음.
        callback_url: 진행 상태 전송용 callback URL.
        preview_output_dir: preview 파일 저장 디렉터리.
        preview_base_url: preview 파일 접근 base URL.
        include_preview: True면 수정본 preview도 재생성한다. 다운로드 전용 빠른 경로에서는 False.

    Returns:
        수정본 preview 및 다운로드 정보를 포함한 결과 딕셔너리.
    """

    print(f"[수정본 저장] Office 문서 저장 시작: {file_path} ({ext})")
    await deps.emit_event("EXTRACT_START", callback_url)
    bundle = load_office_document(file_path, ext, deps)
    await deps.emit_event("EXTRACT_DONE", callback_url, nodes=len(bundle.nodes))

    edited_text_by_id = deps.build_edited_text_by_id(edited_pairs)
    edited_style_by_id = _build_edited_style_by_id(edited_pairs)
    pairs = deps.build_translation_pairs(bundle.nodes, {})

    await deps.emit_event("INJECT_START", callback_url)
    deps.apply_node_translations(bundle.nodes, edited_text_by_id=edited_text_by_id)
    _apply_edited_styles_to_nodes(bundle.nodes, edited_style_by_id)
    inject_edited_office_document(
        ext,
        bundle.obj,
        bundle.nodes,
        edited_text_by_id,
        deps,
    )
    await deps.emit_event("INJECT_DONE", callback_url)

    await deps.emit_event("SAVE_START", callback_url)
    download_payload = save_edited_office_document(file_path, ext, bundle.obj, deps)
    await deps.emit_event("SAVE_DONE", callback_url)

    updated_pairs = apply_edited_pairs_to_pairs(pairs, edited_text_by_id)
    preview_payload = (
        {
            **_html_only_preview_payload(),
            "original_preview_html_url": _build_html_preview_url(
                ext,
                download_payload["file_path"],
                preview_output_dir,
                preview_base_url,
            ),
        }
        if include_preview
        else {}
    )

    return {
        "pairs": updated_pairs,
        "document_blocks": deps.build_document_layout(bundle.nodes),
        **preview_payload,
        **download_payload,
    }
