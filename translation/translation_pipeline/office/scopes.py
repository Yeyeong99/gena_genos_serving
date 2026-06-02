"""Scope helpers for Office translation streaming."""

from __future__ import annotations

import os

_DOCX_TRANSLATION_SCOPE_MAX_CHARS = int(os.getenv("AI_TRANSLATION_DOCX_SCOPE_MAX_CHARS", "6000"))
_DOCX_TRANSLATION_SCOPE_MAX_ITEMS = int(os.getenv("AI_TRANSLATION_DOCX_SCOPE_MAX_ITEMS", "20"))


def docx_total_chars(nodes: list[dict]) -> int:
    return sum(len(str(node.get("text", "")).strip()) for node in nodes)


def node_text_chars_by_id(nodes: list[dict]) -> dict[int, int]:
    chars_by_id: dict[int, int] = {}
    for node in nodes:
        raw_node_id = node.get("node_id")
        if raw_node_id is None:
            continue
        try:
            node_id = int(raw_node_id)
        except (TypeError, ValueError):
            continue
        chars_by_id[node_id] = len(str(node.get("text", "")).strip())
    return chars_by_id


def docx_node_ids_for_scope(nodes: list[dict], scope: str) -> set[int]:
    """Return node ids that belong to a DOCX virtual translation scope."""

    current_page = scope_page_number(scope)
    if current_page is None:
        return set()

    node_ids: set[int] = set()
    for node in nodes:
        try:
            node_page = int(node.get("page_num") or 0)
            node_id = int(node.get("node_id"))
        except (TypeError, ValueError):
            continue
        if node_page == current_page:
            node_ids.add(node_id)
    return node_ids


def assign_docx_translation_batches(nodes: list[dict]) -> int:
    """DOCX 번역 구간을 글자 수/항목 수 기준의 가상 scope로 나눈다."""

    if not nodes:
        return 0

    for node in nodes:
        node.pop("page_num", None)
        node.pop("original_page_num", None)
        node.pop("translated_page_num", None)

    batch_index = 1
    current_chars = 0
    current_items = 0
    for node in nodes:
        text = str(node.get("text", "")).strip()
        estimated_chars = len(text)
        if current_items and (
            current_items >= _DOCX_TRANSLATION_SCOPE_MAX_ITEMS
            or current_chars + estimated_chars > _DOCX_TRANSLATION_SCOPE_MAX_CHARS
        ):
            batch_index += 1
            current_chars = 0
            current_items = 0

        node["page_num"] = batch_index
        node["original_page_num"] = batch_index
        node["translated_page_num"] = batch_index
        current_chars += estimated_chars
        current_items += 1

    return batch_index


def scope_preview_suffix(scope: str) -> str:
    """SSE scope 문자열을 preview 디렉터리 suffix로 변환한다."""

    return scope.replace(":", "-")


def scope_slide_number(scope: str) -> int | None:
    if not scope.startswith("pptx:slide:"):
        return None
    try:
        return int(scope.split(":")[-1])
    except ValueError:
        return None


def scope_page_number(scope: str) -> int | None:
    if not scope.startswith("docx:page:"):
        return None
    try:
        return int(scope.split(":")[-1])
    except ValueError:
        return None


def scope_sheet_name(scope: str) -> str:
    if not scope.startswith("xlsx:sheet:"):
        return ""
    return scope.split(":", 2)[-1]
