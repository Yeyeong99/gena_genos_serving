"""Office 번역/주입 단위 변환 유틸리티."""

from __future__ import annotations

import re
from typing import Dict, List

from .types import InjectionUnit, ResolvedInjection, TranslationTarget, TranslationUnit


_DOCX_SPLIT_THRESHOLD = 500
_DOCX_HARD_SPLIT_THRESHOLD = 800
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")

_DOCX_CONTEXT_SIDE_LIMIT = 220
_XLSX_CONTEXT_CELL_LIMIT = 120
_XLSX_CONTEXT_MAX_ITEMS = 6


def _chunk_words(text: str, max_length: int) -> List[str]:
    words = text.split()
    if not words:
        return [text]
    chunks: List[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        chunks.append(current)
        current = word
    chunks.append(current)
    return chunks


def _split_docx_text(text: str) -> List[str]:
    normalized = text.strip()
    if len(normalized) <= _DOCX_SPLIT_THRESHOLD:
        return [normalized]

    sentences = [item.strip() for item in _SENTENCE_BOUNDARY_RE.split(normalized) if item.strip()]
    if len(sentences) <= 1:
        return _chunk_words(normalized, _DOCX_HARD_SPLIT_THRESHOLD)

    chunks: List[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > _DOCX_HARD_SPLIT_THRESHOLD:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_chunk_words(sentence, _DOCX_HARD_SPLIT_THRESHOLD))
            continue

        candidate = sentence if not current else f"{current} {sentence}"
        if len(candidate) <= _DOCX_HARD_SPLIT_THRESHOLD:
            current = candidate
            continue

        if current:
            chunks.append(current)
        current = sentence

    if current:
        chunks.append(current)
    return chunks or [normalized]


def _split_injection_text(injection: InjectionUnit) -> List[str]:
    text = injection.text.strip()
    if not text:
        return [""]
    if injection.node_type == "xml_text":
        return _split_docx_text(text)
    return [text]


def _join_fragments(injection: InjectionUnit, fragments: List[str]) -> str:
    cleaned = [fragment.strip() for fragment in fragments if fragment is not None]
    nonempty_cleaned = [fragment for fragment in cleaned if fragment]
    if not nonempty_cleaned:
        return injection.text
    if injection.node_type == "xml_text":
        return " ".join(nonempty_cleaned)
    return nonempty_cleaned[0]


def _build_pptx_slide_contexts(injection_units: List[InjectionUnit]) -> Dict[int, str]:
    slide_texts: Dict[int, List[str]] = {}
    for injection in injection_units:
        if injection.slide_index is None:
            continue
        text = injection.text.strip()
        if not text:
            continue
        slide_texts.setdefault(injection.slide_index, []).append(text)

    contexts: Dict[int, str] = {}
    if not slide_texts:
        return contexts

    first_slide_index = min(slide_texts.keys())
    first_slide_text = " ".join(slide_texts.get(first_slide_index, []))
    for slide_index in sorted(slide_texts.keys()):
        previous_text = " ".join(slide_texts.get(slide_index - 1, []))
        current_text = " ".join(slide_texts.get(slide_index, []))
        context_parts = [part for part in (first_slide_text, previous_text, current_text) if part]
        contexts[slide_index] = " ".join(context_parts)
    return contexts


def _build_docx_contexts(injection_units: List[InjectionUnit]) -> Dict[int, str]:
    contexts: Dict[int, str] = {}
    ordered_docx_units = [
        injection
        for injection in injection_units
        if injection.source in {"body", "header", "footer"} and injection.text.strip()
    ]
    for index, injection in enumerate(ordered_docx_units):
        context_parts: List[str] = []
        previous_text = (
            ordered_docx_units[index - 1].text.strip()
            if index - 1 >= 0
            else ""
        )
        next_text = (
            ordered_docx_units[index + 1].text.strip()
            if index + 1 < len(ordered_docx_units)
            else ""
        )
        if previous_text:
            context_parts.append(f"PREVIOUS: {previous_text[:_DOCX_CONTEXT_SIDE_LIMIT]}")
        if next_text:
            context_parts.append(f"NEXT: {next_text[:_DOCX_CONTEXT_SIDE_LIMIT]}")
        contexts[injection.injection_unit_id] = " ".join(context_parts)
    return contexts


def _cell_ref(injection: InjectionUnit) -> str:
    return str(injection.node.get("cell_ref") or "")


def _short_cell_text(injection: InjectionUnit) -> str:
    text = injection.text.strip().replace("\n", " ")
    return text[:_XLSX_CONTEXT_CELL_LIMIT]


def _format_xlsx_cells(label: str, cells: List[InjectionUnit]) -> str:
    if not cells:
        return ""
    formatted = []
    for item in cells[:_XLSX_CONTEXT_MAX_ITEMS]:
        ref = _cell_ref(item)
        prefix = f"{ref}: " if ref else ""
        formatted.append(f"{prefix}{_short_cell_text(item)}")
    return f"{label}: " + " | ".join(formatted)


def _build_xlsx_contexts(injection_units: List[InjectionUnit]) -> Dict[int, str]:
    contexts: Dict[int, str] = {}
    units_by_sheet: Dict[str, List[InjectionUnit]] = {}
    for injection in injection_units:
        if not injection.sheet_name or injection.row is None or injection.col is None:
            continue
        text = injection.text.strip()
        if not text:
            continue
        units_by_sheet.setdefault(injection.sheet_name, []).append(injection)

    for sheet_name, sheet_units in units_by_sheet.items():
        ordered = sorted(
            sheet_units,
            key=lambda item: (
                item.row if item.row is not None else 10**9,
                item.col if item.col is not None else 10**9,
            ),
        )
        for injection in ordered:
            if injection.row is None or injection.col is None:
                continue

            row = injection.row
            col = injection.col
            same_row_left = [
                item
                for item in ordered
                if item.row == row
                and item.col is not None
                and item.col < col
            ][-_XLSX_CONTEXT_MAX_ITEMS:]
            same_col_above = [
                item
                for item in ordered
                if item.col == col
                and item.row is not None
                and item.row < row
            ][-_XLSX_CONTEXT_MAX_ITEMS:]
            previous_rows = [
                item
                for item in ordered
                if item.row is not None
                and 0 < row - item.row <= 3
                and item is not injection
            ][-_XLSX_CONTEXT_MAX_ITEMS:]
            nearby_cells = [
                item
                for item in ordered
                if item is not injection
                and item.row is not None
                and item.col is not None
                and abs(item.row - row) <= 1
                and abs(item.col - col) <= 2
            ][:_XLSX_CONTEXT_MAX_ITEMS]

            parts = [
                f"SHEET: {sheet_name}",
                f"CELL: {_cell_ref(injection) or f'R{row}C{col}'}",
            ]
            merged_range = str(injection.node.get("merged_range") or "").strip()
            if merged_range:
                parts.append(f"MERGED_RANGE: {merged_range}")
            for formatted in (
                _format_xlsx_cells("COLUMN_HEADERS_OR_ABOVE", same_col_above),
                _format_xlsx_cells("ROW_LABELS_OR_LEFT", same_row_left),
                _format_xlsx_cells("PREVIOUS_ROWS", previous_rows),
                _format_xlsx_cells("NEARBY_CELLS", nearby_cells),
            ):
                if formatted:
                    parts.append(formatted)
            contexts[injection.injection_unit_id] = "\n".join(parts)
    return contexts


def _build_translation_context(
    injection: InjectionUnit,
    pptx_slide_contexts: Dict[int, str],
    docx_contexts: Dict[int, str],
    xlsx_contexts: Dict[int, str],
) -> tuple[str, str]:
    if injection.slide_index is not None:
        return (
            f"pptx:slide:{injection.slide_index}",
            pptx_slide_contexts.get(injection.slide_index, injection.text.strip()),
        )
    if injection.sheet_name:
        return (
            f"xlsx:sheet:{injection.sheet_name}",
            xlsx_contexts.get(injection.injection_unit_id, injection.text.strip()),
        )
    if injection.page_num is not None:
        return (
            f"docx:page:{injection.page_num}",
            docx_contexts.get(injection.injection_unit_id, injection.text.strip()),
        )
    if injection.source:
        return (
            f"docx:{injection.source}",
            docx_contexts.get(injection.injection_unit_id, injection.text.strip()),
        )
    return ("", injection.text.strip())


def _should_dedupe_fragment(injection: InjectionUnit) -> bool:
    return injection.node_type == "xml_text"


def build_injection_units(nodes: List[dict]) -> List[InjectionUnit]:
    """추출 노드 목록을 주입 단위 목록으로 변환한다."""

    units: List[InjectionUnit] = []
    for index, node in enumerate(nodes):
        node_id = int(node.get("node_id", index))
        units.append(
            InjectionUnit(
                injection_unit_id=index,
                node_id=node_id,
                text=str(node.get("text", "")),
                node=node,
                source=str(node.get("source", "")),
                group=str(node.get("group", "")),
                node_type=str(node.get("type", "")),
                bbox=node.get("bbox"),
                slide_index=(
                    int(node["slide_index"])
                    if node.get("slide_index") is not None
                    else None
                ),
                sheet_name=str(node.get("sheet_name", "")),
                row=int(node["row"]) if node.get("row") is not None else None,
                col=int(node["col"]) if node.get("col") is not None else None,
                shape_name=str(node.get("shape_name", "")),
                page_num=(
                    int(node["page_num"])
                    if node.get("page_num") is not None
                    else None
                ),
            )
        )
    return units


def build_translation_units(injection_units: List[InjectionUnit]) -> List[TranslationUnit]:
    """주입 단위를 외부 번역용 단위로 묶는다.

    현재는 docx 긴 문단 분할 + 포맷별 컨텍스트 seed 생성을 수행한다.
    """

    pptx_slide_contexts = _build_pptx_slide_contexts(injection_units)
    docx_contexts = _build_docx_contexts(injection_units)
    xlsx_contexts = _build_xlsx_contexts(injection_units)
    grouped: Dict[str, TranslationUnit] = {}
    units: List[TranslationUnit] = []
    next_id = 0
    for injection in injection_units:
        fragments = _split_injection_text(injection)
        fragment_count = len(fragments)
        context_scope, context_text = _build_translation_context(
            injection,
            pptx_slide_contexts,
            docx_contexts,
            xlsx_contexts,
        )
        for fragment_index, fragment_text in enumerate(fragments):
            target = TranslationTarget(
                injection_unit_id=injection.injection_unit_id,
                fragment_index=fragment_index,
                fragment_count=fragment_count,
            )
            dedupe_key = fragment_text if _should_dedupe_fragment(injection) else ""
            unit = grouped.get(dedupe_key) if dedupe_key else None
            if unit is None:
                unit = TranslationUnit(
                    translation_unit_id=next_id,
                    text=fragment_text,
                    targets=[target],
                    context_scope=context_scope,
                    context_text=context_text,
                )
                if dedupe_key:
                    grouped[dedupe_key] = unit
                units.append(unit)
                next_id += 1
                continue
            unit.targets.append(target)
    return units


def resolve_injection_units(
    injection_units: List[InjectionUnit],
    translation_units: List[TranslationUnit],
    translated_by_unit_id: Dict[int, str],
) -> List[ResolvedInjection]:
    """번역 단위 결과를 주입 단위 기준으로 다시 푼다."""

    translated_fragments_by_injection_id: Dict[int, Dict[int, str]] = {}
    fragment_count_by_injection_id: Dict[int, int] = {}
    for translation_unit in translation_units:
        translated_text = translated_by_unit_id.get(translation_unit.translation_unit_id, translation_unit.text)
        for target in translation_unit.targets:
            fragment_count_by_injection_id[target.injection_unit_id] = max(
                target.fragment_count,
                fragment_count_by_injection_id.get(target.injection_unit_id, 1),
            )
            translated_fragments_by_injection_id.setdefault(target.injection_unit_id, {})[
                target.fragment_index
            ] = translated_text

    resolved: List[ResolvedInjection] = []
    for injection in injection_units:
        fragment_count = fragment_count_by_injection_id.get(injection.injection_unit_id, 1)
        translated_fragments_map = translated_fragments_by_injection_id.get(injection.injection_unit_id, {})
        translated_fragments = [
            translated_fragments_map.get(index, "")
            for index in range(fragment_count)
        ]
        translated_text = _join_fragments(injection, translated_fragments)
        resolved.append(
            ResolvedInjection(
                injection_unit_id=injection.injection_unit_id,
                node_id=injection.node_id,
                original_text=injection.text,
                translated_text=translated_text,
                translated_fragments=translated_fragments if fragment_count > 1 else None,
            )
        )
    return resolved
