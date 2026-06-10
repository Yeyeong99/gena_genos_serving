"""Revision pipeline for completed Office translation jobs."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from translation_pipeline.common.azure_uploader import upload_office_to_azure
from translation_pipeline.common.translation_jobs import get_translation_job, update_translation_job

from .preview import append_preview_version
from .preview_helpers import build_html_preview_url, translated_html_preview_subdir
from .result_helpers import (
    build_pairs_from_nodes,
    build_revision_context_payload,
    llm_debug_payload,
)
from .save import _save_office_document, inject_edited_office_document
from .translate import translate_office_nodes
from .types import OfficePipelineDeps

_OFFICE_STREAM_LLM_CONCURRENCY = int(os.getenv("AI_TRANSLATION_OFFICE_STREAM_LLM_CONCURRENCY", "20"))
_PPTX_STREAM_LLM_CONCURRENCY = int(os.getenv("AI_TRANSLATION_PPTX_STREAM_LLM_CONCURRENCY", "4"))


def xlsx_sheet_names_from_nodes(nodes: list[dict]) -> list[str]:
    sheet_names: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        sheet_name = str(node.get("sheet_name") or "").strip()
        if not sheet_name or sheet_name in seen:
            continue
        sheet_names.append(sheet_name)
        seen.add(sheet_name)
    return sheet_names


def normalize_revision_scope(scope: dict[str, Any] | str | None) -> tuple[str, int | str | None]:
    if isinstance(scope, str):
        raw = scope.strip()
        if raw.startswith("pptx:slide:"):
            try:
                return "slide", int(raw.rsplit(":", 1)[-1])
            except (TypeError, ValueError):
                raise ValueError("수정할 슬라이드 번호가 올바르지 않습니다.")
        if raw.startswith("xlsx:sheet:"):
            sheet = raw.split(":", 2)[-1].strip()
            if sheet:
                return "sheet", sheet
            raise ValueError("수정할 시트 정보가 올바르지 않습니다.")
        if raw.startswith("docx:page:"):
            try:
                return "batch", int(raw.rsplit(":", 1)[-1])
            except (TypeError, ValueError):
                raise ValueError("수정할 구간 정보가 올바르지 않습니다.")
        raise ValueError(f"지원하지 않는 scope 형식입니다: {raw}")

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


def _scope_key(scope_type: str, scope_index: int | str | None) -> tuple[str, str]:
    return scope_type, str(scope_index or "")


def _current_scope(scope_type: str, scope_index: int | str | None) -> str | None:
    if scope_type == "slide":
        return f"pptx:slide:{scope_index}"
    if scope_type == "sheet":
        return f"xlsx:sheet:{scope_index}"
    if scope_type == "batch":
        return f"docx:page:{scope_index}"
    return None


def _normalize_revision_scope_list(
    scope: dict[str, Any] | str | None,
    scopes: list[Any] | None,
) -> list[tuple[str, int | str | None]]:
    raw_scopes: list[Any] = list(scopes or [])
    if not raw_scopes and scope:
        raw_scopes = [scope]
    if not raw_scopes:
        return [("document", None)]

    normalized: list[tuple[str, int | str | None]] = []
    seen: set[tuple[str, str]] = set()
    for raw_scope in raw_scopes:
        scope_type, scope_index = normalize_revision_scope(raw_scope)
        key = _scope_key(scope_type, scope_index)
        if key in seen:
            continue
        seen.add(key)
        normalized.append((scope_type, scope_index))
    return normalized or [("document", None)]


def _nodes_for_revision_scope(
    ext: str,
    revision_nodes: list[dict],
    scope_type: str,
    scope_index: int | str | None,
) -> list[dict]:
    if scope_type == "document":
        return list(revision_nodes)
    if scope_type == "slide":
        return [
            node
            for node in revision_nodes
            if int(node.get("slide_index") or 0) == scope_index
        ]
    if scope_type == "sheet":
        sheet_names = xlsx_sheet_names_from_nodes(revision_nodes)
        if isinstance(scope_index, int):
            if scope_index < 1 or scope_index > len(sheet_names):
                raise ValueError(f"{scope_index}번 시트를 찾지 못했습니다.")
            sheet_name = sheet_names[scope_index - 1]
        else:
            sheet_name = str(scope_index or "").strip()
        return [
            node
            for node in revision_nodes
            if str(node.get("sheet_name") or "") == sheet_name
        ]
    if scope_type == "batch":
        return [
            node
            for node in revision_nodes
            if int(node.get("page_num") or 0) == scope_index
        ]
    raise ValueError("현재 문서 종류에서 지원하지 않는 수정 단위입니다.")


def _validate_revision_scope_type(ext: str, scope_type: str) -> None:
    if ext == ".pptx":
        allowed_scope_types = {"document", "slide"}
    elif ext == ".xlsx":
        allowed_scope_types = {"document", "sheet"}
    else:
        allowed_scope_types = {"document", "batch"}
    if scope_type not in allowed_scope_types:
        raise ValueError("현재 문서 종류에서 지원하지 않는 수정 단위입니다.")


async def revise_office_translation_job(
    job_id: str,
    scope: dict[str, Any] | str | None,
    target_lang: str,
    deps: OfficePipelineDeps,
    *,
    scopes: list[Any] | None = None,
    translator_mode: str | None = None,
    style_options: dict[str, Any] | None = None,
    instruction: str = "",
    preview_output_dir: str = "",
    preview_base_url: str = "",
) -> dict[str, Any]:
    """완료된 Office translation job을 기준으로 수정 번역을 수행한다."""

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

    normalized_scopes = _normalize_revision_scope_list(scope, scopes)
    target_node_ids: set[int] = set()
    revision_scopes: list[str] = []
    for scope_type, scope_index in normalized_scopes:
        _validate_revision_scope_type(ext, scope_type)
        scoped_nodes = _nodes_for_revision_scope(ext, revision_nodes, scope_type, scope_index)
        if not scoped_nodes:
            readable_scope = _current_scope(scope_type, scope_index) or "document"
            raise ValueError(f"{readable_scope}에서 수정할 텍스트를 찾지 못했습니다.")
        revision_scopes.append(_current_scope(scope_type, scope_index) or "document")
        for node in scoped_nodes:
            if node.get("node_id") is None:
                continue
            try:
                target_node_ids.add(int(node.get("node_id")))
            except (TypeError, ValueError):
                continue
    target_nodes = []
    for node in revision_nodes:
        try:
            node_id = int(node.get("node_id"))
        except (TypeError, ValueError):
            continue
        if node_id in target_node_ids:
            target_nodes.append(node)
    single_scope_type, single_scope_index = (
        normalized_scopes[0] if len(normalized_scopes) == 1 else ("document", None)
    )

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
    translated_file_url = payload.get("translated_file_url")
    translated_file_path = payload.get("_translated_file_path")
    if preview_output_dir:
        download_dir = os.path.join(preview_output_dir, job_id, "download")
        os.makedirs(download_dir, exist_ok=True)
        translated_file_path = os.path.join(download_dir, f"translated-revised{ext}")
        _save_office_document(office_obj, ext, translated_file_path, deps)
        translated_file_url = upload_office_to_azure(
            Path(translated_file_path),
            job_token=job_id,
            download_filename=f"translated-revised{ext}",
        )
        version = f"revision-{int(time.time() * 1000)}"
        translated_preview_html_url = build_html_preview_url(
            ext,
            translated_file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_id,
            subdir=translated_html_preview_subdir(ext, version=version),
        )
        translated_preview_html_url = append_preview_version(translated_preview_html_url, version)

    pairs = build_pairs_from_nodes(revision_nodes)
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
        "translated_file_url": translated_file_url,
        "translated_preview_status": "done",
        "translation_status": "done",
        "translation_error": artifacts.translation_error or None,
        "current_scope": revision_scopes[0] if len(revision_scopes) == 1 else None,
        "current_slide": (
            single_scope_index if single_scope_type == "slide" else payload.get("total_slides")
        ),
        "current_page": (
            single_scope_index if single_scope_type == "batch" else payload.get("total_pages")
        ),
        "current_sheet": (
            single_scope_index
            if single_scope_type == "sheet" and isinstance(single_scope_index, int)
            else payload.get("total_sheets")
        ),
        "current_sheet_name": (
            str(single_scope_index)
            if single_scope_type == "sheet" and not isinstance(single_scope_index, int)
            else None
        ),
        "total_slides": payload.get("total_slides"),
        "total_pages": payload.get("total_pages"),
        "total_sheets": payload.get("total_sheets"),
        "event_phase": "completed",
        "revision_status": "done",
        "revision_scope": scope or None,
        "revision_scopes": revision_scopes,
        **llm_debug_payload(),
    }
    update_translation_job(
        job_id,
        {
            **result,
            "_translated_file_path": translated_file_path,
            "_translated_file_ext": ext,
            **build_revision_context_payload(
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
