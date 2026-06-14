"""Office translation mode dispatch."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List

import aiohttp

from translation_pipeline.common.llm import clear_last_llm_error, get_last_llm_error
from translation_pipeline.common.term_observer import record_observed_translations

from .contextual_translate import (
    DOCX_CONTEXT_CONFIG,
    PPTX_CONTEXT_CONFIG,
    XLSX_CONTEXT_CONFIG,
    scope_sort_key,
    translate_contextual_units,
)
from .translation_context import style_options_with_relevant_glossary
from .translation_memory import temporary_glossary_memory
from .types import OfficePipelineDeps, TranslationMap, TranslationUnit


def normalize_translator_mode(value: str | None) -> str:
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


async def translate_units_with_mode(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    translation_units: List[TranslationUnit],
    target_lang: str,
    deps: OfficePipelineDeps,
    translator_mode: str | None = None,
    style_options: Dict[str, Any] | None = None,
    on_scope_started: Callable[[str], Awaitable[None]] | None = None,
    on_scope_translated: Callable[[str, Dict[int, str]], Awaitable[None]] | None = None,
    on_scope_wave_translated: Callable[[List[tuple[str, Dict[int, str]]]], Awaitable[None]] | None = None,
) -> tuple[TranslationMap, Dict[int, str], str]:
    """번역 단위 목록을 선택된 번역기 모드로 처리한다."""

    mode = normalize_translator_mode(translator_mode)
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
            temporary_glossary_memory(style_options),
            translation_units,
            translated_by_unit_id,
        )
        if on_scope_translated:
            grouped_units: Dict[str, Dict[int, str]] = defaultdict(dict)
            for unit in translation_units:
                grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"][
                    unit.translation_unit_id
                ] = translated_by_unit_id[unit.translation_unit_id]
            for scope in sorted(grouped_units.keys(), key=scope_sort_key):
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

    if plain_units:
        plain_texts = [unit.text for unit in plain_units]
        plain_style_options = style_options_with_relevant_glossary(style_options, plain_units)
        plain_trans_map = await deps.batch_translate_async(
            sem,
            session,
            plain_texts,
            target_lang,
            style_options=plain_style_options,
        )
        trans_map.update(plain_trans_map)
        translated_by_unit_id.update(
            {
                unit.translation_unit_id: plain_trans_map.get(unit.text, unit.text)
                for unit in plain_units
            }
        )
        record_observed_translations(
            temporary_glossary_memory(style_options),
            plain_units,
            translated_by_unit_id,
        )

    if pptx_contextual_units:
        contextual_translations = await translate_contextual_units(
            sem,
            session,
            pptx_contextual_units,
            target_lang,
            style_options=style_options,
            config=PPTX_CONTEXT_CONFIG,
            on_scope_started=on_scope_started,
            on_scope_translated=on_scope_translated,
            on_scope_wave_translated=on_scope_wave_translated,
        )
        for unit in pptx_contextual_units:
            translated = contextual_translations.get(unit.translation_unit_id, unit.text)
            translated_by_unit_id[unit.translation_unit_id] = translated
            trans_map[unit.text] = translated

    if docx_contextual_units:
        docx_contextual_translations = await translate_contextual_units(
            sem,
            session,
            docx_contextual_units,
            target_lang,
            style_options=style_options,
            config=DOCX_CONTEXT_CONFIG,
            on_scope_started=on_scope_started,
            on_scope_translated=on_scope_translated,
            on_scope_wave_translated=on_scope_wave_translated,
        )
        for unit in docx_contextual_units:
            translated = docx_contextual_translations.get(unit.translation_unit_id, unit.text)
            translated_by_unit_id[unit.translation_unit_id] = translated
            trans_map[unit.text] = translated

    if xlsx_contextual_units:
        xlsx_contextual_translations = await translate_contextual_units(
            sem,
            session,
            xlsx_contextual_units,
            target_lang,
            style_options=style_options,
            config=XLSX_CONTEXT_CONFIG,
            on_scope_started=on_scope_started,
            on_scope_translated=on_scope_translated,
            on_scope_wave_translated=on_scope_wave_translated,
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
