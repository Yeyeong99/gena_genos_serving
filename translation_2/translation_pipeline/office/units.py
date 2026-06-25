"""Office 번역/주입 단위 변환 유틸리티."""

from __future__ import annotations

import re
from typing import Dict, List

from .types import InjectionUnit, ResolvedInjection, TranslationTarget, TranslationUnit


_DOCX_SPLIT_THRESHOLD = 500
_DOCX_HARD_SPLIT_THRESHOLD = 800
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")

_DOCX_CONTEXT_SIDE_LIMIT = 220
_DOCX_CONTEXT_TITLE_LIMIT = 220
_XLSX_CONTEXT_CELL_LIMIT = 120
_XLSX_CONTEXT_MAX_ITEMS = 6
_DOCX_TABLE_CAPTION_RE = re.compile(r"^(?:Table|Figure)\s+\d+[.:]?\s+.+", flags=re.IGNORECASE)
_ABBREVIATED_TOKEN_RE = re.compile(r"\b([A-Za-z]{3,8})\.")
_CONTEXT_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9&'/-]{2,}\b")
_KOREAN_PARTICLES_RE = re.compile(
    r"(?P<token>[A-Za-z0-9&/.-]+|\([A-Z0-9&/.-]{2,}\))\s+"
    r"(?P<particle>은|는|이|가|을|를|와|과|로|으로|에|에서|에게|부터|까지)(?=\s|$|[,.!?;:)\]])"
)
_ACRONYM_PAREN_SPACE_RE = re.compile(r"(?P<prefix>[가-힣A-Za-z0-9])\s+\((?P<abbr>[A-Z0-9&/.-]{2,})\)")


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


def _infer_element_type(injection: InjectionUnit) -> str:
    explicit = str(injection.node.get("element_type") or "").strip()
    if explicit:
        return explicit

    group = injection.group.strip().lower()
    text = " ".join(injection.text.strip().split())
    lower_text = text.lower().strip("* ")

    if group in {"table_cell", "sheet_cell"}:
        if lower_text in {"blank", "n/a", "na"}:
            return "placeholder"
        return "table_cell"

    if group.startswith("chart_"):
        return group

    if injection.node_type in {"cell"}:
        return "table_cell"

    return "paragraph" if injection.node_type == "xml_text" else ""


def _node_element_type(injection: InjectionUnit) -> str:
    return str(injection.node.get("element_type") or injection.element_type or "").strip()


def _short_context_value(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:_DOCX_CONTEXT_TITLE_LIMIT]


def _build_context_abbreviation_hints(text: str, context_text: str) -> str:
    """Build generic abbreviation hints from local context.

    Example: source ``Dep.`` and context ``Deposit Processing...`` yields
    ``Dep. = Deposit``. The mapping is inferred from prefix shape only and is
    not a document-specific glossary.
    """

    abbreviations = []
    for match in _ABBREVIATED_TOKEN_RE.finditer(str(text or "")):
        abbreviation = match.group(1).strip()
        if abbreviation and abbreviation not in abbreviations:
            abbreviations.append(abbreviation)
    if not abbreviations or not context_text:
        return ""

    context_words = []
    for match in _CONTEXT_WORD_RE.finditer(context_text):
        word = match.group(0).strip(".,;:()[]{}")
        if word and word not in context_words:
            context_words.append(word)

    hints = []
    for abbreviation in abbreviations:
        abbr_lower = abbreviation.lower()
        matches = [
            word
            for word in context_words
            if len(word) > len(abbreviation)
            and word.lower().startswith(abbr_lower)
            and word.lower() != abbr_lower
        ]
        if not matches:
            continue
        matches.sort(key=lambda item: (len(item), item.lower()))
        hints.append(f"{abbreviation}. = {matches[0]}")
    return "ABBREVIATION_HINTS: " + "; ".join(hints) if hints else ""


def _append_abbreviation_hints(text: str, context_text: str) -> str:
    hints = _build_context_abbreviation_hints(text, context_text)
    if not hints:
        return context_text
    return f"{context_text}\n{hints}" if context_text else hints


def _join_fragments(injection: InjectionUnit, fragments: List[str]) -> str:
    cleaned = [fragment.strip() for fragment in fragments if fragment is not None]
    nonempty_cleaned = [fragment for fragment in cleaned if fragment]
    if not nonempty_cleaned:
        return injection.text
    if injection.node_type == "xml_text":
        return " ".join(nonempty_cleaned)
    return nonempty_cleaned[0]


def _cleanup_translated_text(text: str) -> str:
    cleaned = _ACRONYM_PAREN_SPACE_RE.sub(r"\g<prefix>(\g<abbr>)", str(text or ""))
    cleaned = _KOREAN_PARTICLES_RE.sub(r"\g<token>\g<particle>", cleaned)
    return cleaned


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
    table_titles: Dict[int, str] = {}
    table_sections: Dict[int, str] = {}
    latest_caption = ""
    latest_caption_table_index: int | None = None
    latest_heading = ""
    for injection in ordered_docx_units:
        text = _short_context_value(injection.text)
        if not text:
            continue
        if injection.table_index is None:
            element_type = _node_element_type(injection)
            if element_type == "heading":
                latest_heading = text
            if _DOCX_TABLE_CAPTION_RE.match(text):
                latest_caption = text
                latest_caption_table_index = None
            continue
        if latest_caption and (
            latest_caption_table_index is None
            or latest_caption_table_index == injection.table_index
        ):
            table_titles.setdefault(injection.table_index, latest_caption)
            if not injection.node.get("table_title"):
                injection.node["table_title"] = latest_caption
            latest_caption_table_index = injection.table_index
        if latest_heading:
            table_sections.setdefault(injection.table_index, latest_heading)
            if not injection.node.get("section"):
                injection.node["section"] = latest_heading
            if not injection.node.get("section_path"):
                injection.node["section_path"] = [latest_heading]

    for index, injection in enumerate(ordered_docx_units):
        context_parts: List[str] = []
        if injection.table_index is not None:
            table_title = table_titles.get(injection.table_index, "")
            table_section = table_sections.get(injection.table_index, "")
            if table_section:
                context_parts.append(f"SECTION_HEADING: {table_section}")
            if table_title:
                context_parts.append(f"TABLE_TITLE: {table_title}")
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
        contexts[injection.injection_unit_id] = "\n".join(context_parts)
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
                doc_format=str(node.get("doc_format", "")),
                table_index=(
                    int(node["table_index"])
                    if node.get("table_index") is not None
                    else None
                ),
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
                element_type=str(node.get("element_type", "")),
                is_header=bool(node.get("is_header", False)),
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
        element_type = _infer_element_type(injection)
        injection.element_type = element_type
        for fragment_index, fragment_text in enumerate(fragments):
            target = TranslationTarget(
                injection_unit_id=injection.injection_unit_id,
                fragment_index=fragment_index,
                fragment_count=fragment_count,
            )
            dedupe_key = (
                f"{element_type}\0{fragment_text}"
                if _should_dedupe_fragment(injection)
                else ""
            )
            unit = grouped.get(dedupe_key) if dedupe_key else None
            if unit is None:
                effective_context_text = _append_abbreviation_hints(fragment_text, context_text)
                unit = TranslationUnit(
                    translation_unit_id=next_id,
                    text=fragment_text,
                    targets=[target],
                    context_scope=context_scope,
                    context_text=effective_context_text,
                    element_type=element_type,
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
        translated_text = _cleanup_translated_text(translated_text)
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
