"""Evaluation-only Office translation pipeline."""

from __future__ import annotations

import time
import asyncio
from typing import Any

import aiohttp

from translation_pipeline.common.language_detection import (
    build_same_language_skip_notice,
    has_text_requiring_translation,
)
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.term_memory_store import memory_summary

from .extract import load_office_document
from .scopes import assign_docx_translation_batches
from .translate import translate_office_nodes
from .types import OfficePipelineDeps


def build_translation_evaluation_units(artifacts: Any) -> list[dict[str, Any]]:
    """평가 파이프라인 입력용 번역 단위 목록을 만든다."""

    injection_by_id = {
        injection.injection_unit_id: injection
        for injection in artifacts.injection_units
    }
    units: list[dict[str, Any]] = []
    for unit in artifacts.translation_units:
        targets: list[dict[str, Any]] = []
        for target in unit.targets:
            injection = injection_by_id.get(target.injection_unit_id)
            target_payload: dict[str, Any] = {
                "injection_unit_id": target.injection_unit_id,
                "fragment_index": target.fragment_index,
                "fragment_count": target.fragment_count,
            }
            if injection is not None:
                target_payload.update(
                    {
                        "node_id": injection.node_id,
                        "doc_format": injection.doc_format,
                        "source": injection.source,
                        "group": injection.group,
                        "type": injection.node_type,
                        "element_type": injection.element_type,
                        "table_index": injection.table_index,
                        "slide_index": injection.slide_index,
                        "sheet_name": injection.sheet_name or None,
                        "row": injection.row,
                        "col": injection.col,
                        "page_num": injection.page_num,
                        "is_header": injection.is_header,
                    }
                )
            targets.append({key: value for key, value in target_payload.items() if value is not None})

        units.append(
            {
                "id": unit.translation_unit_id,
                "original": unit.text,
                "translated": artifacts.translated_by_unit_id.get(
                    unit.translation_unit_id,
                    unit.text,
                ),
                "context_scope": unit.context_scope,
                "context": unit.context_text,
                "element_type": unit.element_type,
                "targets": targets,
            }
        )
    return units


async def run_office_evaluation_pipeline(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    file_path: str,
    ext: str,
    target_lang: str,
    deps: OfficePipelineDeps,
    translator_mode: str | None = None,
    style_options: dict[str, Any] | None = None,
) -> dict:
    """번역 평가용으로 Office 번역 단위(id/원문/번역)를 반환한다.

    Preview 생성, 파일 주입, 저장은 수행하지 않는다.
    """

    pipeline_start = time.perf_counter()
    bundle = load_office_document(file_path, ext, deps)
    if ext == ".docx":
        assign_docx_translation_batches(bundle.nodes)
    log_info(
        f"[Office evaluation] 시작: {file_path} ({ext}), nodes={len(bundle.nodes)}, "
        f"translator_mode={translator_mode or 'env/default'}"
    )

    if not bundle.nodes:
        return {
            "test_mode": "translation_evaluation",
            "translation_status": "done",
            "file_type": ext.lstrip("."),
            "format": target_lang,
            "translation_unit_count": 0,
            "translation_units": [],
            "text": "",
        }

    effective_translator_mode = translator_mode
    translation_notice = None
    translation_skipped_reason = None
    if not has_text_requiring_translation((node.get("text", "") for node in bundle.nodes), target_lang):
        effective_translator_mode = "noop"
        translation_notice = build_same_language_skip_notice(target_lang)
        translation_skipped_reason = "same_language"

    artifacts = await translate_office_nodes(
        sem,
        session,
        bundle.nodes,
        target_lang,
        deps,
        translator_mode=effective_translator_mode,
        style_options=style_options,
    )
    evaluation_units = build_translation_evaluation_units(artifacts)
    translation_error = artifacts.translation_error or ""
    payload = {
        "test_mode": "translation_evaluation",
        "translation_status": "error" if translation_error else "done",
        "translation_error": translation_error or None,
        "translation_notice": translation_notice,
        "translation_skipped_reason": translation_skipped_reason,
        "file_type": ext.lstrip("."),
        "format": target_lang,
        "node_count": len(bundle.nodes),
        "translation_unit_count": len(evaluation_units),
        "translation_units": evaluation_units,
        "temporary_glossary": artifacts.temporary_glossary,
        "temporary_glossary_summary": memory_summary(artifacts.temporary_glossary),
        "text": artifacts.text,
        "elapsed_ms": int(max(0.0, time.perf_counter() - pipeline_start) * 1000),
    }
    return {key: value for key, value in payload.items() if value is not None}
