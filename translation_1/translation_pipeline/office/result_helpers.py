"""Shared result-building helpers for Office pipelines."""

from __future__ import annotations

import os
import shutil
from typing import Any

from translation_pipeline.common.llm import Config


def build_pairs_from_nodes(nodes: list[dict]) -> list[dict]:
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


def build_revision_context_payload(
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


def persist_docx_revision_source(
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


def llm_debug_payload() -> dict[str, Any]:
    return {
        "llm_model_name": Config.DEFAULT_TRANSLATION_MODEL,
        "llm_provider_sort": Config.LLM_API_PROVIDER_SORT or None,
    }
