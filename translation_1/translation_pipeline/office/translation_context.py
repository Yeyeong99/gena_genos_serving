"""Office 번역 프롬프트에 넣을 문서/용어 메모리 필터링."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from translation_pipeline.common.document_term_memory import find_relevant_document_terms
from translation_pipeline.common.bilingual_summary_memory import get_prompt_bilingual_summary

from .translation_memory import bilingual_summary_memory, document_term_memory
from .types import TranslationUnit

_GLOSSARY_CONTEXT_PREFIXES = ("TABLE_TITLE:", "SECTION_HEADING:", "ABBREVIATION_HINTS:")
_PRE_ANALYSIS_LIST_KEYS = (
    "source_meaning_notes",
    "acronym_notes",
)


def glossary_lookup_texts(units: List[TranslationUnit]) -> List[str]:
    """현재 batch에서 용어 메모리 조회에 사용할 원문/문맥 텍스트를 모은다."""

    texts: List[str] = []
    for unit in units:
        if unit.text:
            texts.append(unit.text)
        for line in str(unit.context_text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith(_GLOSSARY_CONTEXT_PREFIXES):
                texts.append(stripped)
    return texts


def _normalized_match_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _analysis_entry_sources(entry: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("source", "term", "source_term"):
        value = str(entry.get(key) or "").strip()
        if value:
            values.append(value)
    for key in ("source_terms", "aliases", "patterns"):
        items = entry.get(key)
        if isinstance(items, list):
            values.extend(str(item).strip() for item in items if str(item).strip())
    return values


def _analysis_entry_matches(entry: dict[str, Any], lookup_text: str) -> bool:
    for source in _analysis_entry_sources(entry):
        normalized_source = _normalized_match_text(source)
        if normalized_source and re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_source)}(?![a-z0-9])",
            lookup_text,
        ):
            return True
    return False


def filter_analysis_entries(entries: Any, lookup_text: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """현재 chunk와 관련 있는 pre-analysis list entry만 남긴다."""

    if not isinstance(entries, list):
        return []
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if _analysis_entry_matches(entry, lookup_text):
            filtered.append(entry)
        if len(filtered) >= limit:
            break
    return filtered


def style_options_with_relevant_pre_analysis(
    style_options: Dict[str, Any] | None,
    units: List[TranslationUnit],
) -> Dict[str, Any] | None:
    """현재 batch에 관련 있는 pre-analysis 항목만 prompt에 남긴다."""

    if not isinstance(style_options, dict):
        return style_options
    analysis = style_options.get("_pre_translation_analysis")
    if not isinstance(analysis, dict):
        return style_options

    lookup_text = _normalized_match_text("\n".join(glossary_lookup_texts(units)))
    if not lookup_text:
        return style_options

    filtered_analysis = dict(analysis)
    matched_any = False
    for key in _PRE_ANALYSIS_LIST_KEYS:
        if key not in filtered_analysis:
            continue
        filtered = filter_analysis_entries(filtered_analysis.get(key), lookup_text)
        filtered_analysis[key] = filtered
        matched_any = matched_any or bool(filtered)

    # Keep document summary, but avoid carrying unrelated source meaning notes
    # into every chunk.
    if not matched_any:
        for key in _PRE_ANALYSIS_LIST_KEYS:
            if key in filtered_analysis:
                filtered_analysis[key] = []

    return {
        **style_options,
        "_pre_translation_analysis": filtered_analysis,
    }


def style_options_with_relevant_glossary(
    style_options: Dict[str, Any] | None,
    units: List[TranslationUnit],
) -> Dict[str, Any] | None:
    """현재 batch와 관련 있는 document term memory만 prompt에 넣는다."""

    style_options = style_options_with_relevant_pre_analysis(style_options, units)
    summary_memory = bilingual_summary_memory(style_options)
    prompt_summary = get_prompt_bilingual_summary(summary_memory)
    if prompt_summary:
        style_options = {
            **(style_options or {}),
            "_bilingual_summary_memory": prompt_summary,
        }
    term_memory = document_term_memory(style_options)
    if term_memory:
        relevant_document_terms = find_relevant_document_terms(
            term_memory,
            glossary_lookup_texts(units),
            primary_texts=[unit.text for unit in units if unit.text],
        )
        style_options = {
            **(style_options or {}),
            "_document_term_memory": {
                "terms": relevant_document_terms,
            },
        }
    return style_options
