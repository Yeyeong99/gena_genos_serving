"""Office 포맷 공통 context-aware translation executor."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import aiohttp

from translation_pipeline.common.job_artifacts import job_artifact_path, safe_artifact_part
from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.prompt_builder import (
    build_office_context_system_prompt,
    build_office_context_user_prompt,
    build_single_user_prompt,
    build_validation_retry_system_prompt,
)
from translation_pipeline.common.term_observer import record_observed_translations
from translation_pipeline.common.document_term_memory import normalize_document_source

from .translation_context import style_options_with_relevant_glossary
from .translation_memory import temporary_glossary_memory
from .translation_validation import (
    duplicate_like_translation_unit_ids,
    is_symbol_junk_source,
    needs_context_label_retry,
    needs_corruption_retry,
    needs_formality_retry,
    needs_structure_retry,
    normalize_space,
    parse_json_array_response,
    target_language_retry_reasons,
    validate_context_batch_items,
)
from .types import TranslationUnit

_XLSX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_XLSX_MAX_ITEMS_PER_BATCH", "24"))
_XLSX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_XLSX_MAX_CHARS_PER_BATCH", "9000"))
_PPTX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_PPTX_MAX_ITEMS_PER_BATCH", "24"))
_PPTX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_PPTX_MAX_CHARS_PER_BATCH", "9000"))
_PPTX_CONTEXT_SCOPE_CONCURRENCY = int(os.getenv("AI_TRANSLATION_PPTX_SCOPE_CONCURRENCY", "1"))
_DOCX_CONTEXT_SCOPE_CONCURRENCY = int(os.getenv("AI_TRANSLATION_DOCX_SCOPE_CONCURRENCY", "5"))
_PPTX_CONTEXT_VERBOSE_LOG = os.getenv("AI_TRANSLATION_PPTX_CONTEXT_VERBOSE_LOG", "0") == "1"
_LLM_VALIDATION_RETRY_COUNT = int(os.getenv("AI_TRANSLATION_LLM_VALIDATION_RETRY_COUNT", "1"))
_TARGET_LANGUAGE_VALIDATION_RETRY_COUNT = int(os.getenv("AI_TRANSLATION_TARGET_LANGUAGE_RETRY_COUNT", "2"))
_SINGLE_RESCUE_CONTEXT_RETRY_COUNT = int(os.getenv("AI_TRANSLATION_SINGLE_RESCUE_CONTEXT_RETRY_COUNT", "1"))
_SINGLE_RESCUE_SOURCE_ONLY_RETRY_COUNT = int(os.getenv("AI_TRANSLATION_SINGLE_RESCUE_SOURCE_ONLY_RETRY_COUNT", "2"))
_SINGLE_RESCUE_CONTEXT_MAX_CHARS = int(os.getenv("AI_TRANSLATION_SINGLE_RESCUE_CONTEXT_MAX_CHARS", "2400"))
_SINGLE_RESCUE_TERM_LIMIT = int(os.getenv("AI_TRANSLATION_SINGLE_RESCUE_TERM_LIMIT", "8"))
_DEFAULT_TRANSLATION_PROMPT_SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "tmp" / "translation_prompt_snapshots"
_ELEMENT_TYPE_ORDER = (
    "placeholder",
    "column_header",
    "row_header",
    "table_cell",
    "heading",
    "list_item",
    "paragraph",
    "slide_title",
    "text_box",
    "chart_title",
    "chart_axis",
    "chart_series",
    "chart_category",
    "chart_label",
)


class TranslationRescueExhaustedError(RuntimeError):
    """Raised when all retry/rescue attempts fail to produce a valid translation."""


@dataclass(frozen=True, slots=True)
class ContextTranslationConfig:
    """포맷별 문맥 번역 정책."""

    doc_format: str
    log_prefix: str
    source_label: str
    context_label: str
    context_instruction: str
    extra_instruction: str | Callable[[str], str] = ""
    max_items_per_batch: int | None = None
    max_chars_per_batch: int | None = None
    scope_concurrency: int = 1
    sort_scopes: bool = False
    concurrent_scopes_only_without_callbacks: bool = False
    use_previous_translation: bool = False
    enable_target_language_retry: bool = False
    validation_retry_top_level_only: bool = False
    single_scope_batch: bool = False
    dedupe_context_chars_in_batch: bool = False
    compact_log_label: bool = False
    log_context_preview: bool = False
    pptx_target_items_prompt: bool = False


DOCX_CONTEXT_CONFIG = ContextTranslationConfig(
    doc_format="docx",
    log_prefix="[DOCX 문맥 번역]",
    source_label="SOURCE_TEXT",
    context_label="CONTEXT",
    context_instruction="Use the CONTEXT only for local meaning.",
    max_items_per_batch=None,
    max_chars_per_batch=None,
    scope_concurrency=_DOCX_CONTEXT_SCOPE_CONCURRENCY,
    sort_scopes=True,
    enable_target_language_retry=True,
    validation_retry_top_level_only=True,
    single_scope_batch=True,
)
XLSX_CONTEXT_CONFIG = ContextTranslationConfig(
    doc_format="xlsx",
    log_prefix="[XLSX 문맥 번역]",
    source_label="CELL_TEXT",
    context_label="CELL_CONTEXT",
    context_instruction="Use CELL_CONTEXT only to understand the spreadsheet table.",
    extra_instruction=lambda target_lang: (
        "Do not infer script labels from nearby values. "
        f"Translate Korean/Hanja currency display text into natural {target_lang}."
    ),
    max_items_per_batch=_XLSX_CONTEXT_MAX_ITEMS_PER_BATCH,
    max_chars_per_batch=_XLSX_CONTEXT_MAX_CHARS_PER_BATCH,
    use_previous_translation=True,
    enable_target_language_retry=True,
    log_context_preview=True,
)
PPTX_CONTEXT_CONFIG = ContextTranslationConfig(
    doc_format="pptx",
    log_prefix="[PPTX 문맥 번역]",
    source_label="TARGET_TEXT",
    context_label="CONTEXT",
    context_instruction="Use the CONTEXT only to understand the presentation item.",
    extra_instruction="Keep slide labels and table cells compact.",
    max_items_per_batch=_PPTX_CONTEXT_MAX_ITEMS_PER_BATCH,
    max_chars_per_batch=_PPTX_CONTEXT_MAX_CHARS_PER_BATCH,
    scope_concurrency=_PPTX_CONTEXT_SCOPE_CONCURRENCY,
    sort_scopes=True,
    concurrent_scopes_only_without_callbacks=True,
    use_previous_translation=True,
    enable_target_language_retry=True,
    dedupe_context_chars_in_batch=True,
    compact_log_label=True,
    pptx_target_items_prompt=True,
)


def scope_sort_key(scope: str) -> Tuple[int, str]:
    """Office scope 이름을 사용자 표시 순서대로 정렬하기 위한 key."""

    if scope.startswith("pptx:slide:"):
        try:
            return (int(scope.split(":")[-1]), scope)
        except ValueError:
            return (10**9, scope)
    if scope.startswith("docx:page:"):
        try:
            return (int(scope.split(":")[-1]), scope)
        except ValueError:
            return (10**9, scope)
    return (10**9, scope)


def _batch_element_type(units: List[TranslationUnit]) -> str:
    present = {unit.element_type for unit in units if unit.element_type}
    ordered = [item for item in _ELEMENT_TYPE_ORDER if item in present]
    ordered.extend(sorted(present - set(ordered)))
    return ",".join(ordered)


def _previous_by_injection_id(style_options: Dict[str, Any] | None) -> dict[int, str]:
    previous_by_injection_id = (
        style_options.get("_previous_translation_by_injection_id")
        if isinstance(style_options, dict)
        else None
    )
    return previous_by_injection_id if isinstance(previous_by_injection_id, dict) else {}


def _previous_text_for_unit(unit: TranslationUnit, previous_by_injection_id: dict[int, str]) -> str:
    for target in unit.targets:
        previous = previous_by_injection_id.get(target.injection_unit_id)
        if previous:
            return str(previous)
    return ""


def _previous_items_for_batch(
    batch: List[TranslationUnit],
    previous_by_injection_id: dict[int, str],
) -> Dict[int, str]:
    previous_items: Dict[int, str] = {}
    for unit in batch:
        previous_text = _previous_text_for_unit(unit, previous_by_injection_id)
        if previous_text:
            previous_items[unit.translation_unit_id] = previous_text
    return previous_items


def _extra_instruction(config: ContextTranslationConfig, target_lang: str) -> str:
    if callable(config.extra_instruction):
        return config.extra_instruction(target_lang)
    return str(config.extra_instruction or "")


def _split_batches(
    units: List[TranslationUnit],
    config: ContextTranslationConfig,
) -> List[List[TranslationUnit]]:
    if config.single_scope_batch:
        return [units] if units else []

    batches: List[List[TranslationUnit]] = []
    current: List[TranslationUnit] = []
    current_chars = 0
    current_context = ""
    max_items = config.max_items_per_batch or 10**9
    max_chars = config.max_chars_per_batch or 10**12

    for unit in units:
        unit_context = unit.context_text or ""
        context_chars = 0 if config.dedupe_context_chars_in_batch and current and unit_context == current_context else len(unit_context)
        estimated_chars = len(unit.text) + context_chars + 100
        if current and (
            len(current) >= max_items
            or current_chars + estimated_chars > max_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
            current_context = ""
            context_chars = len(unit_context)
            estimated_chars = len(unit.text) + context_chars + 100
        if not current:
            current_context = unit_context
        current.append(unit)
        current_chars += estimated_chars
    if current:
        batches.append(current)
    return batches


def _batch_user_prompt(
    config: ContextTranslationConfig,
    batch: List[TranslationUnit],
    previous_items: Dict[int, str],
) -> str:
    if config.pptx_target_items_prompt:
        target_items = [(unit.translation_unit_id, unit.text) for unit in batch]
        return build_office_context_user_prompt(
            config.doc_format,
            context_text=batch[0].context_text or "",
            target_items=target_items,
            previous_items=previous_items,
        )
    return build_office_context_user_prompt(
        config.doc_format,
        batch,
        previous_items=previous_items if previous_items else None,
    )


def _repair_appendix(
    *,
    previous_translation: str,
    reasons: list[str],
    target_lang: str,
) -> str:
    if not reasons and not previous_translation:
        return ""
    lines = [
        "",
        "VALIDATION_REPAIR:",
        f"- The previous translation failed target-language validation for {target_lang}.",
    ]
    for reason in reasons:
        lines.append(f"- Failure: {reason}")
    forbidden_fragments = _forbidden_fragments_from_reasons(reasons)
    if forbidden_fragments:
        lines.extend(
            [
                "",
                "<DO_NOT_OUTPUT_AGAIN>",
                *forbidden_fragments,
                "</DO_NOT_OUTPUT_AGAIN>",
                "- Do not copy the exact forbidden fragments above into the target text.",
                "- Translate their meaning naturally into the target language instead.",
            ]
        )
    lines.extend(
        [
            "- Re-translate the SOURCE_TEXT only.",
            "- Remove unintended Chinese/Hanja/Kanji/Kana/Cyrillic fragments unless the exact characters appear in SOURCE_TEXT.",
            "- Translate any remaining English prose into the target language. Preserve only proper nouns, acronyms, URLs, standard identifiers, and Document Term Memory preferred targets.",
        ]
    )
    if any(str(reason).startswith("source_target_structure_mismatch") for reason in reasons):
        lines.append(
            "- Preserve the SOURCE_TEXT structure: narrative prose must remain narrative prose, and direct dialogue must remain direct dialogue."
        )
    if previous_translation:
        lines.extend(["", "PREVIOUS_INVALID_TRANSLATION:", previous_translation])
    return "\n".join(lines)


def _forbidden_fragments_from_reasons(reasons: list[str], *, limit: int = 8) -> list[str]:
    """Extract concrete fragments that should not appear again in retry output."""

    fragments: list[str] = []
    seen: set[str] = set()
    for reason in reasons or []:
        text = str(reason or "").strip()
        values: list[str] = []
        if text.startswith("untranslated_english_phrase="):
            values = text.split("=", 1)[1].split(" | ")
        elif text.startswith("unexpected_foreign_script="):
            raw = text.split("=", 1)[1]
            values = [raw] + list(raw)
        elif text in {"context_label_leaked", "corrupted_text"}:
            values = [text]
        for value in values:
            fragment = normalize_space(str(value or "").strip())
            if not fragment or fragment in seen:
                continue
            fragments.append(fragment)
            seen.add(fragment)
            if len(fragments) >= limit:
                return fragments
    return fragments


def _clip_rescue_text(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    half = max(1, max_chars // 2)
    return value[:half].rstrip() + "\n...\n" + value[-half:].lstrip()


def _is_korean_target(target_lang: str) -> bool:
    return str(target_lang or "").strip().lower() in {"korean", "ko", "kor", "한국어"}


def _rescue_document_term_lines(
    style_options: Dict[str, Any] | None,
    source_text: str,
    *,
    limit: int = _SINGLE_RESCUE_TERM_LIMIT,
) -> list[str]:
    if not isinstance(style_options, dict):
        return []
    document_term_memory = style_options.get("_document_term_memory")
    terms = document_term_memory.get("terms") if isinstance(document_term_memory, dict) else None
    if not isinstance(terms, list):
        return []
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in terms:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("memory_kind") or "").strip().lower() == "raw_evidence_candidate":
            continue
        policy = str(entry.get("pre_judge_inject_policy") or "").strip().lower()
        if policy in {"source_meaning_only", "blocked", "drop", "drop_candidate", "unresolved"}:
            continue
        if str(entry.get("preferred_application") or "").strip() == "contextual_meaning":
            continue
        target = str(entry.get("preferred_target") or "").strip()
        if not target:
            continue
        source = next(
            (
                candidate
                for candidate in _term_sources_for_injection(entry)
                if _contains_in_source_text(source_text, candidate)
            ),
            "",
        )
        if not source:
            continue
        key = (normalize_document_source(source), normalize_document_source(target))
        if key in seen:
            continue
        lines.append(f"- {source} => {target}")
        seen.add(key)
        if len(lines) >= limit:
            break
    return lines


def _rescue_translation_system_prompt(target_lang: str) -> str:
    lines = [
        "You are a rescue translation engine.",
        f"Translate into {target_lang}.",
        "Return only the requested JSON array. Do not include explanations, labels, markdown, or source text fallback.",
        "Translate only the text inside <SOURCE_TEXT>.",
        "Use context and term requirements only as references; never copy reference labels into the output.",
    ]
    if _is_korean_target(target_lang):
        lines.append(
            "The translation must be natural Korean. Remove unintended Chinese/Hanja/Kana/Cyrillic fragments unless they appear in SOURCE_TEXT."
        )
    return "\n".join(lines)


def _rescue_relationship_context(style_options: Dict[str, Any] | None, *, limit: int = 8) -> list[str]:
    analysis = (style_options or {}).get("_pre_translation_analysis") if isinstance(style_options, dict) else None
    if not isinstance(analysis, dict):
        return []
    lines: list[str] = []
    for item in analysis.get("participants_and_roles") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        if not source:
            continue
        parts = [source]
        for key, label in (
            ("document_local_role", "role"),
            ("relationship_or_dependency", "relationship"),
            ("register_or_speech_relevance", "register"),
        ):
            value = str(item.get(key) or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        lines.append("; ".join(parts))
        if len(lines) >= limit:
            break
    return lines


def _rescue_translation_user_prompt(
    unit: TranslationUnit,
    *,
    target_lang: str,
    config: ContextTranslationConfig,
    style_options: Dict[str, Any] | None,
    include_context: bool,
    previous_invalid_translation: str,
    reasons: list[str],
) -> str:
    lines = [
        f"TARGET_LANGUAGE: {target_lang}",
        f"DOCUMENT_FORMAT: {config.doc_format}",
        'OUTPUT_FORMAT: [{"id": <same id>, "t": "<target-language translation>"}]',
        "RULES:",
        "- Do not return the original source as fallback.",
        "- If validation failed before, produce a new target-language translation instead of editing labels.",
        "- Preserve only proper nouns, acronyms, URLs, standard identifiers, and required term targets.",
        "- Do not translate or output instruction text, context labels, term labels, examples, or metadata.",
    ]
    if any(str(reason).startswith("source_target_structure_mismatch") for reason in reasons):
        lines.append("- Preserve SOURCE_TEXT structure: keep narrative prose as prose and direct dialogue as dialogue.")
    formality = str((style_options or {}).get("formality") or "").strip()
    if formality:
        lines.append(f"- Respect the user's formality/style setting: {formality}.")
    term_lines = _rescue_document_term_lines(style_options, unit.text)
    if term_lines:
        lines.extend(
            [
                "",
                "<DOCUMENT_TERM_REQUIREMENTS>",
                *term_lines,
                "</DOCUMENT_TERM_REQUIREMENTS>",
            ]
        )
    if include_context and unit.context_text:
        lines.extend(
            [
                "",
                "<LOCAL_CONTEXT>",
                _clip_rescue_text(unit.context_text, _SINGLE_RESCUE_CONTEXT_MAX_CHARS),
                "</LOCAL_CONTEXT>",
            ]
        )
    if isinstance(unit.dialogue_hint, dict) and unit.dialogue_hint:
        lines.extend(
            [
                "",
                "<DIALOGUE_HINT>",
                json.dumps(unit.dialogue_hint, ensure_ascii=False),
                "</DIALOGUE_HINT>",
            ]
        )
        relationship_lines = _rescue_relationship_context(style_options)
        if relationship_lines:
            lines.extend(
                [
                    "",
                    "<PRE_TRANSLATION_RELATIONSHIP_CONTEXT>",
                    *relationship_lines,
                    "</PRE_TRANSLATION_RELATIONSHIP_CONTEXT>",
                ]
            )
    if reasons:
        lines.extend(["", "<VALIDATION_FAILURES>", *[str(reason) for reason in reasons], "</VALIDATION_FAILURES>"])
        forbidden_fragments = _forbidden_fragments_from_reasons(reasons)
        if forbidden_fragments:
            lines.extend(
                [
                    "",
                    "<DO_NOT_OUTPUT_AGAIN>",
                    *forbidden_fragments,
                    "</DO_NOT_OUTPUT_AGAIN>",
                    "Do not copy the exact forbidden fragments above into the target text.",
                    "Translate their meaning naturally into the target language instead.",
                ]
            )
    if previous_invalid_translation:
        lines.extend(
            [
                "",
                "<PREVIOUS_INVALID_TRANSLATION>",
                previous_invalid_translation,
                "</PREVIOUS_INVALID_TRANSLATION>",
            ]
        )
    lines.extend(
        [
            "",
            f'<SOURCE_TEXT id="{unit.translation_unit_id}">',
            unit.text,
            "</SOURCE_TEXT>",
        ]
    )
    return "\n".join(lines)


def _log_pptx_context_prompt(
    scope: str,
    units: List[TranslationUnit],
    system_prompt: str,
    user_prompt: str,
) -> None:
    log_info(
        "[PPTX 문맥 번역] "
        f"scope={scope} "
        f"items={len(units)} "
        f"targets={[[ (t.injection_unit_id, t.fragment_index, t.fragment_count) for t in unit.targets ] for unit in units[:5]]}"
    )
    if not _PPTX_CONTEXT_VERBOSE_LOG:
        return
    context_preview = ((units[0].context_text if units else "") or "").replace("\n", " ").strip()[:700]
    log_info(f"  system_prompt_chars={len(system_prompt)}")
    log_info(f"  context_preview={context_preview}")
    log_info(f"  user_prompt_chars={len(user_prompt)}")


def _safe_snapshot_part(value: Any) -> str:
    safe = re.sub(r"[^0-9A-Za-z가-힣_.() -]+", "_", str(value or "").strip())
    safe = re.sub(r"\s+", "_", safe).strip("._- ")
    return safe[:120]


def _term_sources_for_injection(entry: dict[str, Any]) -> list[str]:
    sources = [entry.get("source"), *(entry.get("source_terms") or [])]
    result: list[str] = []
    seen: set[str] = set()
    for source in sources:
        text = str(source or "").strip()
        key = normalize_document_source(text)
        if key and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _contains_in_source_text(source_text: str, source_term: str) -> bool:
    source_key = normalize_document_source(source_term)
    text_key = normalize_document_source(source_text)
    return bool(source_key and text_key and source_key in text_key)


def _contains_in_target_text(target_text: str, target_term: str) -> bool:
    target_key = normalize_document_source(target_term)
    text_key = normalize_document_source(target_text)
    return bool(target_key and text_key and target_key in text_key)


def _format_strict_document_term_violations(violations: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for violation in violations[:5]:
        source = str(violation.get("source_term") or "").strip()
        target = str(violation.get("preferred_target") or "").strip()
        if source and target:
            parts.append(f"{source}->{target}")
        elif source:
            parts.append(source)
        elif target:
            parts.append(target)
    return ", ".join(parts)


def _strict_document_term_violations(
    style_options: Dict[str, Any] | None,
    source_text: str,
    translated_text: str,
) -> list[dict[str, Any]]:
    if not isinstance(style_options, dict):
        return []
    document_term_memory = style_options.get("_document_term_memory")
    terms = document_term_memory.get("terms") if isinstance(document_term_memory, dict) else None
    if not isinstance(terms, list):
        return []
    violations: list[dict[str, Any]] = []
    for entry in terms:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("memory_kind") or "").strip().lower() == "raw_evidence_candidate":
            continue
        preferred = str(entry.get("preferred_target") or "").strip()
        if not preferred:
            continue
        if str(entry.get("preferred_application") or "").strip() == "contextual_meaning":
            continue
        policy = str(entry.get("pre_judge_inject_policy") or "").strip().lower()
        if policy in {"source_meaning_only", "blocked", "drop", "drop_candidate", "unresolved"}:
            continue
        matched_source = next(
            (
                source
                for source in _term_sources_for_injection(entry)
                if _contains_in_source_text(source_text, source)
            ),
            "",
        )
        if not matched_source:
            continue
        if _contains_in_target_text(translated_text, preferred):
            continue
        violations.append(
            {
                "source_term": matched_source,
                "preferred_target": preferred,
                "status": entry.get("status"),
                "memory_kind": entry.get("memory_kind"),
            }
        )
    return violations


def _extract_single_fallback_translation(raw: str, unit: TranslationUnit) -> str:
    """Normalize single fallback output into plain translated text."""

    text = str(raw or "").strip()
    if not text:
        return ""
    parsed = parse_json_array_response(text)
    if isinstance(parsed, list):
        candidates = [item for item in parsed if isinstance(item, dict)]
        for item in candidates:
            item_id = item.get("id")
            if str(item_id) != str(unit.translation_unit_id) and len(candidates) != 1:
                continue
            translated = item.get("t") or item.get("translation") or item.get("translated_text") or item.get("text")
            if translated is not None:
                return str(translated).strip()
        string_candidates = [item.strip() for item in parsed if isinstance(item, str) and item.strip()]
        if len(string_candidates) == 1:
            return string_candidates[0]
    if isinstance(parsed, dict):
        translated = (
            parsed.get("t")
            or parsed.get("translation")
            or parsed.get("translated_text")
            or parsed.get("text")
        )
        if translated is not None:
            return str(translated).strip()
    return text


def _single_translation_retry_reasons(
    *,
    unit: TranslationUnit,
    translated: str,
    target_lang: str,
    config: ContextTranslationConfig,
    style_options: Dict[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    if needs_context_label_retry(unit.text, translated):
        reasons.append("context_label_leaked")
    if needs_corruption_retry(unit.text, translated):
        reasons.append("corrupted_text")
    if needs_structure_retry(unit.text, translated):
        reasons.append("source_target_structure_mismatch")
    if config.enable_target_language_retry:
        reasons.extend(target_language_retry_reasons(unit.text, translated, target_lang))
    term_violations = _strict_document_term_violations(style_options, unit.text, translated)
    if term_violations:
        reasons.append(
            "strict_document_term_missing="
            + _format_strict_document_term_violations(term_violations)
        )
    if needs_formality_retry(
        translated,
        target_lang,
        str((style_options or {}).get("formality") or ""),
        element_type=unit.element_type,
    ):
        reasons.append("formal_hamnida_ending_violation")
    return reasons


def _fatal_single_translation_reasons(reasons: list[str]) -> list[str]:
    fatal_prefixes = (
        "context_label_leaked",
        "corrupted_text",
        "unexpected_foreign_script=",
        "untranslated_english_phrase=",
        "hangul_remaining",
        "han_remaining",
        "kana_remaining",
        "cyrillic_remaining",
    )
    return [
        reason
        for reason in reasons
        if any(str(reason).startswith(prefix) for prefix in fatal_prefixes)
    ]


def _injected_target_for_entry(entry: dict[str, Any]) -> tuple[str, str]:
    preferred = str(entry.get("preferred_target") or "").strip()
    if preferred:
        return preferred, "preferred"
    suggested = str(entry.get("suggested_target") or "").strip()
    if suggested:
        return suggested, "suggested"
    return "", "source_context"


def _candidate_targets_for_entry(entry: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in entry.get("candidate_targets") or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "").strip()
        key = normalize_document_source(target)
        if not key or key in seen:
            continue
        result.append(target)
        seen.add(key)
    return result


def _record_document_term_prompt_injections(
    style_options: Dict[str, Any] | None,
    batch: List[TranslationUnit],
    *,
    scope: str,
) -> None:
    if not isinstance(style_options, dict):
        return
    memory = style_options.get("_document_term_memory_memory")
    document_term_memory = style_options.get("_document_term_memory")
    if not isinstance(memory, dict) or not isinstance(document_term_memory, dict):
        return
    terms = document_term_memory.get("terms")
    if not isinstance(terms, list):
        return
    injections_by_unit = memory.setdefault("_prompt_injections_by_unit_id", {})
    if not isinstance(injections_by_unit, dict):
        injections_by_unit = {}
        memory["_prompt_injections_by_unit_id"] = injections_by_unit
    for unit in batch:
        source_text = str(unit.text or "")
        unit_id = str(unit.translation_unit_id)
        current = injections_by_unit.setdefault(unit_id, [])
        if not isinstance(current, list):
            current = []
            injections_by_unit[unit_id] = current
        seen = {
            (
                normalize_document_source(item.get("source_term")),
                normalize_document_source(item.get("injected_target")),
                str(item.get("scope") or ""),
            )
            for item in current
            if isinstance(item, dict)
        }
        for entry in terms:
            if not isinstance(entry, dict):
                continue
            matched_source = next(
                (source for source in _term_sources_for_injection(entry) if _contains_in_source_text(source_text, source)),
                "",
            )
            if not matched_source:
                continue
            injected_target, strength = _injected_target_for_entry(entry)
            candidate_targets = _candidate_targets_for_entry(entry)
            if candidate_targets and not injected_target:
                strength = "candidate_pool"
            marker = (normalize_document_source(matched_source), normalize_document_source(injected_target), scope)
            if marker in seen:
                continue
            current.append(
                {
                    "scope": scope,
                    "source_term": matched_source,
                    "entry_source": entry.get("source"),
                    "source_terms": entry.get("source_terms") or [],
                    "injected_target": injected_target or None,
                    "candidate_targets": candidate_targets,
                    "injection_strength": strength,
                    "status": entry.get("status"),
                    "memory_kind": entry.get("memory_kind"),
                    "needs_review": bool(entry.get("needs_review")),
                    "target_decision_needed": bool(entry.get("target_decision_needed")),
                    "target_language_risk": entry.get("target_language_risk") or "",
                    "preferred_application": entry.get("preferred_application") or "",
                }
            )
            seen.add(marker)


def _translation_prompt_snapshot_enabled(style_options: Dict[str, Any] | None) -> bool:
    if os.getenv("AI_TRANSLATION_PROMPT_SNAPSHOT_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return isinstance(style_options, dict) and isinstance(style_options.get("_document_term_memory_memory"), dict)


def _translation_prompt_snapshot_dir() -> Path:
    value = os.getenv("AI_TRANSLATION_TRANSLATION_PROMPT_SNAPSHOT_DIR", "").strip()
    return Path(value) if value else _DEFAULT_TRANSLATION_PROMPT_SNAPSHOT_DIR


def _save_translation_prompt_snapshot(
    *,
    style_options: Dict[str, Any] | None,
    config: ContextTranslationConfig,
    scope: str,
    batch: List[TranslationUnit],
    system_prompt: str,
    user_prompt: str,
    batch_index: int | None = None,
    batch_total: int | None = None,
) -> str:
    if not _translation_prompt_snapshot_enabled(style_options):
        return ""
    memory = style_options.get("_document_term_memory_memory") if isinstance(style_options, dict) else {}
    job_id = _safe_snapshot_part((style_options or {}).get("_job_id") or (memory or {}).get("job_id")) or f"translation-prompt-{uuid.uuid4().hex[:12]}"
    artifact = _safe_snapshot_part((style_options or {}).get("_filename") or (style_options or {}).get("_file_name") or (memory or {}).get("_artifact_label"))
    safe_scope = _safe_snapshot_part(scope)
    batch_label = f"batch{batch_index}-of-{batch_total}" if batch_index and batch_total else "single"
    stamp = int(time.time() * 1000)
    prefix = "__".join(item for item in (safe_scope, batch_label, str(stamp)) if item)
    path = job_artifact_path(
        job_id,
        artifact,
        f"{safe_artifact_part(prefix, limit=180)}.json",
        subdir="translation_prompts",
    )
    prompt_terms = []
    document_term_memory = (style_options or {}).get("_document_term_memory")
    if isinstance(document_term_memory, dict) and isinstance(document_term_memory.get("terms"), list):
        for entry in document_term_memory.get("terms") or []:
            if not isinstance(entry, dict):
                continue
            prompt_terms.append(
                {
                    "source_term": entry.get("source"),
                    "status": entry.get("status"),
                    "memory_kind": entry.get("memory_kind"),
                    "preferred_target": entry.get("preferred_target"),
                    "suggested_target": entry.get("suggested_target"),
                    "candidate_targets": entry.get("candidate_targets") or [],
                    "active_sense_id": entry.get("active_sense_id"),
                }
            )
    payload = {
        "job_id": (memory or {}).get("job_id") or (style_options or {}).get("_job_id"),
        "artifact_label": (style_options or {}).get("_filename") or (memory or {}).get("_artifact_label"),
        "doc_format": config.doc_format,
        "scope": scope,
        "batch_index": batch_index,
        "batch_total": batch_total,
        "unit_ids": [unit.translation_unit_id for unit in batch],
        "source_texts": [unit.text for unit in batch],
        "dialogue_hints": {
            str(unit.translation_unit_id): unit.dialogue_hint
            for unit in batch
            if isinstance(unit.dialogue_hint, dict) and unit.dialogue_hint
        },
        "document_term_memory_dump_path": (memory or {}).get("_dump_path"),
        "document_term_memory_resolver_dump_path": (memory or {}).get("_resolver_dump_path"),
        "document_term_memory_terms_in_source": prompt_terms,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


async def translate_contextual_units(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    translation_units: List[TranslationUnit],
    target_lang: str,
    *,
    style_options: Dict[str, Any] | None,
    config: ContextTranslationConfig,
    on_scope_started: Callable[[str], Awaitable[None]] | None = None,
    on_scope_translated: Callable[[str, Dict[int, str]], Awaitable[None]] | None = None,
    on_scope_wave_translated: Callable[[List[tuple[str, Dict[int, str]]]], Awaitable[None]] | None = None,
    on_batch_translated: Callable[[str, List[TranslationUnit], Dict[int, str]], Awaitable[None]] | None = None,
) -> Dict[int, str]:
    """포맷별 config에 따라 context-aware Office translation을 수행한다."""

    previous_by_injection_id = _previous_by_injection_id(style_options) if config.use_previous_translation else {}
    results: Dict[int, str] = {}
    grouped_units: Dict[str, List[TranslationUnit]] = defaultdict(list)
    for unit in translation_units:
        grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"].append(unit)

    for unit in translation_units:
        if not unit.text.strip() or is_symbol_junk_source(unit.text):
            results[unit.translation_unit_id] = unit.text

    async def safe_translate_single(
        unit: TranslationUnit,
        *,
        initial_repair_reasons: list[str] | None = None,
        previous_invalid_translation: str = "",
    ) -> str:
        effective_style_options = style_options_with_relevant_glossary(style_options, [unit])
        prompt = build_single_user_prompt(
            unit.text,
            source_label=config.source_label,
            target_lang=target_lang,
            context_instruction=config.context_instruction,
            extra_instruction=_extra_instruction(config, target_lang),
            style_options=effective_style_options,
            context_label=config.context_label,
            context_text=unit.context_text,
            previous_translation=_previous_text_for_unit(unit, previous_by_injection_id) if config.use_previous_translation else "",
            doc_format=config.doc_format,
            element_type=unit.element_type,
            dialogue_hint=unit.dialogue_hint,
        )
        attempts = max(1, max(_LLM_VALIDATION_RETRY_COUNT, _TARGET_LANGUAGE_VALIDATION_RETRY_COUNT) + 1)
        last_clean_translation = ""
        repair_reasons = list(initial_repair_reasons or [])
        invalid_translation = previous_invalid_translation

        async def run_rescue_stage(
            *,
            stage_name: str,
            include_context: bool,
            stage_attempts: int,
        ) -> str:
            nonlocal invalid_translation, repair_reasons, last_clean_translation
            if stage_attempts <= 0:
                return ""
            for rescue_attempt in range(stage_attempts):
                try:
                    translated = await llm_call_async(
                        sem,
                        session,
                        _rescue_translation_system_prompt(target_lang),
                        _rescue_translation_user_prompt(
                            unit,
                            target_lang=target_lang,
                            config=config,
                            style_options=effective_style_options,
                            include_context=include_context,
                            previous_invalid_translation=invalid_translation,
                            reasons=repair_reasons,
                        ),
                    )
                except Exception as exc:
                    log_info(
                        f"  {config.log_prefix} {stage_name} rescue failed: "
                        f"unit_id={unit.translation_unit_id} exc={exc}"
                    )
                    continue
                translated = _extract_single_fallback_translation(translated, unit)
                if not translated:
                    continue
                invalid_translation = translated
                retry_reasons = _single_translation_retry_reasons(
                    unit=unit,
                    translated=translated,
                    target_lang=target_lang,
                    config=config,
                    style_options=effective_style_options,
                )
                if retry_reasons:
                    log_info(
                        f"  {config.log_prefix} {stage_name} rescue retry "
                        f"{rescue_attempt + 1}/{stage_attempts}: {retry_reasons[:3]}"
                    )
                    repair_reasons = retry_reasons
                    continue
                last_clean_translation = translated
                return translated
            return ""

        for attempt in range(attempts):
            try:
                system_prompt = (
                    build_validation_retry_system_prompt("")
                    if attempt > 0
                    else ""
                )
                attempt_prompt = prompt
                if attempt > 0 or repair_reasons or invalid_translation:
                    attempt_prompt += _repair_appendix(
                        previous_translation=invalid_translation,
                        reasons=repair_reasons,
                        target_lang=target_lang,
                    )
                translated = await llm_call_async(sem, session, system_prompt, attempt_prompt)
            except Exception as exc:
                log_info(f"  {config.log_prefix} single fallback failed: {exc}")
                break
            if not translated:
                continue
            translated = _extract_single_fallback_translation(translated, unit)
            if not translated:
                continue
            invalid_translation = translated
            retry_reasons = _single_translation_retry_reasons(
                unit=unit,
                translated=translated,
                target_lang=target_lang,
                config=config,
                style_options=effective_style_options,
            )
            if retry_reasons:
                log_info(
                    f"  {config.log_prefix} single fallback retry "
                    f"{attempt + 1}/{attempts}: {retry_reasons[:3]}"
                )
                repair_reasons = retry_reasons
                continue
            last_clean_translation = translated
            return translated

        rescued = await run_rescue_stage(
            stage_name="context",
            include_context=True,
            stage_attempts=_SINGLE_RESCUE_CONTEXT_RETRY_COUNT,
        )
        if rescued:
            return rescued

        rescued = await run_rescue_stage(
            stage_name="source-only",
            include_context=False,
            stage_attempts=_SINGLE_RESCUE_SOURCE_ONLY_RETRY_COUNT,
        )
        if rescued:
            return rescued

        log_info(
            f"  {config.log_prefix} single fallback exhausted; "
            f"unit_id={unit.translation_unit_id} "
            f"reasons={repair_reasons[:3]}"
        )
        fatal_reasons = _fatal_single_translation_reasons(repair_reasons)
        if invalid_translation:
            log_info(
                f"  {config.log_prefix} accepting last fallback translation after exhausted retries "
                f"unit_id={unit.translation_unit_id} "
                f"fatal={bool(fatal_reasons)} reasons={repair_reasons[:3]}"
            )
            return invalid_translation
        if last_clean_translation:
            return last_clean_translation
        raise TranslationRescueExhaustedError(
            f"{config.log_prefix} translation rescue exhausted "
            f"unit_id={unit.translation_unit_id} "
            f"reasons={repair_reasons[:5] or ['unknown']}"
        )

    async def run_batch(
        scope: str,
        batch: List[TranslationUnit],
        *,
        batch_index: int | None = None,
        batch_total: int | None = None,
        depth: int = 0,
        branch: str = "",
    ) -> Dict[int, str]:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        if batch_index is not None and batch_total is not None:
            label = f"batch={batch_index}/{batch_total}"
        else:
            label = f"split depth={depth}{f' branch={branch}' if branch else ''}"
        unit_ids = [unit.translation_unit_id for unit in batch]
        char_count = sum(len(unit.text) + len(unit.context_text) for unit in batch)
        if config.doc_format == "docx":
            log_info(
                f"{config.log_prefix} "
                f"{label} start items={len(batch)} chars={char_count} "
                f"ids={unit_ids[:5]}{'...' if len(unit_ids) > 5 else ''}"
            )

        effective_style_options = style_options_with_relevant_glossary(style_options, batch)
        _record_document_term_prompt_injections(effective_style_options, batch, scope=scope)
        previous_items = _previous_items_for_batch(batch, previous_by_injection_id) if config.use_previous_translation else {}
        user_prompt = _batch_user_prompt(config, batch, previous_items)
        system_prompt = build_office_context_system_prompt(
            config.doc_format,
            target_lang,
            effective_style_options,
            element_type=_batch_element_type(batch),
        )
        snapshot_path = _save_translation_prompt_snapshot(
            style_options=effective_style_options,
            config=config,
            scope=scope,
            batch=batch,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            batch_index=batch_index,
            batch_total=batch_total,
        )
        if snapshot_path:
            log_info(f"{config.log_prefix} prompt snapshot saved: {snapshot_path}")
        if config.doc_format == "pptx":
            _log_pptx_context_prompt(scope, batch, system_prompt, user_prompt)
        try:
            raw = await llm_call_async(sem, session, system_prompt, user_prompt)
        except Exception as exc:
            if config.doc_format == "docx":
                log_info(
                    f"{config.log_prefix} "
                    f"{label} failed {loop.time() - started_at:.2f}s: {exc}"
                )
            else:
                log_info(f"  {config.log_prefix} batch call failed for scope={scope}: {exc}")
            raw = ""
        if not raw:
            empty_message = (
                f"{config.log_prefix} {label} empty response {loop.time() - started_at:.2f}s; "
                "splitting batch without source fallback"
                if config.doc_format == "docx"
                else f"  {config.log_prefix} empty batch response for scope={scope}; splitting without source fallback"
            )
            log_info(empty_message)
            if len(batch) > 1:
                mid = max(1, len(batch) // 2)
                left = await run_batch(scope, batch[:mid], depth=depth + 1, branch=f"{branch}L")
                right = await run_batch(scope, batch[mid:], depth=depth + 1, branch=f"{branch}R")
                return {**left, **right}
            return {
                batch[0].translation_unit_id: await safe_translate_single(
                    batch[0],
                    initial_repair_reasons=["empty_batch_response"],
                )
            }

        if config.doc_format == "pptx" and _PPTX_CONTEXT_VERBOSE_LOG:
            log_info(f"  raw_response_preview={raw[:700].replace(chr(10), ' ')}")
        parsed = parse_json_array_response(raw)
        normalized, hard_errors = validate_context_batch_items(
            parsed,
            batch,
            log_prefix=f"{config.log_prefix} {label}" if config.doc_format == "docx" else f"{config.log_prefix} scope={scope}",
        )
        should_retry = hard_errors and _LLM_VALIDATION_RETRY_COUNT > 0
        if should_retry and (not config.validation_retry_top_level_only or depth == 0):
            for attempt in range(_LLM_VALIDATION_RETRY_COUNT):
                log_info(
                    f"  {config.log_prefix} "
                    f"validation retry {attempt + 1}/{_LLM_VALIDATION_RETRY_COUNT}"
                )
                retry_raw = await llm_call_async(
                    sem,
                    session,
                    build_validation_retry_system_prompt(system_prompt),
                    user_prompt,
                )
                retry_parsed = parse_json_array_response(retry_raw)
                normalized, hard_errors = validate_context_batch_items(
                    retry_parsed,
                    batch,
                    log_prefix=f"{config.log_prefix} retry",
                )
                if not hard_errors:
                    break
        if hard_errors or not normalized:
            log_info(
                f"{config.log_prefix} "
                f"{label} parse failed {loop.time() - started_at:.2f}s; splitting batch"
                if config.doc_format == "docx"
                else f"  {config.log_prefix} batch parse failed for scope={scope}; splitting batch"
            )
            if len(batch) > 1:
                mid = max(1, len(batch) // 2)
                left = await run_batch(scope, batch[:mid], depth=depth + 1, branch=f"{branch}L")
                right = await run_batch(scope, batch[mid:], depth=depth + 1, branch=f"{branch}R")
                return {**left, **right}
            return {batch[0].translation_unit_id: await safe_translate_single(batch[0])}

        for unit in batch:
            current = normalized.get(unit.translation_unit_id)
            if current is None:
                normalized[unit.translation_unit_id] = await safe_translate_single(unit)
                continue
            corruption_retry = needs_corruption_retry(unit.text, current)
            structure_retry = needs_structure_retry(unit.text, current)
            language_reasons = (
                target_language_retry_reasons(unit.text, current, target_lang)
                if config.enable_target_language_retry
                else []
            )
            target_language_retry = bool(language_reasons)
            strict_term_violations = _strict_document_term_violations(
                effective_style_options,
                unit.text,
                current,
            )
            formality_retry = needs_formality_retry(
                current,
                target_lang,
                str((effective_style_options or {}).get("formality") or ""),
                element_type=unit.element_type,
            )
            if (
                needs_context_label_retry(unit.text, current)
                or corruption_retry
                or structure_retry
                or target_language_retry
                or strict_term_violations
                or formality_retry
            ):
                if corruption_retry:
                    log_info(
                        f"  {config.log_prefix} corrupted text retry "
                        f"scope={scope} unit_id={unit.translation_unit_id}"
                    )
                if structure_retry:
                    log_info(
                        f"  {config.log_prefix} source/target structure retry "
                        f"scope={scope} unit_id={unit.translation_unit_id}"
                    )
                if target_language_retry:
                    log_info(
                        f"  {config.log_prefix} target language script retry "
                        f"scope={scope} unit_id={unit.translation_unit_id} "
                        f"reasons={language_reasons[:3]}"
                    )
                if strict_term_violations:
                    log_info(
                        f"  {config.log_prefix} strict document term retry "
                        f"scope={scope} unit_id={unit.translation_unit_id} "
                        f"violations={strict_term_violations[:3]}"
                    )
                if formality_retry:
                    log_info(
                        f"  {config.log_prefix} formal_hamnida retry "
                        f"scope={scope} unit_id={unit.translation_unit_id}"
                    )
                repair_reasons: list[str] = []
                if corruption_retry:
                    repair_reasons.append("corrupted_text")
                if structure_retry:
                    repair_reasons.append("source_target_structure_mismatch")
                repair_reasons.extend(language_reasons)
                if strict_term_violations:
                    formatted_violations = _format_strict_document_term_violations(strict_term_violations)
                    repair_reasons.append(f"strict_document_term_missing={formatted_violations}")
                if formality_retry:
                    repair_reasons.append("formal_hamnida_ending_violation")
                normalized[unit.translation_unit_id] = await safe_translate_single(
                    unit,
                    initial_repair_reasons=repair_reasons,
                    previous_invalid_translation=current,
                )
        for duplicate_unit_id in duplicate_like_translation_unit_ids(batch, normalized):
            unit = next((item for item in batch if item.translation_unit_id == duplicate_unit_id), None)
            if unit is None:
                continue
            log_info(
                f"  {config.log_prefix} adjacent duplicate retry "
                f"scope={scope} unit_id={unit.translation_unit_id}"
            )
            normalized[unit.translation_unit_id] = await safe_translate_single(unit)
        record_observed_translations(
            temporary_glossary_memory(style_options),
            batch,
            normalized,
        )
        if config.doc_format == "docx":
            log_info(
                f"{config.log_prefix} "
                f"{label} done {loop.time() - started_at:.2f}s "
                f"items={len(batch)} translated={len(normalized)}"
            )
        return normalized

    async def translate_scope(scope: str, units: List[TranslationUnit]) -> Dict[int, str]:
        pending = [unit for unit in units if unit.text.strip() and unit.translation_unit_id not in results]
        if not pending:
            return {
                unit.translation_unit_id: unit.text
                for unit in units
                if not unit.text.strip()
            }
        batches = _split_batches(pending, config)
        if config.doc_format == "docx":
            log_info(
                f"{config.log_prefix} "
                f"{scope} {len(pending)}개 단위 -> {len(batches)}개 배치 "
                "(scope single request)"
            )
        else:
            log_info(
                f"{config.log_prefix} "
                f"{'scope=' if config.compact_log_label else ''}{scope}"
                f"{':' if not config.compact_log_label else ''} {len(pending)}개 단위 -> {len(batches)}개 배치 "
                f"(max_items={config.max_items_per_batch}, max_chars={config.max_chars_per_batch})"
            )
        if config.log_context_preview and pending:
            preview = pending[0].context_text.replace("\n", " ").strip()[:700]
            log_info(f"  context_preview={preview}")
        start = asyncio.get_running_loop().time()

        async def run_indexed_batch(index: int, batch: List[TranslationUnit]) -> Dict[int, str]:
            if config.doc_format == "pptx":
                log_info(f"{config.log_prefix} scope={scope} batch={index}/{len(batches)} start items={len(batch)}")
            result = await run_batch(scope, batch, batch_index=index, batch_total=len(batches))
            if config.doc_format == "pptx":
                log_info(
                    f"{config.log_prefix} scope={scope} batch={index}/{len(batches)} done "
                    f"{asyncio.get_running_loop().time() - start:.2f}s"
                )
            if on_batch_translated:
                await on_batch_translated(scope, batch, result)
            return result

        batch_results = await asyncio.gather(
            *[
                run_indexed_batch(index, batch)
                for index, batch in enumerate(batches, start=1)
            ]
        )
        scope_result: Dict[int, str] = {}
        for batch_result in batch_results:
            scope_result.update(batch_result)
        if config.doc_format == "docx":
            log_info(
                f"{config.log_prefix} "
                f"{scope} LLM 병렬 배치 완료: {asyncio.get_running_loop().time() - start:.2f}s"
            )
        else:
            log_info(f"{config.log_prefix} {scope} 배치 완료: {asyncio.get_running_loop().time() - start:.2f}s")
        return scope_result

    async def run_scope(scope: str) -> tuple[str, Dict[int, str]]:
        if on_scope_started:
            await on_scope_started(scope)
        scope_result = await translate_scope(scope, grouped_units[scope])
        if on_scope_translated:
            await on_scope_translated(scope, scope_result)
        return scope, scope_result

    scope_names = list(grouped_units.keys())
    if config.sort_scopes:
        scope_names = sorted(scope_names, key=scope_sort_key)

    if config.doc_format == "docx":
        pending = [unit for unit in translation_units if unit.text.strip()]
        log_info(
            f"{config.log_prefix} "
            f"{len(pending)}개 단위를 {len(scope_names)}개 scope로 나누어 "
            f"최대 {config.scope_concurrency}개 병렬 번역합니다."
        )

    should_run_concurrently = config.scope_concurrency > 1
    if config.concurrent_scopes_only_without_callbacks:
        should_run_concurrently = (
            should_run_concurrently
            and on_scope_started is None
            and on_scope_translated is None
        )
    if should_run_concurrently:
        scope_sem = asyncio.Semaphore(max(1, config.scope_concurrency))

        async def run_scope_worker(scope: str) -> tuple[str, Dict[int, str]]:
            async with scope_sem:
                return await run_scope(scope)

        if config.doc_format == "pptx":
            log_info(
                f"{config.log_prefix} "
                f"slide scope {len(scope_names)}개를 최대 "
                f"{config.scope_concurrency}개 병렬로 번역합니다."
            )
        if on_scope_wave_translated:
            wave_size = max(1, config.scope_concurrency)
            for wave_start in range(0, len(scope_names), wave_size):
                wave_scopes = scope_names[wave_start : wave_start + wave_size]
                wave_results = await asyncio.gather(
                    *[run_scope_worker(scope) for scope in wave_scopes]
                )
                for _, scope_results in wave_results:
                    results.update(scope_results)
                await on_scope_wave_translated(wave_results)
            return results
        for task in asyncio.as_completed([run_scope_worker(scope) for scope in scope_names]):
            _, scope_results = await task
            results.update(scope_results)
        return results

    for scope in scope_names:
        _, scope_result = await run_scope(scope)
        results.update(scope_result)
        if on_scope_wave_translated:
            await on_scope_wave_translated([(scope, scope_result)])
    return results
