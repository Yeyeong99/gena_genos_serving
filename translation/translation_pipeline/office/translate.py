"""Office 문서 번역 단계 모듈."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Dict, List

import aiohttp

from translation_pipeline.common.bilingual_summary_memory import (
    bilingual_summary_memory_enabled,
    bilingual_summary_memory_is_enabled,
    save_bilingual_summary_memory_to_local_file,
    update_bilingual_summary_memory,
)
from translation_pipeline.common.document_term_memory import (
    document_term_memory_summary,
    save_document_term_resolver_snapshot_to_local_file,
)
from translation_pipeline.common.document_term_memory_resolver import (
    resolve_document_term_memory_actions,
)
from translation_pipeline.common.logging_utils import log_info

from .translation_memory import setup_translation_memory
from .translation_modes import translate_units_with_mode
from .translation_validation import (
    needs_context_label_retry,
    needs_corruption_retry,
    needs_formality_retry,
    target_language_retry_reasons,
)
from .types import (
    InjectionUnit,
    OfficePipelineDeps,
    OfficeTranslationArtifacts,
    ResolvedInjection,
    TranslationUnit,
)
from .units import build_injection_units, build_translation_units, resolve_injection_units


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


def _term_resolver_enabled(
    style_options: Dict[str, Any] | None,
    translator_mode: str | None,
) -> bool:
    mode = str(translator_mode or "").strip().lower()
    if mode in {"mock", "noop", "same_language"}:
        return False
    env_value = os.getenv("AI_TRANSLATION_TERM_RESOLVER_ENABLED", "0").strip().lower()
    if env_value in {"0", "false", "no", "off"}:
        return False
    if isinstance(style_options, dict):
        value = style_options.get("term_resolver_enabled")
        if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
            return False
        if value is False:
            return False
    return True


def _translation_safe_for_summary_memory(
    unit: TranslationUnit,
    translated: str,
    target_lang: str,
    style_options: Dict[str, Any] | None,
) -> bool:
    text = str(translated or "").strip()
    if not text:
        return False
    if text.startswith("[번역 실패") or text.startswith("[Translation failed"):
        return False
    if text == str(unit.text or "").strip():
        return False
    if needs_context_label_retry(unit.text, text):
        return False
    if needs_corruption_retry(unit.text, text):
        return False
    if target_language_retry_reasons(unit.text, text, target_lang):
        return False
    if needs_formality_retry(
        text,
        target_lang,
        str((style_options or {}).get("formality") or ""),
        element_type=unit.element_type,
    ):
        return False
    return True


def _docx_neighbor_context_enabled(style_options: Dict[str, Any] | None) -> bool:
    value = os.getenv("AI_TRANSLATION_DOCX_DISABLE_LOCAL_CONTEXT_WHEN_SUMMARY_MEMORY", "0").strip().lower()
    if value not in {"1", "true", "yes", "on"}:
        return True
    return not bilingual_summary_memory_enabled(style_options)


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
    on_scope_batch_translated: Callable[[str, set[int], Dict[int, str]], Awaitable[None]] | None = None,
    on_temporary_glossary_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_pre_translation_analysis: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_document_term_memory_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> OfficeTranslationArtifacts:
    """추출된 Office 노드를 번역/주입 분리 구조로 처리한다."""

    _ = on_temporary_glossary_update
    injection_units = build_injection_units(nodes)
    translation_units = build_translation_units(
        injection_units,
        include_docx_neighbor_context=_docx_neighbor_context_enabled(style_options),
    )

    memory_setup = await setup_translation_memory(
        sem,
        session,
        injection_units,
        translation_units,
        target_lang,
        style_options,
        on_pre_translation_analysis=on_pre_translation_analysis,
        on_document_term_memory_update=on_document_term_memory_update,
    )
    effective_style_options = dict(memory_setup.style_options or {})

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

    resolver_enabled = _term_resolver_enabled(effective_style_options, translator_mode)
    summary_memory_enabled = bilingual_summary_memory_is_enabled(memory_setup.bilingual_summary_memory)
    translated_snapshot_by_unit_id: Dict[int, str] = {}
    injection_by_id = {item.injection_unit_id: item for item in injection_units}

    async def _handle_batch_translated(
        scope: str,
        batch_units: List[TranslationUnit],
        batch_translations: Dict[int, str],
    ) -> None:
        if not on_scope_batch_translated:
            return
        completed_node_ids: set[int] = set()
        for unit in batch_units:
            if unit.translation_unit_id not in batch_translations:
                continue
            for target in unit.targets:
                injection = injection_by_id.get(target.injection_unit_id)
                if injection is not None:
                    completed_node_ids.add(injection.node_id)
        if completed_node_ids:
            await on_scope_batch_translated(scope, completed_node_ids, batch_translations)

    async def _handle_scope_translated(scope: str, scope_translations: Dict[int, str]) -> None:
        translated_snapshot_by_unit_id.update(scope_translations)
        scope_units = [unit for unit in translation_units if unit.context_scope == scope]
        if resolver_enabled and memory_setup.document_term_memory:
            resolver_result = await resolve_document_term_memory_actions(
                sem,
                session,
                memory_setup.document_term_memory,
                target_lang=target_lang,
                evidence_memory=memory_setup.temporary_glossary,
                units=scope_units,
                translated_by_unit_id=scope_translations,
                pre_analysis=memory_setup.pre_translation_analysis,
                apply=True,
            )
            if resolver_result:
                job_id = str(effective_style_options.get("_job_id") or memory_setup.document_term_memory.get("job_id") or "")
                artifact_label = str(effective_style_options.get("_filename") or effective_style_options.get("_file_name") or "")
                snapshot_path = save_document_term_resolver_snapshot_to_local_file(
                    job_id,
                    memory_setup.document_term_memory,
                    artifact_label=artifact_label,
                    scope=scope,
                    resolver_result=resolver_result,
                )
                memory_setup.document_term_memory["_resolver_dump_path"] = snapshot_path or None
                memory_setup.document_term_memory["_last_resolver_result"] = resolver_result
                effective_style_options["_document_term_memory_memory"] = memory_setup.document_term_memory
                log_info(
                    "[Document Term Resolver] applied "
                    f"scope={scope} {document_term_memory_summary(memory_setup.document_term_memory)} "
                    f"dump_path={snapshot_path}"
                )
                if on_document_term_memory_update:
                    await on_document_term_memory_update(memory_setup.document_term_memory)
        if not on_scope_translated:
            return
        partial_resolved = resolve_injection_units(
            injection_units,
            translation_units,
            translated_snapshot_by_unit_id,
        )
        await on_scope_translated(scope, partial_resolved)

    async def _handle_scope_wave_translated(wave_results: List[tuple[str, Dict[int, str]]]) -> None:
        if not summary_memory_enabled or not memory_setup.bilingual_summary_memory:
            return
        wave_scopes = [scope for scope, _ in wave_results]
        wave_translations: Dict[int, str] = {}
        wave_units_by_id: Dict[int, TranslationUnit] = {}
        for scope, scope_translations in wave_results:
            wave_translations.update(scope_translations)
            for unit in translation_units:
                if unit.context_scope == scope:
                    wave_units_by_id[unit.translation_unit_id] = unit
        filtered_translations: Dict[int, str] = {}
        filtered_units: List[TranslationUnit] = []
        skipped_count = 0
        for unit_id, translated in wave_translations.items():
            unit = wave_units_by_id.get(unit_id)
            if unit is None:
                continue
            if _translation_safe_for_summary_memory(unit, translated, target_lang, effective_style_options):
                filtered_translations[unit_id] = translated
                filtered_units.append(unit)
            else:
                skipped_count += 1
        if not wave_units_by_id:
            return
        if skipped_count:
            log_info(
                "[Bilingual Summary Memory] skipped invalid translations before memory update "
                f"scope=wave:{','.join(wave_scopes)} skipped={skipped_count}"
            )
        if not filtered_units:
            log_info(
                "[Bilingual Summary Memory] skip wave update because no validated translations remain "
                f"scope=wave:{','.join(wave_scopes)}"
            )
            return
        wave_label = "wave:" + ",".join(wave_scopes)
        summary_update_started_at = time.perf_counter()
        await update_bilingual_summary_memory(
            sem,
            session,
            memory_setup.bilingual_summary_memory,
            scope=wave_label,
            units=filtered_units,
            translated_by_unit_id=filtered_translations,
        )
        summary_update_elapsed_ms = int((time.perf_counter() - summary_update_started_at) * 1000)
        summary_path = save_bilingual_summary_memory_to_local_file(
            str(
                effective_style_options.get("_job_id")
                or memory_setup.bilingual_summary_memory.get("job_id")
                or ""
            ),
            memory_setup.bilingual_summary_memory,
            artifact_label=str(
                effective_style_options.get("_filename")
                or effective_style_options.get("_file_name")
                or ""
            ),
        )
        memory_setup.bilingual_summary_memory["_dump_path"] = summary_path or None
        effective_style_options["_bilingual_summary_memory_memory"] = memory_setup.bilingual_summary_memory
        log_info(
            "[Bilingual Summary Memory] wave update overhead "
            f"scope={wave_label} elapsed_ms={summary_update_elapsed_ms} "
            f"last_status={memory_setup.bilingual_summary_memory.get('summary_update_last_status')} "
            f"total_overhead_ms={memory_setup.bilingual_summary_memory.get('summary_update_total_elapsed_ms')} "
            f"llm_overhead_ms={memory_setup.bilingual_summary_memory.get('summary_update_llm_elapsed_ms')} "
            f"dump_path={summary_path}"
        )

    trans_map, translated_by_unit_id, translation_error = await translate_units_with_mode(
        sem,
        session,
        translation_units,
        target_lang,
        deps,
        translator_mode=translator_mode,
        style_options=effective_style_options,
        on_scope_started=on_scope_started,
        on_batch_translated=_handle_batch_translated if on_scope_batch_translated else None,
        on_scope_translated=_handle_scope_translated
        if (on_scope_translated or resolver_enabled or summary_memory_enabled)
        else None,
        on_scope_wave_translated=_handle_scope_wave_translated if summary_memory_enabled else None,
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
        temporary_glossary=memory_setup.temporary_glossary,
        pre_translation_analysis=memory_setup.pre_translation_analysis,
        document_term_memory=memory_setup.document_term_memory,
        bilingual_summary_memory=memory_setup.bilingual_summary_memory,
    )
