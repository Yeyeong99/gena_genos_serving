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
    needs_context_label_retry,
    needs_target_language_retry,
    parse_json_array_response,
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
    dump_dir = _translation_prompt_snapshot_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    memory = style_options.get("_document_term_memory_memory") if isinstance(style_options, dict) else {}
    job_id = _safe_snapshot_part((style_options or {}).get("_job_id") or (memory or {}).get("job_id")) or f"translation-prompt-{uuid.uuid4().hex[:12]}"
    artifact = _safe_snapshot_part((style_options or {}).get("_filename") or (style_options or {}).get("_file_name") or (memory or {}).get("_artifact_label"))
    safe_scope = _safe_snapshot_part(scope)
    batch_label = f"batch{batch_index}-of-{batch_total}" if batch_index and batch_total else "single"
    stamp = int(time.time() * 1000)
    prefix = "__".join(item for item in (artifact, job_id, safe_scope, batch_label, str(stamp)) if item)
    path = dump_dir / f"{prefix}-translation-prompt.json"
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
) -> Dict[int, str]:
    """포맷별 config에 따라 context-aware Office translation을 수행한다."""

    previous_by_injection_id = _previous_by_injection_id(style_options) if config.use_previous_translation else {}
    results: Dict[int, str] = {}
    grouped_units: Dict[str, List[TranslationUnit]] = defaultdict(list)
    for unit in translation_units:
        grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"].append(unit)

    for unit in translation_units:
        if not unit.text.strip():
            results[unit.translation_unit_id] = unit.text

    async def safe_translate_single(unit: TranslationUnit) -> str:
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
        )
        try:
            return await llm_call_async(sem, session, "", prompt)
        except Exception as exc:
            log_info(f"  {config.log_prefix} single fallback failed: {exc}")
            return unit.text

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
                "using original text for this batch"
                if config.doc_format == "docx"
                else f"  {config.log_prefix} empty batch response for scope={scope}; using original text for this batch"
            )
            log_info(empty_message)
            return {unit.translation_unit_id: unit.text for unit in batch}

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
            if needs_context_label_retry(unit.text, current) or (
                config.enable_target_language_retry and needs_target_language_retry(unit.text, current, target_lang)
            ):
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
        pending = [unit for unit in units if unit.text.strip()]
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
