"""One-shot Office translation pipeline."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import aiohttp

from translation_pipeline.common.language_detection import (
    build_same_language_skip_notice,
    has_text_requiring_translation,
)
from translation_pipeline.common.llm import get_last_llm_error
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.preview_jobs import (
    complete_preview_job,
    create_preview_job,
    fail_preview_job,
)

from .extract import load_office_document
from .preview import append_preview_version
from .preview_helpers import (
    build_html_preview_url,
    html_only_preview_payload,
    translated_html_preview_job_subdir,
)
from .save import (
    _save_office_document,
    inject_translated_office_document,
    save_translated_office_document,
)
from .scopes import assign_docx_translation_batches
from .translate import translate_office_nodes
from .types import OfficePipelineDeps


def _elapsed(start: float) -> str:
    return f"{time.perf_counter() - start:.2f}s"


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
            translated_preview_html_url = build_html_preview_url(
                ext,
                translated_preview_path,
                preview_output_dir,
                preview_base_url,
                job_token=job_id,
                subdir=translated_html_preview_job_subdir(ext),
            )
            translated_preview_payload = html_only_preview_payload()

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
        log_info(f"[Office 파이프라인] 비동기 번역 preview 생성 실패: {exc}")
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
    """Office 문서 번역 파이프라인을 단계별로 실행한다."""

    pipeline_start = time.perf_counter()
    log_info(
        f"[Office 파이프라인] 시작: {file_path} ({ext}), "
        f"is_return_file={is_return_file}, translator_mode={translator_mode or 'env/default'}"
    )

    await deps.emit_event("EXTRACT_START", callback_url)
    stage_start = time.perf_counter()
    bundle = load_office_document(file_path, ext, deps)
    if ext == ".docx":
        assign_docx_translation_batches(bundle.nodes)
    log_info(f"[Pipeline timing] 추출/초기 bbox: {_elapsed(stage_start)} (nodes={len(bundle.nodes)})")

    if not bundle.nodes:
        log_info("[Office 파이프라인] 번역할 텍스트 없음")
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
            "original_preview_html_url": build_html_preview_url(
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
        log_info("[Office 파이프라인] 같은 언어로 판단되어 번역 생략")
        original_preview_html_url = build_html_preview_url(
            ext,
            file_path,
            preview_output_dir,
            preview_base_url,
        )
        original_preview_payload = html_only_preview_payload()
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
    log_info(f"[Pipeline timing] LLM 배치 번역: {_elapsed(stage_start)} (unique={len(unique_texts)})")
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
    original_preview_html_url = build_html_preview_url(
        ext,
        file_path,
        preview_output_dir,
        preview_base_url,
    )
    original_preview_payload = html_only_preview_payload()
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

    log_info(
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
        log_info(f"[Pipeline timing] 번역 주입: {_elapsed(stage_start)}")

        await deps.emit_event("SAVE_START", callback_url)
        save_start = time.perf_counter()
        download_payload = save_translated_office_document(file_path, ext, bundle.obj, deps)
        await deps.emit_event("SAVE_DONE", callback_url)
        log_info(f"[Office 파이프라인] 저장 완료: {download_payload['file_path']}")
        log_info(f"[Pipeline timing] 파일 저장/다운로드 payload: {_elapsed(save_start)}")
        result.update(download_payload)
    else:
        log_info("[Office 파이프라인] is_return_file=False, 인젝션/저장 스킵")

    log_info(f"[Pipeline timing] Office 전체: {_elapsed(pipeline_start)}")
    return result
