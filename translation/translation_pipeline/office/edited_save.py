"""Save helpers for user-edited Office translation output."""

from __future__ import annotations

from typing import Any

from translation_pipeline.common.logging_utils import log_info

from .extract import load_office_document
from .preview_helpers import build_html_preview_url, html_only_preview_payload
from .save import (
    apply_edited_pairs_to_pairs,
    inject_edited_office_document,
    save_edited_office_document,
)
from .types import OfficePipelineDeps


def build_edited_style_by_id(edited_pairs: list[dict]) -> dict[int, dict[str, Any]]:
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


def apply_edited_styles_to_nodes(nodes: list[dict], edited_style_by_id: dict[int, dict[str, Any]]) -> None:
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
    """사용자 수정본을 반영한 Office 문서를 저장한다."""

    log_info(f"[수정본 저장] Office 문서 저장 시작: {file_path} ({ext})")
    await deps.emit_event("EXTRACT_START", callback_url)
    bundle = load_office_document(file_path, ext, deps)
    await deps.emit_event("EXTRACT_DONE", callback_url, nodes=len(bundle.nodes))

    edited_text_by_id = deps.build_edited_text_by_id(edited_pairs)
    edited_style_by_id = build_edited_style_by_id(edited_pairs)
    pairs = deps.build_translation_pairs(bundle.nodes, {})

    await deps.emit_event("INJECT_START", callback_url)
    deps.apply_node_translations(bundle.nodes, edited_text_by_id=edited_text_by_id)
    apply_edited_styles_to_nodes(bundle.nodes, edited_style_by_id)
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
            **html_only_preview_payload(),
            "original_preview_html_url": build_html_preview_url(
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
