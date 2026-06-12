"""Office 문서 파이프라인 orchestration 모듈."""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp

from translation_pipeline.common.azure_uploader import upload_office_to_azure
from translation_pipeline.common.language_detection import (
    build_same_language_skip_notice,
    has_text_requiring_translation,
)
from translation_pipeline.common.document_term_memory import document_term_memory_summary
from translation_pipeline.common.term_memory_store import (
    memory_summary,
    save_memory_to_local_file,
    save_memory_to_redis,
)
from translation_pipeline.common.translation_jobs import (
    complete_translation_job,
    create_translation_job,
    fail_translation_job,
    update_translation_job,
)

from .batch_pipeline import run_office_pipeline
from .extract import load_office_document
from .edited_save import save_edited_office_file
from .evaluation import run_office_evaluation_pipeline
from .preview_helpers import (
    build_html_preview_url as _build_html_preview_url,
    default_html_preview_subdir as _default_html_preview_subdir,
    html_only_preview_payload as _html_only_preview_payload,
    translated_html_preview_subdir as _translated_html_preview_subdir,
)
from .preview import append_preview_version
from .progress import (
    build_initial_overall_progress_payload as _build_initial_overall_progress_payload,
    build_progress_payload as _build_progress_payload,
    log_progress as _log_progress,
)
from .result_helpers import (
    build_revision_context_payload as _build_revision_context_payload,
    llm_debug_payload as _llm_debug_payload,
    persist_docx_revision_source as _persist_docx_revision_source,
)
from .revision import revise_office_translation_job
from .save import (
    _save_office_document,
    inject_edited_office_document,
    inject_translated_office_document,
)
from .scopes import (
    assign_docx_translation_batches as _assign_docx_translation_batches,
    docx_node_ids_for_scope as _docx_node_ids_for_scope,
    docx_total_chars as _docx_total_chars,
    node_text_chars_by_id as _node_text_chars_by_id,
    scope_page_number as _scope_page_number,
    scope_preview_suffix as _scope_preview_suffix,
    scope_sheet_name as _scope_sheet_name,
    scope_slide_number as _scope_slide_number,
)
from .stream_events import publish_office_translation_event as _publish_translation_event

from .translate import translate_office_nodes
from .types import OfficePipelineDeps

