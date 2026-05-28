"""Office 문서 번역 단계 모듈."""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import aiohttp

from translation_pipeline.common.llm import (
    clear_last_llm_error,
    get_last_llm_error,
    llm_call_async,
)
from translation_pipeline.common.prompt_builder import (
    build_office_context_system_prompt,
    build_office_context_user_prompt,
    build_single_user_prompt,
    build_validation_retry_system_prompt,
)
from translation_pipeline.common.pre_translation_analysis import (
    run_pre_translation_analysis,
    save_pre_analysis_to_local_file,
)
from translation_pipeline.common.document_profile import (
    document_profile_enabled,
    get_static_document_profile,
)
from translation_pipeline.common.document_term_memory import (
    create_document_term_memory,
    document_term_memory_summary,
    find_relevant_document_terms,
    save_document_term_memory_to_local_file,
)
from translation_pipeline.common.term_extractor import scan_terms
from translation_pipeline.common.term_memory_store import (
    create_memory,
    glossary_enabled,
    memory_summary,
    update_memory_from_scan,
)
from translation_pipeline.common.term_observer import record_observed_translations
from translation_pipeline.common.validation import validate_translation_batch_response

from .types import (
    InjectionUnit,
    OfficePipelineDeps,
    OfficeTranslationArtifacts,
    ResolvedInjection,
    TranslationMap,
    TranslationUnit,
)
from .units import build_injection_units, build_translation_units, resolve_injection_units

_XLSX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_XLSX_MAX_ITEMS_PER_BATCH", "24"))
_XLSX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_XLSX_MAX_CHARS_PER_BATCH", "9000"))
_PPTX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_PPTX_MAX_ITEMS_PER_BATCH", "24"))
_PPTX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_PPTX_MAX_CHARS_PER_BATCH", "9000"))
_PPTX_CONTEXT_SCOPE_CONCURRENCY = int(os.getenv("AI_TRANSLATION_PPTX_SCOPE_CONCURRENCY", "1"))
_DOCX_CONTEXT_SCOPE_CONCURRENCY = int(os.getenv("AI_TRANSLATION_DOCX_SCOPE_CONCURRENCY", "5"))
_PPTX_CONTEXT_VERBOSE_LOG = os.getenv("AI_TRANSLATION_PPTX_CONTEXT_VERBOSE_LOG", "0") == "1"
_LLM_VALIDATION_RETRY_COUNT = int(os.getenv("AI_TRANSLATION_LLM_VALIDATION_RETRY_COUNT", "1"))
_GLOSSARY_CONTEXT_PREFIXES = ("TABLE_TITLE:", "SECTION_HEADING:", "ABBREVIATION_HINTS:")
_PRE_ANALYSIS_LIST_KEYS = (
    "source_meaning_notes",
    "acronym_notes",
)
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


def _normalize_translator_mode(value: str | None) -> str:
    """실행 환경에서 사용할 번역기 모드를 결정한다."""

    mode = (value or os.getenv("AI_TRANSLATION_TRANSLATOR_MODE", "llm")).strip().lower()
    if mode in {"llm", "mock", "noop"}:
        return mode
    return "llm"


def _is_pptx_contextual_unit(unit: TranslationUnit) -> bool:
    return unit.context_scope.startswith("pptx:slide:")


def _is_docx_contextual_unit(unit: TranslationUnit) -> bool:
    return unit.context_scope.startswith("docx:")


def _is_xlsx_contextual_unit(unit: TranslationUnit) -> bool:
    return unit.context_scope.startswith("xlsx:sheet:")


def _batch_element_type(units: List[TranslationUnit]) -> str:
    present = {unit.element_type for unit in units if unit.element_type}
    ordered = [item for item in _ELEMENT_TYPE_ORDER if item in present]
    ordered.extend(sorted(present - set(ordered)))
    return ",".join(ordered)