_OFFICE_STREAM_LLM_CONCURRENCY = int(os.getenv("AI_TRANSLATION_OFFICE_STREAM_LLM_CONCURRENCY", "20"))
_PPTX_STREAM_LLM_CONCURRENCY = int(os.getenv("AI_TRANSLATION_PPTX_STREAM_LLM_CONCURRENCY", "4"))
_PPTX_STREAM_PREVIEW_FLUSH_SLIDES = int(os.getenv("AI_TRANSLATION_PPTX_STREAM_PREVIEW_FLUSH_SLIDES", "1"))
_KEEP_TMP_ARTIFACTS = os.getenv("AI_TRANSLATION_KEEP_TMP_ARTIFACTS", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_TMP_ARTIFACT_ROOT = Path(os.getenv("AI_TRANSLATION_TMP_ARTIFACT_ROOT", "./tmp"))


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


def _node_id_set(nodes: list[dict]) -> set[int]:
    ids: set[int] = set()
    for node in nodes:
        try:
            ids.add(int(node.get("node_id")))
        except (TypeError, ValueError):
            continue
    return ids


def _node_ids_for_office_scope(nodes: list[dict], scope: str) -> set[int]:
    if scope.startswith("pptx:slide:"):
        current_slide = _scope_slide_number(scope)
        if current_slide is None:
            return set()
        return {
            node_id
            for node in nodes
            if (node_id := _safe_node_id(node)) is not None
            and int(node.get("slide_index") or 0) == current_slide
        }
    if scope.startswith("xlsx:sheet:"):
        current_sheet_name = _scope_sheet_name(scope)
        return {
            node_id
            for node in nodes
            if (node_id := _safe_node_id(node)) is not None
            and str(node.get("sheet_name") or "") == current_sheet_name
        }
    return set()


def _safe_node_id(node: dict) -> int | None:
    try:
        return int(node.get("node_id"))
    except (TypeError, ValueError):
        return None


def _cleanup_job_tmp_artifacts(job_id: str) -> None:
    """Delete local job-scoped debug artifacts under the translation tmp directory."""

    if _KEEP_TMP_ARTIFACTS:
        log_info(f"[Office tmp cleanup] skipped job_id={job_id} keep_tmp_artifacts=1")
        return
    if not job_id:
        return

    root = _TMP_ARTIFACT_ROOT
    if not root.is_absolute():
        root = Path.cwd() / root
    if not root.exists() or not root.is_dir():
        return

    removed = 0
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if job_id not in path.name:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            log_info(f"[Office tmp cleanup] failed path={path} error={exc}")

    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            pass

    if removed:
        log_info(f"[Office tmp cleanup] removed job_id={job_id} count={removed} root={root}")


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
    log_info(f"[Office start] 추출/초기 bbox: {_elapsed(stage_start)} (nodes={len(bundle.nodes)})")
    original_preview_html_url = None
    total_slides = 0
    total_sheets = 0
    total_pages = 0
    docx_total_chars = _docx_total_chars(bundle.nodes) if ext == ".docx" else 0
    docx_chars_by_node_id = _node_text_chars_by_id(bundle.nodes) if ext == ".docx" else {}
    pptx_total_text_units = len(_node_id_set(bundle.nodes)) if ext == ".pptx" else 0
    xlsx_total_cell_units = len(_node_id_set(bundle.nodes)) if ext == ".xlsx" else 0
    docx_progressive_preview = False
    should_stream_scope_events = ext in {".pptx", ".xlsx", ".docx"}
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
    log_info("[Office start] 원본 preview 생성: HTML iframe route (background)")
    same_language_skip_notice = None
    if not has_text_requiring_translation((node.get("text", "") for node in bundle.nodes), target_lang):
        same_language_skip_notice = build_same_language_skip_notice(target_lang)
        log_info(f"[Office start] 같은 언어로 판단되어 번역 생략 예정: {same_language_skip_notice}")

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
    update_translation_job(
        job_id,
        _build_revision_context_payload(
            ext=ext,
            office_obj=bundle.obj,
            nodes=bundle.nodes,
            target_lang=target_lang,
            style_options=style_options,
            preview_output_dir=preview_output_dir,
            preview_base_url=preview_base_url,
        ),
    )

    async def _run_job() -> None:
        nonlocal original_preview_html_url
        last_started_scope: str | None = None
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
                log_info(f"[Office start] 원본 HTML 변환: {_elapsed(html_stage_start)}")
                if ext == ".pptx":
                    progress = _build_progress_payload(
                        unit_kind="text_box",
                        completed_units=0,
                        total_units=pptx_total_text_units,
                        started_at=job_start,
                        current_label="번역 대기",
                    )
                elif ext == ".xlsx":
                    progress = _build_progress_payload(
                        unit_kind="cell",
                        completed_units=0,
                        total_units=xlsx_total_cell_units,
                        started_at=job_start,
                        current_label="번역 대기",
                    )
                else:
                    progress = _build_initial_overall_progress_payload(
                        ext=ext,
                        total_slides=total_slides,
                        total_sheets=total_sheets,
                        docx_total_chars=docx_total_chars,
                        started_at=job_start,
                    )
                _log_progress("original_preview_ready", progress)
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
                        **progress,
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
                docx_completed_node_ids: set[int] = set()
                completed_stream_node_ids: set[int] = set()
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
                        nonlocal last_started_scope
                        last_started_scope = scope
                        current_slide = _scope_slide_number(scope)
                        current_page = _scope_page_number(scope)
                        current_sheet = sheet_index_by_scope.get(scope)
                        current_sheet_name = _scope_sheet_name(scope)
                        if ext == ".docx":
                            completed_chars = sum(
                                docx_chars_by_node_id.get(node_id, 0)
                                for node_id in docx_completed_node_ids
                            )
                            progress = _build_progress_payload(
                                unit_kind="char",
                                completed_units=completed_chars,
                                total_units=docx_total_chars,
                                started_at=job_start,
                                current_label="문서 번역 중",
                            )
                            _log_progress("docx_scope_started", progress)
                            _publish_translation_event(
                                job_id,
                                "translation_progress",
                                {
                                    "translation_status": "translating",
                                    "translated_preview_status": "pending",
                                    "current_scope": scope,
                                    "current_page": current_page,
                                    "total_pages": total_pages or None,
                                    "total_slides": total_slides or None,
                                    "total_sheets": total_sheets or None,
                                    "event_phase": "translation_progress",
                                    "debug_page_timings": debug_page_timings,
                                    **progress,
                                    **_llm_debug_payload(),
                                },
                            )
                            return

                        if scope.startswith("pptx:slide:"):
                            event_name = "slide_translation_started"
                            progress = _build_progress_payload(
                                unit_kind="text_box",
                                completed_units=len(completed_stream_node_ids),
                                total_units=pptx_total_text_units,
                                started_at=job_start,
                                current_label=f"{current_slide} 슬라이드" if current_slide else "",
                            )
                        elif scope.startswith("docx:page:"):
                            event_name = "page_translation_started"
                            progress = {}
                        elif scope.startswith("xlsx:sheet:"):
                            event_name = "sheet_translation_started"
                            progress = _build_progress_payload(
                                unit_kind="cell",
                                completed_units=len(completed_stream_node_ids),
                                total_units=xlsx_total_cell_units,
                                started_at=job_start,
                                current_label=current_sheet_name or (
                                    f"{current_sheet} 시트" if current_sheet else ""
                                ),
                            )
                        else:
                            event_name = "scope_translation_started"
                            progress = {}

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
                                **progress,
                                **_llm_debug_payload(),
                            },
                        )
                        if progress:
                            _publish_translation_event(
                                job_id,
                                "translation_progress",
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
                                    "event_phase": "translation_progress",
                                    "debug_page_timings": debug_page_timings,
                                    **progress,
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
                        if ext == ".docx":
                            docx_completed_node_ids.update(
                                _docx_node_ids_for_scope(working_nodes, scope)
                            )
                            completed_chars = sum(
                                docx_chars_by_node_id.get(node_id, 0)
                                for node_id in docx_completed_node_ids
                            )
                            progress = _build_progress_payload(
                                unit_kind="char",
                                completed_units=completed_chars,
                                total_units=docx_total_chars,
                                started_at=job_start,
                                current_label="문서 번역 중",
                            )
                            _log_progress("docx_scope_translated", progress)
                            _publish_translation_event(
                                job_id,
                                "translation_progress",
                                {
                                    "translation_status": "translating",
                                    "translated_preview_status": "pending",
                                    "current_scope": scope,
                                    "current_page": current_page,
                                    "total_pages": total_pages or None,
                                    "total_slides": total_slides or None,
                                    "total_sheets": total_sheets or None,
                                    "event_phase": "translation_progress",
                                    "debug_page_timings": debug_page_timings,
                                    **progress,
                                    **_llm_debug_payload(),
                                },
                            )
                            return

                        if ext in {".pptx", ".xlsx"}:
                            completed_stream_node_ids.update(
                                _node_ids_for_office_scope(working_nodes, scope)
                            )

                        current_blocks = deps.build_document_layout(preview_nodes)
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
                                visible_slides=current_slide,
                            )
                            if translated_preview_html_url:
                                html_ready_elapsed_ms = int(max(0.0, time.perf_counter() - job_start) * 1000)
                                html_render_ms = int(max(0.0, time.perf_counter() - html_stage_start) * 1000)
                                progress = _build_progress_payload(
                                    unit_kind="text_box",
                                    completed_units=len(completed_stream_node_ids),
                                    total_units=pptx_total_text_units,
                                    started_at=job_start,
                                    current_label=f"{current_slide} 슬라이드",
                                )
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
                                    "translation_progress",
                                    {
                                        "translation_status": "translating",
                                        "translated_preview_status": "pending",
                                        "current_scope": scope,
                                        "current_slide": current_slide,
                                        "total_slides": total_slides or None,
                                        "total_pages": total_pages or None,
                                        "total_sheets": total_sheets or None,
                                        "event_phase": "translation_progress",
                                        "debug_page_timings": debug_page_timings,
                                        **progress,
                                        **_llm_debug_payload(),
                                    },
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
                                        **progress,
                                        **_llm_debug_payload(),
                                    },
                                )
                        elif (
                            ext == ".docx"
                            and docx_progressive_preview
                            and scope.startswith("docx:page:")
                            and preview_output_dir
                        ):
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
                                progress = _build_progress_payload(
                                    unit_kind="cell",
                                    completed_units=len(completed_stream_node_ids),
                                    total_units=xlsx_total_cell_units,
                                    started_at=job_start,
                                    current_label=current_sheet_name or (
                                        f"{current_sheet} 시트" if current_sheet else ""
                                    ),
                                )
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
                                    "translation_progress",
                                    {
                                        "translation_status": "translating",
                                        "translated_preview_status": "pending",
                                        "current_scope": scope,
                                        "current_sheet": current_sheet,
                                        "current_sheet_name": current_sheet_name or None,
                                        "total_sheets": total_sheets or None,
                                        "event_phase": "translation_progress",
                                        "debug_page_timings": debug_page_timings,
                                        **progress,
                                        **_llm_debug_payload(),
                                    },
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
                                        **progress,
                                        **_llm_debug_payload(),
                                    },
                                )

                    stage_start = time.perf_counter()

                    async def _store_temporary_glossary(memory: dict[str, Any]) -> None:
                        memory["job_id"] = job_id
                        dump_path = save_memory_to_local_file(job_id, memory)
                        redis_saved = await save_memory_to_redis(job_id, memory)
                        update_translation_job(
                            job_id,
                            {
                                "_temporary_glossary": memory,
                                "_temporary_glossary_summary": memory_summary(memory),
                                "_temporary_glossary_dump_path": dump_path or None,
                                "_temporary_glossary_redis_saved": redis_saved,
                            },
                        )
                        log_info(
                            "[Temporary Glossary] stored "
                            f"{memory_summary(memory)} redis_saved={redis_saved}"
                            f"{f' dump_path={dump_path}' if dump_path else ''}"
                        )

                    async def _store_pre_translation_analysis(analysis: dict[str, Any]) -> None:
                        profile = analysis.get("document_profile")
                        domain = (
                            profile.get("domain")
                            if isinstance(profile, dict)
                            else analysis.get("domain")
                        )
                        update_translation_job(
                            job_id,
                            {
                                "_pre_translation_analysis": analysis,
                                "_pre_translation_analysis_dump_path": analysis.get("_dump_path"),
                            },
                        )
                        log_info(
                            "[Pre-Translation Analysis] stored "
                            f"domain={domain} dump_path={analysis.get('_dump_path')}"
                        )

                    async def _store_document_term_memory(memory: dict[str, Any]) -> None:
                        update_translation_job(
                            job_id,
                            {
                                "_document_term_memory": memory,
                                "_document_term_memory_summary": document_term_memory_summary(memory),
                                "_document_term_memory_dump_path": memory.get("_dump_path"),
                                "_document_term_memory_resolver_dump_path": memory.get("_resolver_dump_path"),
                            },
                        )
                        log_info(
                            "[Document Term Memory] stored "
                            f"{document_term_memory_summary(memory)} "
                            f"dump_path={memory.get('_dump_path')} "
                            f"resolver_dump_path={memory.get('_resolver_dump_path')}"
                        )

                    translation_style_options = {
                        **(style_options or {}),
                        "_job_id": job_id,
                    }
                    translation_style_options.setdefault("_filename", Path(file_path).name)

                    artifacts = await translate_office_nodes(
                        sem,
                        session,
                        working_nodes,
                        target_lang,
                        deps,
                        translator_mode=translator_mode,
                        style_options=translation_style_options,
                        on_scope_started=(
                            _emit_scope_started
                            if should_stream_scope_events
                            else None
                        ),
                        on_scope_translated=(
                            _emit_scope
                            if should_stream_scope_events
                            else None
                        ),
                        on_temporary_glossary_update=_store_temporary_glossary,
                        on_pre_translation_analysis=_store_pre_translation_analysis,
                        on_document_term_memory_update=_store_document_term_memory,
                    )
                    log_info(f"[Office start] LLM 번역 완료: {_elapsed(stage_start)}")

                    async def _store_final_temporary_glossary(storage_phase: str) -> None:
                        if not artifacts.temporary_glossary:
                            return
                        artifacts.temporary_glossary["storage_phase"] = storage_phase
                        await _store_temporary_glossary(artifacts.temporary_glossary)

                    if artifacts.translation_error:
                        await _store_final_temporary_glossary("translation_error")
                        log_info(f"[Office start] LLM 번역 실패 감지: {artifacts.translation_error}")
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
                    log_info(f"[Office start] 번역 이벤트/블록 준비: {_elapsed(stage_start)}")

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
                        await _store_final_temporary_glossary("completed_no_html")
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
                                "original_preview_html_url": original_preview_html_url,
                                "original_preview_status": "done" if original_preview_html_url else "error",
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
                    log_info(f"[Office start] 번역 주입: {_elapsed(stage_start)}")
                    if preview_output_dir:
                        download_dir = os.path.join(preview_output_dir, job_id, "download")
                        os.makedirs(download_dir, exist_ok=True)
                        translated_preview_path = os.path.join(download_dir, f"translated-final{ext}")
                        # 정적 서빙은 제거됐다 — 다운로드 URL 은 Azure SAS URL 만 사용한다.
                        translated_file_url = None
                        stage_start = time.perf_counter()
                        _save_office_document(bundle.obj, ext, translated_preview_path, deps)
                        log_info(f"[Office start] 번역 파일 저장: {_elapsed(stage_start)}")
                        stage_start = time.perf_counter()
                        translated_file_url = upload_office_to_azure(
                            Path(translated_preview_path),
                            job_token=job_id,
                            download_filename=f"translated-final{ext}",
                        )
                        log_info(f"[Office start] 번역 파일 Azure 업로드: {_elapsed(stage_start)} -> {bool(translated_file_url)}")
                        stage_start = time.perf_counter()
                        translated_preview_html_url = _build_html_preview_url(
                            ext,
                            translated_preview_path,
                            preview_output_dir,
                            preview_base_url,
                            job_token=job_id,
                            subdir=_translated_html_preview_subdir(ext),
                        )
                        log_info(f"[Office start] 번역 HTML 변환: {_elapsed(stage_start)}")
                        translated_preview_payload = _html_only_preview_payload()
                    else:
                        translated_file_url = None
                        with tempfile.TemporaryDirectory(prefix="office-preview-stream-") as tmpdir:
                            translated_preview_path = os.path.join(tmpdir, f"translated-preview{ext}")
                            stage_start = time.perf_counter()
                            _save_office_document(bundle.obj, ext, translated_preview_path, deps)
                            log_info(f"[Office start] 번역 파일 저장: {_elapsed(stage_start)}")
                            stage_start = time.perf_counter()
                            translated_file_url = upload_office_to_azure(
                                Path(translated_preview_path),
                                job_token=job_id,
                                download_filename=f"translated-final{ext}",
                            )
                            log_info(f"[Office start] 번역 파일 Azure 업로드: {_elapsed(stage_start)} -> {bool(translated_file_url)}")
                            stage_start = time.perf_counter()
                            translated_preview_html_url = _build_html_preview_url(
                                ext,
                                translated_preview_path,
                                preview_output_dir,
                                preview_base_url,
                                job_token=job_id,
                                subdir=_translated_html_preview_subdir(ext),
                            )
                            log_info(f"[Office start] 번역 HTML 변환: {_elapsed(stage_start)}")
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
                    await _store_final_temporary_glossary("final_html_ready")
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
                            "original_preview_html_url": original_preview_html_url,
                            "original_preview_status": "done" if original_preview_html_url else "error",
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
                    log_info(f"[Office start] SSE job 전체: {_elapsed(job_start)}")
                finally:
                    preview_tmpdir_ctx.__exit__(None, None, None)
        except Exception as exc:
            log_info(f"[Office start] SSE job 실패: {exc}")
            failed_slide = _scope_slide_number(last_started_scope or "")
            failed_page = _scope_page_number(last_started_scope or "")
            failed_sheet = sheet_index_by_scope.get(last_started_scope or "")
            fail_translation_job(
                job_id,
                str(exc),
                {
                    "translation_status": "error",
                    "translated_preview_status": "error",
                    "current_scope": last_started_scope,
                    "current_slide": failed_slide,
                    "current_page": failed_page,
                    "current_sheet": failed_sheet,
                    "current_sheet_name": _scope_sheet_name(last_started_scope or "") or None,
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
            _cleanup_job_tmp_artifacts(job_id)

    asyncio.create_task(_run_job())
    return {
        **initial_payload,
        "job_id": job_id,
    }