def _temporary_glossary_memory(style_options: Dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(style_options, dict):
        return None
    memory = style_options.get("_temporary_glossary_memory")
    return memory if isinstance(memory, dict) else None


def _document_term_memory(style_options: Dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(style_options, dict):
        return None
    memory = style_options.get("_document_term_memory_memory")
    return memory if isinstance(memory, dict) else None


def _glossary_lookup_texts(units: List[TranslationUnit]) -> List[str]:
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


def _filter_analysis_entries(entries: Any, lookup_text: str, *, limit: int = 12) -> list[dict[str, Any]]:
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


def _style_options_with_relevant_pre_analysis(
    style_options: Dict[str, Any] | None,
    units: List[TranslationUnit],
) -> Dict[str, Any] | None:
    if not isinstance(style_options, dict):
        return style_options
    analysis = style_options.get("_pre_translation_analysis")
    if not isinstance(analysis, dict):
        return style_options

    lookup_text = _normalized_match_text("\n".join(_glossary_lookup_texts(units)))
    if not lookup_text:
        return style_options

    filtered_analysis = dict(analysis)
    matched_any = False
    for key in _PRE_ANALYSIS_LIST_KEYS:
        if key not in filtered_analysis:
            continue
        filtered = _filter_analysis_entries(filtered_analysis.get(key), lookup_text)
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


def _style_options_with_relevant_glossary(
    style_options: Dict[str, Any] | None,
    units: List[TranslationUnit],
) -> Dict[str, Any] | None:
    style_options = _style_options_with_relevant_pre_analysis(style_options, units)
    document_term_memory = _document_term_memory(style_options)
    if document_term_memory:
        relevant_document_terms = find_relevant_document_terms(
            document_term_memory,
            _glossary_lookup_texts(units),
        )
        style_options = {
            **(style_options or {}),
            "_document_term_memory": {
                "terms": relevant_document_terms,
            },
        }
    return style_options


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


def _validate_context_batch_items(
    parsed_items: Any,
    batch: List[TranslationUnit],
    *,
    log_prefix: str,
) -> tuple[Dict[int, str], list[str]]:
    expected = {unit.translation_unit_id: unit.text for unit in batch}
    validation = validate_translation_batch_response(parsed_items, expected)
    if validation.hard_errors:
        log_info(
            f"{log_prefix} hard validation failed: "
            + "; ".join(validation.hard_errors[:5])
        )
    if validation.soft_warnings:
        log_info(
            f"{log_prefix} validation warnings: "
            + "; ".join(validation.soft_warnings[:5])
        )
    return validation.normalized, validation.hard_errors


def _parse_json_array_response(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except Exception:
                pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except Exception:
                return None
    return None


def _contains_hangul(text: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in text)


def _contains_latin(text: str) -> bool:
    return any(("a" <= char.lower() <= "z") for char in text)


_RETRY_TARGET_ALIASES = {
    "english": "en",
    "en": "en",
    "eng": "en",
    "영어": "en",
    "japanese": "ja",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "일본어": "ja",
    "chinese": "zh",
    "zh": "zh",
    "cn": "zh",
    "chi": "zh",
    "zho": "zh",
    "중국어": "zh",
    "korean": "ko",
    "ko": "ko",
    "kor": "ko",
    "한국어": "ko",
}


def _contains_kana(text: str) -> bool:
    for char in text:
        code = ord(char)
        if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF:
            return True
    return False


def _contains_han(text: str) -> bool:
    for char in text:
        code = ord(char)
        if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            return True
    return False


def _needs_target_language_retry(
    original: str,
    translated: str,
    target_lang: str,
) -> bool:
    """타겟 언어 대비 결과가 여전히 한국어 위주로 남아 있으면 재시도를 요청한다.

    영어 외에도 일본어/중국어 타겟에서 한국어가 그대로 남는 회귀를 막기 위해
    타겟별로 "그 언어 고유 문자가 없으면 한국어 잔존" 으로 판정한다.
    """

    target = _RETRY_TARGET_ALIASES.get((target_lang or "").strip().lower(), "")
    if target in ("", "ko"):
        return False
    if not original.strip() or not translated.strip():
        return False
    if not _contains_hangul(original):
        return False
    if not _contains_hangul(translated):
        return False

    if target == "en":
        return not _contains_latin(translated)
    if target == "ja":
        return not _contains_kana(translated)
    if target == "zh":
        return not _contains_han(translated)
    return False


def _scope_sort_key(scope: str) -> Tuple[int, str]:
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


def _is_docx_plain_unit(unit: TranslationUnit) -> bool:
    return unit.context_scope.startswith("docx:")


async def _translate_docx_units_with_context(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    translation_units: List[TranslationUnit],
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
    on_scope_started: Callable[[str], Awaitable[None]] | None = None,
    on_scope_translated: Callable[[str, Dict[int, str]], Awaitable[None]] | None = None,
) -> Dict[int, str]:
    results: Dict[int, str] = {}
    grouped_units: Dict[str, List[TranslationUnit]] = defaultdict(list)
    for unit in translation_units:
        grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"].append(unit)

    pending = [unit for unit in translation_units if unit.text.strip()]
    for unit in translation_units:
        if not unit.text.strip():
            results[unit.translation_unit_id] = unit.text

    async def _safe_translate_single(unit: TranslationUnit) -> str:
        effective_style_options = _style_options_with_relevant_glossary(style_options, [unit])
        prompt = build_single_user_prompt(
            unit.text,
            source_label="SOURCE_TEXT",
            target_lang=target_lang,
            context_instruction="Use the CONTEXT only for local meaning.",
            extra_instruction="",
            style_options=effective_style_options,
            context_label="CONTEXT",
            context_text=unit.context_text,
            previous_translation="",
            doc_format="docx",
            element_type=unit.element_type,
        )
        try:
            return await llm_call_async(sem, session, "", prompt)
        except Exception as exc:
            log_info(f"  [DOCX 문맥 번역] single fallback failed: {exc}")
            return unit.text

    def _split_batches(units: List[TranslationUnit]) -> List[List[TranslationUnit]]:
        return [units] if units else []

    async def _run_batch(
        batch: List[TranslationUnit],
        *,
        batch_index: int | None = None,
        batch_total: int | None = None,
        depth: int = 0,
        branch: str = "",
    ) -> Dict[int, str]:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        label = (
            f"batch={batch_index}/{batch_total}"
            if batch_index is not None and batch_total is not None
            else f"split depth={depth}{f' branch={branch}' if branch else ''}"
        )
        unit_ids = [unit.translation_unit_id for unit in batch]
        char_count = sum(len(unit.text) + len(unit.context_text) for unit in batch)
        log_info(
            "[DOCX 문맥 번역] "
            f"{label} start items={len(batch)} chars={char_count} "
            f"ids={unit_ids[:5]}{'...' if len(unit_ids) > 5 else ''}"
        )
        system_prompt = build_office_context_system_prompt(
            "docx",
            target_lang,
            _style_options_with_relevant_glossary(style_options, batch),
            element_type=_batch_element_type(batch),
        )
        user_prompt = build_office_context_user_prompt("docx", batch)
        try:
            raw = await llm_call_async(sem, session, system_prompt, user_prompt)
        except Exception as exc:
            log_info(
                "[DOCX 문맥 번역] "
                f"{label} failed {loop.time() - started_at:.2f}s: {exc}"
            )
            raw = ""
        if not raw:
            log_info(
                "[DOCX 문맥 번역] "
                f"{label} empty response {loop.time() - started_at:.2f}s; "
                "using original text for this batch"
            )
            return {unit.translation_unit_id: unit.text for unit in batch}

        parsed = _parse_json_array_response(raw)
        normalized, hard_errors = _validate_context_batch_items(
            parsed,
            batch,
            log_prefix=f"[DOCX 문맥 번역] {label}",
        )
        if hard_errors and depth == 0 and _LLM_VALIDATION_RETRY_COUNT > 0:
            for attempt in range(_LLM_VALIDATION_RETRY_COUNT):
                log_info(
                    "[DOCX 문맥 번역] "
                    f"{label} validation retry {attempt + 1}/{_LLM_VALIDATION_RETRY_COUNT}"
                )
                retry_raw = await llm_call_async(
                    sem,
                    session,
                    build_validation_retry_system_prompt(system_prompt),
                    user_prompt,
                )
                retry_parsed = _parse_json_array_response(retry_raw)
                normalized, hard_errors = _validate_context_batch_items(
                    retry_parsed,
                    batch,
                    log_prefix=f"[DOCX 문맥 번역] {label} retry",
                )
                if not hard_errors:
                    break
        if hard_errors or not normalized:
            log_info(
                "[DOCX 문맥 번역] "
                f"{label} parse failed {loop.time() - started_at:.2f}s; splitting batch"
            )
            if len(batch) > 1:
                mid = max(1, len(batch) // 2)
                left = await _run_batch(batch[:mid], depth=depth + 1, branch=f"{branch}L")
                right = await _run_batch(batch[mid:], depth=depth + 1, branch=f"{branch}R")
                return {**left, **right}
            return {batch[0].translation_unit_id: await _safe_translate_single(batch[0])}

        for unit in batch:
            if unit.translation_unit_id not in normalized:
                normalized[unit.translation_unit_id] = await _safe_translate_single(unit)
        record_observed_translations(
            _temporary_glossary_memory(style_options),
            batch,
            normalized,
        )
        log_info(
            "[DOCX 문맥 번역] "
            f"{label} done {loop.time() - started_at:.2f}s "
            f"items={len(batch)} translated={len(normalized)}"
        )
        return normalized

    async def _translate_scope(scope: str, units: List[TranslationUnit]) -> Dict[int, str]:
        pending_units = [unit for unit in units if unit.text.strip()]
        if not pending_units:
            return {
                unit.translation_unit_id: unit.text
                for unit in units
                if not unit.text.strip()
            }

        batches = _split_batches(pending_units)
        log_info(
            "[DOCX 문맥 번역] "
            f"{scope} {len(pending_units)}개 단위 -> {len(batches)}개 배치 "
            "(scope single request)"
        )
        start = asyncio.get_running_loop().time()
        batch_total = len(batches)
        batch_results = await asyncio.gather(
            *[
                _run_batch(batch, batch_index=index, batch_total=batch_total)
                for index, batch in enumerate(batches, start=1)
            ]
        )
        log_info(
            "[DOCX 문맥 번역] "
            f"{scope} LLM 병렬 배치 완료: {asyncio.get_running_loop().time() - start:.2f}s"
        )
        scope_result: Dict[int, str] = {}
        for batch_result in batch_results:
            scope_result.update(batch_result)
        return scope_result

    scope_names = sorted(grouped_units.keys(), key=_scope_sort_key)
    log_info(
        "[DOCX 문맥 번역] "
        f"{len(pending)}개 단위를 {len(scope_names)}개 scope로 나누어 "
        f"최대 {_DOCX_CONTEXT_SCOPE_CONCURRENCY}개 병렬 번역합니다."
    )

    scope_sem = asyncio.Semaphore(max(1, _DOCX_CONTEXT_SCOPE_CONCURRENCY))

    async def _run_scope_worker(scope: str) -> tuple[str, Dict[int, str]]:
        async with scope_sem:
            if on_scope_started:
                await on_scope_started(scope)
            scope_results = await _translate_scope(scope, grouped_units[scope])
            if on_scope_translated:
                await on_scope_translated(scope, scope_results)
            return scope, scope_results

    tasks = [_run_scope_worker(scope) for scope in scope_names]
    for task in asyncio.as_completed(tasks):
        _scope, scope_results = await task
        results.update(scope_results)
    return results


async def _translate_xlsx_units_with_context(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    translation_units: List[TranslationUnit],
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
    on_scope_started: Callable[[str], Awaitable[None]] | None = None,
    on_scope_translated: Callable[[str, Dict[int, str]], Awaitable[None]] | None = None,
) -> Dict[int, str]:
    previous_by_injection_id = (
        style_options.get("_previous_translation_by_injection_id")
        if isinstance(style_options, dict)
        else None
    )
    if not isinstance(previous_by_injection_id, dict):
        previous_by_injection_id = {}
    results: Dict[int, str] = {}
    grouped_units: Dict[str, List[TranslationUnit]] = defaultdict(list)
    for unit in translation_units:
        grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"].append(unit)

    for unit in translation_units:
        if not unit.text.strip():
            results[unit.translation_unit_id] = unit.text

    async def _safe_translate_single(unit: TranslationUnit) -> str:
        effective_style_options = _style_options_with_relevant_glossary(style_options, [unit])
        previous_text = ""
        for target in unit.targets:
            previous = previous_by_injection_id.get(target.injection_unit_id)
            if previous:
                previous_text = str(previous)
                break
        prompt = build_single_user_prompt(
            unit.text,
            source_label="CELL_TEXT",
            target_lang=target_lang,
            context_instruction="Use CELL_CONTEXT only to understand the spreadsheet table.",
            extra_instruction=(
                "Do not infer script labels from nearby values. "
                f"Translate Korean/Hanja currency display text into natural {target_lang}."
            ),
            style_options=effective_style_options,
            context_label="CELL_CONTEXT",
            context_text=unit.context_text,
            previous_translation=previous_text,
            doc_format="xlsx",
            element_type=unit.element_type,
        )
        try:
            return await llm_call_async(sem, session, "", prompt)
        except Exception as exc:
            log_info(f"  [XLSX 문맥 번역] single fallback failed: {exc}")
            return unit.text

    def _split_batches(units: List[TranslationUnit]) -> List[List[TranslationUnit]]:
        batches: List[List[TranslationUnit]] = []
        current: List[TranslationUnit] = []
        current_chars = 0
        for unit in units:
            estimated_chars = len(unit.text) + len(unit.context_text) + 100
            if current and (
                len(current) >= _XLSX_CONTEXT_MAX_ITEMS_PER_BATCH
                or current_chars + estimated_chars > _XLSX_CONTEXT_MAX_CHARS_PER_BATCH
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(unit)
            current_chars += estimated_chars
        if current:
            batches.append(current)
        return batches

    async def _run_batch(batch: List[TranslationUnit]) -> Dict[int, str]:
        effective_style_options = _style_options_with_relevant_glossary(style_options, batch)
        previous_items: Dict[int, str] = {}
        for unit in batch:
            for target in unit.targets:
                previous = previous_by_injection_id.get(target.injection_unit_id)
                if previous:
                    previous_items[unit.translation_unit_id] = str(previous)
                    break
        user_prompt = build_office_context_user_prompt(
            "xlsx",
            batch,
            previous_items=previous_items,
        )
        system_prompt = build_office_context_system_prompt(
            "xlsx",
            target_lang,
            effective_style_options,
            element_type=_batch_element_type(batch),
        )
        try:
            raw = await llm_call_async(sem, session, system_prompt, user_prompt)
        except Exception as exc:
            log_info(f"  [XLSX 문맥 번역] batch call failed: {exc}")
            raw = ""
        if not raw:
            log_info("  [XLSX 문맥 번역] empty batch response; using original text for this batch")
            return {unit.translation_unit_id: unit.text for unit in batch}

        parsed = _parse_json_array_response(raw)
        normalized, hard_errors = _validate_context_batch_items(
            parsed,
            batch,
            log_prefix="[XLSX 문맥 번역]",
        )
        if hard_errors and _LLM_VALIDATION_RETRY_COUNT > 0:
            for attempt in range(_LLM_VALIDATION_RETRY_COUNT):
                log_info(
                    "  [XLSX 문맥 번역] "
                    f"validation retry {attempt + 1}/{_LLM_VALIDATION_RETRY_COUNT}"
                )
                retry_raw = await llm_call_async(
                    sem,
                    session,
                    build_validation_retry_system_prompt(system_prompt),
                    user_prompt,
                )
                retry_parsed = _parse_json_array_response(retry_raw)
                normalized, hard_errors = _validate_context_batch_items(
                    retry_parsed,
                    batch,
                    log_prefix="[XLSX 문맥 번역] retry",
                )
                if not hard_errors:
                    break
        if hard_errors or not normalized:
            log_info("  [XLSX 문맥 번역] batch parse failed; splitting batch")
            if len(batch) > 1:
                mid = max(1, len(batch) // 2)
                left = await _run_batch(batch[:mid])
                right = await _run_batch(batch[mid:])
                return {**left, **right}
            return {batch[0].translation_unit_id: await _safe_translate_single(batch[0])}

        for unit in batch:
            current = normalized.get(unit.translation_unit_id)
            if current is None:
                normalized[unit.translation_unit_id] = await _safe_translate_single(unit)
                continue
            if _needs_target_language_retry(unit.text, current, target_lang):
                normalized[unit.translation_unit_id] = await _safe_translate_single(unit)
        record_observed_translations(
            _temporary_glossary_memory(style_options),
            batch,
            normalized,
        )
        return normalized

    async def _run_scope(scope: str, units: List[TranslationUnit]) -> Dict[int, str]:
        if on_scope_started:
            await on_scope_started(scope)
        pending = [unit for unit in units if unit.text.strip()]
        batches = _split_batches(pending)
        log_info(
            "[XLSX 문맥 번역] "
            f"{scope}: {len(pending)}개 셀 -> {len(batches)}개 배치 "
            f"(max_items={_XLSX_CONTEXT_MAX_ITEMS_PER_BATCH}, "
            f"max_chars={_XLSX_CONTEXT_MAX_CHARS_PER_BATCH})"
        )
        if pending:
            preview = pending[0].context_text.replace("\n", " ").strip()[:700]
            log_info(f"  context_preview={preview}")
        start = asyncio.get_running_loop().time()
        batch_results = await asyncio.gather(*[_run_batch(batch) for batch in batches])
        scope_result: Dict[int, str] = {}
        for batch_result in batch_results:
            scope_result.update(batch_result)
        log_info(f"[XLSX 문맥 번역] {scope} 배치 완료: {asyncio.get_running_loop().time() - start:.2f}s")
        if on_scope_translated:
            await on_scope_translated(scope, scope_result)
        return scope_result

    for scope, scoped_units in grouped_units.items():
        scope_result = await _run_scope(scope, scoped_units)
        results.update(scope_result)
    return results


async def _translate_pptx_units_with_context(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    translation_units: List[TranslationUnit],
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
    on_scope_started: Callable[[str], Awaitable[None]] | None = None,
    on_scope_translated: Callable[[str, Dict[int, str]], Awaitable[None]] | None = None,
) -> Dict[int, str]:
    previous_by_injection_id = (
        style_options.get("_previous_translation_by_injection_id")
        if isinstance(style_options, dict)
        else None
    )
    if not isinstance(previous_by_injection_id, dict):
        previous_by_injection_id = {}
    grouped_units: Dict[str, List[TranslationUnit]] = defaultdict(list)
    for unit in translation_units:
        grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"].append(unit)

    async def _safe_translate_single(unit: TranslationUnit) -> str:
        effective_style_options = _style_options_with_relevant_glossary(style_options, [unit])
        prompt = build_single_user_prompt(
            unit.text,
            source_label="TARGET_TEXT",
            target_lang=target_lang,
            context_instruction="Use the CONTEXT only to understand the presentation item.",
            extra_instruction="Keep slide labels and table cells compact.",
            style_options=effective_style_options,
            context_label="CONTEXT",
            context_text=unit.context_text,
            previous_translation="",
            doc_format="pptx",
            element_type=unit.element_type,
        )
        try:
            return await llm_call_async(sem, session, "", prompt)
        except Exception as exc:
            log_info(f"  [PPTX 문맥 번역] single fallback failed: {exc}")
            return unit.text

    async def translate_batch(scope: str, units: List[TranslationUnit]) -> Dict[int, str]:
        results: Dict[int, str] = {}
        pending = [unit for unit in units if unit.text.strip()]
        for unit in units:
            if not unit.text.strip():
                results[unit.translation_unit_id] = unit.text

        def _split_batches(batch_units: List[TranslationUnit]) -> List[List[TranslationUnit]]:
            batches: List[List[TranslationUnit]] = []
            current: List[TranslationUnit] = []
            current_chars = 0
            current_context = ""
            for unit in batch_units:
                unit_context = unit.context_text or ""
                context_chars = 0 if current and unit_context == current_context else len(unit_context)
                estimated_chars = len(unit.text) + context_chars + 100
                if current and (
                    len(current) >= _PPTX_CONTEXT_MAX_ITEMS_PER_BATCH
                    or current_chars + estimated_chars > _PPTX_CONTEXT_MAX_CHARS_PER_BATCH
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

        async def run_one_batch(batch: List[TranslationUnit]) -> Dict[int, str]:
            effective_style_options = _style_options_with_relevant_glossary(style_options, batch)
            target_items = [(unit.translation_unit_id, unit.text) for unit in batch]
            previous_items: Dict[int, str] = {}
            for unit in batch:
                for target in unit.targets:
                    previous = previous_by_injection_id.get(target.injection_unit_id)
                    if previous:
                        previous_items[unit.translation_unit_id] = str(previous)
                        break
            user_prompt = build_office_context_user_prompt(
                "pptx",
                context_text=batch[0].context_text or "",
                target_items=target_items,
                previous_items=previous_items,
            )
            system_prompt = build_office_context_system_prompt(
                "pptx",
                target_lang,
                effective_style_options,
                element_type=_batch_element_type(batch),
            )
            _log_pptx_context_prompt(scope, batch, system_prompt, user_prompt)
            try:
                raw = await llm_call_async(sem, session, system_prompt, user_prompt)
            except Exception as exc:
                log_info(f"  [PPTX 문맥 번역] batch call failed for scope={scope}: {exc}")
                raw = ""
            if not raw:
                log_info(
                    f"  [PPTX 문맥 번역] empty batch response for scope={scope}; "
                    "using original text for this batch"
                )
                return {unit.translation_unit_id: unit.text for unit in batch}
            if _PPTX_CONTEXT_VERBOSE_LOG:
                log_info(f"  raw_response_preview={raw[:700].replace(chr(10), ' ')}")
            parsed = _parse_json_array_response(raw)
            normalized, hard_errors = _validate_context_batch_items(
                parsed,
                batch,
                log_prefix=f"[PPTX 문맥 번역] scope={scope}",
            )
            if hard_errors and _LLM_VALIDATION_RETRY_COUNT > 0:
                for attempt in range(_LLM_VALIDATION_RETRY_COUNT):
                    log_info(
                        f"  [PPTX 문맥 번역] scope={scope} "
                        f"validation retry {attempt + 1}/{_LLM_VALIDATION_RETRY_COUNT}"
                    )
                    retry_raw = await llm_call_async(
                        sem,
                        session,
                        build_validation_retry_system_prompt(system_prompt),
                        user_prompt,
                    )
                    retry_parsed = _parse_json_array_response(retry_raw)
                    normalized, hard_errors = _validate_context_batch_items(
                        retry_parsed,
                        batch,
                        log_prefix=f"[PPTX 문맥 번역] scope={scope} retry",
                    )
                    if not hard_errors:
                        break
            if hard_errors or not normalized:
                log_info(f"  [PPTX 문맥 번역] batch parse failed for scope={scope}; splitting batch")
                if len(batch) > 1:
                    mid = max(1, len(batch) // 2)
                    left = await run_one_batch(batch[:mid])
                    right = await run_one_batch(batch[mid:])
                    return {**left, **right}
                return {
                    batch[0].translation_unit_id: await _safe_translate_single(batch[0])
                }
            for unit in batch:
                current = normalized.get(unit.translation_unit_id)
                if current is None:
                    normalized[unit.translation_unit_id] = await _safe_translate_single(unit)
                    continue
                if _needs_target_language_retry(unit.text, current, target_lang):
                    normalized[unit.translation_unit_id] = await _safe_translate_single(unit)
            record_observed_translations(
                _temporary_glossary_memory(style_options),
                batch,
                normalized,
            )
            return normalized

        if pending:
            batches = _split_batches(pending)
            log_info(
                "[PPTX 문맥 번역] "
                f"scope={scope} {len(pending)}개 단위 -> {len(batches)}개 배치 "
                f"(max_items={_PPTX_CONTEXT_MAX_ITEMS_PER_BATCH}, "
                f"max_chars={_PPTX_CONTEXT_MAX_CHARS_PER_BATCH})"
            )

            async def _run_indexed_batch(index: int, batch: List[TranslationUnit]) -> Dict[int, str]:
                start = asyncio.get_running_loop().time()
                log_info(f"[PPTX 문맥 번역] scope={scope} batch={index}/{len(batches)} start items={len(batch)}")
                result = await run_one_batch(batch)
                log_info(
                    f"[PPTX 문맥 번역] scope={scope} batch={index}/{len(batches)} done "
                    f"{asyncio.get_running_loop().time() - start:.2f}s"
                )
                return result

            batch_results = await asyncio.gather(
                *[_run_indexed_batch(index, batch) for index, batch in enumerate(batches, start=1)]
            )
            for batch_result in batch_results:
                results.update(batch_result)
        return results

    async def _run_scope(scope: str, units: List[TranslationUnit]) -> Dict[int, str]:
        if on_scope_started:
            await on_scope_started(scope)
        scope_result = await translate_batch(scope, units)
        if on_scope_translated:
            await on_scope_translated(scope, scope_result)
        return scope_result

    sorted_scopes = sorted(grouped_units.keys(), key=_scope_sort_key)
    merged: Dict[int, str] = {}

    if (
        _PPTX_CONTEXT_SCOPE_CONCURRENCY > 1
        and on_scope_started is None
        and on_scope_translated is None
    ):
        scope_sem = asyncio.Semaphore(max(1, _PPTX_CONTEXT_SCOPE_CONCURRENCY))

        async def _run_scope_worker(scope: str) -> tuple[str, Dict[int, str]]:
            async with scope_sem:
                return scope, await _run_scope(scope, grouped_units[scope])

        tasks = [asyncio.create_task(_run_scope_worker(scope)) for scope in sorted_scopes]
        log_info(
            "[PPTX 문맥 번역] "
            f"slide scope {len(sorted_scopes)}개를 최대 "
            f"{_PPTX_CONTEXT_SCOPE_CONCURRENCY}개 병렬로 번역합니다."
        )
        for task in asyncio.as_completed(tasks):
            _, result = await task
            merged.update(result)
        return merged

    for scope in sorted_scopes:
        result = await _run_scope(scope, grouped_units[scope])
        merged.update(result)
    return merged


async def _translate_units_with_mode(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    translation_units: List[TranslationUnit],
    target_lang: str,
    deps: OfficePipelineDeps,
    translator_mode: str | None = None,
    style_options: Dict[str, Any] | None = None,
    on_scope_started: Callable[[str], Awaitable[None]] | None = None,
    on_scope_translated: Callable[[str, Dict[int, str]], Awaitable[None]] | None = None,
) -> tuple[TranslationMap, Dict[int, str], str]:
    """번역 단위 목록을 선택된 번역기 모드로 처리한다."""

    mode = _normalize_translator_mode(translator_mode)
    if mode == "noop":
        translated_by_unit_id = {
            unit.translation_unit_id: unit.text for unit in translation_units
        }
        trans_map = {unit.text: unit.text for unit in translation_units}
        return trans_map, translated_by_unit_id, ""

    if mode == "mock":
        translated_by_unit_id = {}
        trans_map = {}
        for unit in translation_units:
            translated = f"[{target_lang}] {unit.text}" if unit.text.strip() else unit.text
            translated_by_unit_id[unit.translation_unit_id] = translated
            trans_map[unit.text] = translated
        record_observed_translations(
            _temporary_glossary_memory(style_options),
            translation_units,
            translated_by_unit_id,
        )
        if on_scope_translated:
            grouped_units: Dict[str, Dict[int, str]] = defaultdict(dict)
            for unit in translation_units:
                grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"][
                    unit.translation_unit_id
                ] = translated_by_unit_id[unit.translation_unit_id]
            for scope in sorted(grouped_units.keys(), key=_scope_sort_key):
                if on_scope_started:
                    await on_scope_started(scope)
                await on_scope_translated(scope, grouped_units[scope])
        return trans_map, translated_by_unit_id, ""

    clear_last_llm_error()
    pptx_contextual_units = [unit for unit in translation_units if _is_pptx_contextual_unit(unit)]
    docx_contextual_units = [unit for unit in translation_units if _is_docx_contextual_unit(unit)]
    xlsx_contextual_units = [unit for unit in translation_units if _is_xlsx_contextual_unit(unit)]
    plain_units = [
        unit
        for unit in translation_units
        if not _is_pptx_contextual_unit(unit)
        and not _is_docx_contextual_unit(unit)
        and not _is_xlsx_contextual_unit(unit)
    ]

    trans_map: TranslationMap = {}
    translated_by_unit_id: Dict[int, str] = {}

    other_plain_units = plain_units

    if other_plain_units:
        other_plain_texts = [unit.text for unit in other_plain_units]
        other_style_options = _style_options_with_relevant_glossary(style_options, other_plain_units)
        other_trans_map = await deps.batch_translate_async(
            sem,
            session,
            other_plain_texts,
            target_lang,
            style_options=other_style_options,
        )
        trans_map.update(other_trans_map)
        translated_by_unit_id.update(
            {
                unit.translation_unit_id: other_trans_map.get(unit.text, unit.text)
                for unit in other_plain_units
            }
        )
        record_observed_translations(
            _temporary_glossary_memory(style_options),
            other_plain_units,
            translated_by_unit_id,
        )

    if pptx_contextual_units:
        contextual_translations = await _translate_pptx_units_with_context(
            sem,
            session,
            pptx_contextual_units,
            target_lang,
            style_options=style_options,
            on_scope_started=on_scope_started,
            on_scope_translated=on_scope_translated,
        )
        for unit in pptx_contextual_units:
            translated = contextual_translations.get(unit.translation_unit_id, unit.text)
            translated_by_unit_id[unit.translation_unit_id] = translated
            trans_map[unit.text] = translated

    if docx_contextual_units:
        docx_contextual_translations = await _translate_docx_units_with_context(
            sem,
            session,
            docx_contextual_units,
            target_lang,
            style_options=style_options,
            on_scope_started=on_scope_started,
            on_scope_translated=on_scope_translated,
        )
        for unit in docx_contextual_units:
            translated = docx_contextual_translations.get(unit.translation_unit_id, unit.text)
            translated_by_unit_id[unit.translation_unit_id] = translated
            trans_map[unit.text] = translated

    if xlsx_contextual_units:
        xlsx_contextual_translations = await _translate_xlsx_units_with_context(
            sem,
            session,
            xlsx_contextual_units,
            target_lang,
            style_options=style_options,
            on_scope_started=on_scope_started,
            on_scope_translated=on_scope_translated,
        )
        for unit in xlsx_contextual_units:
            translated = xlsx_contextual_translations.get(unit.translation_unit_id, unit.text)
            translated_by_unit_id[unit.translation_unit_id] = translated
            trans_map[unit.text] = translated

    translation_error = ""
    all_unit_texts = [unit.text for unit in translation_units]
    plain_texts = [unit.text for unit in plain_units]
    if plain_texts and all((trans_map.get(item, item) == item) for item in plain_texts):
        translation_error = get_last_llm_error()
    elif all_unit_texts and all(
        translated_by_unit_id.get(unit.translation_unit_id, unit.text) == unit.text
        for unit in translation_units
    ):
        translation_error = get_last_llm_error()
    return trans_map, translated_by_unit_id, translation_error


def _build_pairs_from_resolved(
    injection_units: List[InjectionUnit],
    resolved_injections: List[ResolvedInjection],
) -> List[dict]:
    """주입 단위 기준으로 원문/번역 pair 목록을 만든다."""

    resolved_by_injection_id = {
        item.injection_unit_id: item for item in resolved_injections
    }
    pairs: List[dict] = []
    for injection in injection_units:
        resolved = resolved_by_injection_id.get(injection.injection_unit_id)
        translated = injection.text if resolved is None else resolved.translated_text
        pairs.append(
            {
                "id": injection.node_id,
                "original": injection.text,
                "translated": translated,
                "type": injection.node.get("type", ""),
                "source": injection.source,
                "group": injection.group,
            }
        )
    return pairs


async def translate_office_nodes(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    nodes: list[dict],
    target_lang: str,
    deps: OfficePipelineDeps,
    translator_mode: str | None = None,
    style_options: Dict[str, Any] | None = None,
    on_scope_started: Callable[[str], Awaitable[None]] | None = None,
    on_scope_translated: Callable[[str, List[ResolvedInjection]], Awaitable[None]] | None = None,
    on_temporary_glossary_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_pre_translation_analysis: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_document_term_memory_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> OfficeTranslationArtifacts:
    """추출된 Office 노드를 번역/주입 분리 구조로 처리한다."""

    injection_units = build_injection_units(nodes)
    translation_units = build_translation_units(injection_units)
    effective_style_options = style_options
    if (
        document_profile_enabled(style_options)
        and not (
            isinstance(effective_style_options, dict)
            and isinstance(effective_style_options.get("_source_document_profile"), dict)
        )
    ):
        source_document_profile = get_static_document_profile(
            style_options,
            target_lang=target_lang,
        )
        if source_document_profile:
            effective_style_options = {
                **(effective_style_options or {}),
                "_source_document_profile": source_document_profile,
            }
    temporary_glossary_memory: dict[str, Any] | None = _temporary_glossary_memory(style_options)
    document_term_memory: dict[str, Any] | None = _document_term_memory(style_options)
    if glossary_enabled(style_options):
        memory_stage_start = time.perf_counter()
        pre_analysis: dict[str, Any] | None = None
        if temporary_glossary_memory is None:
            scan_stage_start = time.perf_counter()
            temporary_glossary_memory = create_memory(target_lang=target_lang)
            scan_result = scan_terms(
                translation_units,
                injection_units,
                target_lang=target_lang,
            )
            update_memory_from_scan(temporary_glossary_memory, scan_result)
            log_info(
                "[Temporary Glossary] scan complete "
                f"{memory_summary(temporary_glossary_memory)} "
                f"elapsed={time.perf_counter() - scan_stage_start:.2f}s"
            )
        effective_style_options = {
            **(effective_style_options or {}),
            "_temporary_glossary_memory": temporary_glossary_memory,
        }
        analysis_stage_start = time.perf_counter()
        pre_analysis = await run_pre_translation_analysis(
            sem,
            session,
            temporary_glossary_memory,
            target_lang=target_lang,
            style_options=effective_style_options,
        )
        log_info(
            "[Pre-Translation Analysis] stage elapsed "
            f"{time.perf_counter() - analysis_stage_start:.2f}s"
        )
        style_job_id = (
            effective_style_options.get("_job_id")
            if isinstance(effective_style_options, dict)
            else ""
        )
        job_id = str(
            (pre_analysis.get("job_id") if isinstance(pre_analysis, dict) else "")
            or temporary_glossary_memory.get("job_id")
            or style_job_id
            or ""
        )
        if pre_analysis:
            dump_path = save_pre_analysis_to_local_file(job_id, pre_analysis)
            pre_analysis["_dump_path"] = dump_path or None
            effective_style_options = {
                **(effective_style_options or {}),
                "_pre_translation_analysis": pre_analysis,
            }
            if on_pre_translation_analysis:
                await on_pre_translation_analysis(pre_analysis)
        if document_term_memory is None:
            dtm_stage_start = time.perf_counter()
            document_term_memory = create_document_term_memory(
                pre_analysis,
                job_id=job_id,
                target_lang=target_lang,
                evidence_memory=temporary_glossary_memory,
            )
            if document_term_memory:
                term_memory_path = save_document_term_memory_to_local_file(
                    job_id,
                    document_term_memory,
                )
                document_term_memory["_dump_path"] = term_memory_path or None
                effective_style_options = {
                    **(effective_style_options or {}),
                    "_document_term_memory_memory": document_term_memory,
                }
                log_info(
                    "[Document Term Memory] initialized "
                    f"{document_term_memory_summary(document_term_memory)}"
                    f"{f' dump_path={term_memory_path}' if term_memory_path else ''} "
                    f"elapsed={time.perf_counter() - dtm_stage_start:.2f}s"
                )
                if on_document_term_memory_update:
                    await on_document_term_memory_update(document_term_memory)
        log_info(
            "[Document Term Memory] setup elapsed "
            f"{time.perf_counter() - memory_stage_start:.2f}s"
        )
    else:
        pre_analysis = None
    previous_by_node_id = (
        effective_style_options.get("_previous_translation_by_node_id")
        if isinstance(effective_style_options, dict)
        else None
    )
    if isinstance(previous_by_node_id, dict):
        previous_by_injection_id: Dict[int, str] = {}
        for injection in injection_units:
            previous = previous_by_node_id.get(injection.node_id)
            if previous:
                previous_by_injection_id[injection.injection_unit_id] = str(previous)
        effective_style_options = {
            **(effective_style_options or {}),
            "_previous_translation_by_injection_id": previous_by_injection_id,
        }

    translated_snapshot_by_unit_id: Dict[int, str] = {}

    async def _handle_scope_translated(scope: str, scope_translations: Dict[int, str]) -> None:
        translated_snapshot_by_unit_id.update(scope_translations)
        if not on_scope_translated:
            return
        partial_resolved = resolve_injection_units(
            injection_units,
            translation_units,
            translated_snapshot_by_unit_id,
        )
        await on_scope_translated(scope, partial_resolved)

    trans_map, translated_by_unit_id, translation_error = await _translate_units_with_mode(
        sem,
        session,
        translation_units,
        target_lang,
        deps,
        translator_mode=translator_mode,
        style_options=effective_style_options,
        on_scope_started=on_scope_started,
        on_scope_translated=_handle_scope_translated if on_scope_translated else None,
    )
    resolved_injections = resolve_injection_units(
        injection_units,
        translation_units,
        translated_by_unit_id,
    )
    pairs = _build_pairs_from_resolved(injection_units, resolved_injections)
    text = "\n".join(item.translated_text for item in resolved_injections)

    return OfficeTranslationArtifacts(
        pairs=pairs,
        text=text,
        trans_map=trans_map,
        injection_units=injection_units,
        translation_units=translation_units,
        translated_by_unit_id=translated_by_unit_id,
        resolved_injections=resolved_injections,
        translation_error=translation_error,
        temporary_glossary=temporary_glossary_memory,
        pre_translation_analysis=pre_analysis,
        document_term_memory=document_term_memory,
    )
