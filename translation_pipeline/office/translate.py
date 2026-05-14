"""Office 문서 번역 단계 모듈."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import aiohttp

from translation_pipeline.common.llm import (
    build_target_language_guard,
    build_translation_style_instruction,
    clear_last_llm_error,
    get_last_llm_error,
    llm_call_async,
    translate_single_async,
)
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

_DOCX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_DOCX_MAX_ITEMS_PER_BATCH", "12"))
_DOCX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_DOCX_MAX_CHARS_PER_BATCH", "6000"))
_DOCX_CONTEXT_SCOPE_CONCURRENCY = int(os.getenv("AI_TRANSLATION_DOCX_SCOPE_CONCURRENCY", "20"))
_XLSX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_XLSX_MAX_ITEMS_PER_BATCH", "24"))
_XLSX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_XLSX_MAX_CHARS_PER_BATCH", "9000"))
_PPTX_CONTEXT_MAX_ITEMS_PER_BATCH = int(os.getenv("AI_TRANSLATION_PPTX_MAX_ITEMS_PER_BATCH", "24"))
_PPTX_CONTEXT_MAX_CHARS_PER_BATCH = int(os.getenv("AI_TRANSLATION_PPTX_MAX_CHARS_PER_BATCH", "9000"))
_PPTX_CONTEXT_SCOPE_CONCURRENCY = int(os.getenv("AI_TRANSLATION_PPTX_SCOPE_CONCURRENCY", "1"))
_PPTX_CONTEXT_VERBOSE_LOG = os.getenv("AI_TRANSLATION_PPTX_CONTEXT_VERBOSE_LOG", "0") == "1"
_LLM_VALIDATION_RETRY_COUNT = int(os.getenv("AI_TRANSLATION_LLM_VALIDATION_RETRY_COUNT", "1"))


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


def _build_pptx_context_system_prompt(
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
) -> str:
    style_instruction = build_translation_style_instruction(target_lang, style_options)
    language_guard = build_target_language_guard(target_lang)
    return (
        "You are a professional presentation translator. "
        f"Translate ONLY each TARGET_TEXT item into {target_lang} naturally and accurately, "
        "following the selected translation purpose, style, and terminology requirements when provided. "
        "Use CONTEXT_TEXT only to preserve meaning, terminology, tone, and slide-level coherence. "
        f"{language_guard} "
        "Do not summarize the context. Do not translate any label other than TARGET_TEXT. "
        'Return ONLY a JSON array in the form [{"id": 0, "t": "translated text"}]. '
        'Keep each "id" unchanged and put translated text in key "t".'
        f"{style_instruction}"
    )


def _build_pptx_context_user_prompt(
    context_text: str,
    target_items: List[Tuple[int, str]],
    previous_items: Dict[int, str] | None = None,
) -> str:
    previous_items = previous_items or {}
    payload = []
    for item_id, text in target_items:
        item: Dict[str, Any] = {"id": item_id, "s": text}
        previous = previous_items.get(item_id)
        if previous:
            item["previous_t"] = previous
        payload.append(item)
    return (
        "CONTEXT_TEXT:\n"
        f"{context_text}\n\n"
        "TARGET_TEXT_ITEMS:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _build_docx_context_system_prompt(
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
) -> str:
    style_instruction = build_translation_style_instruction(target_lang, style_options)
    language_guard = build_target_language_guard(target_lang)
    return (
        "You are a professional document translator. "
        f"Translate ONLY each SOURCE_TEXT item into {target_lang} naturally and accurately, "
        "following the selected translation purpose, style, and terminology requirements when provided. "
        "Use ITEM_CONTEXT only to preserve local meaning, terminology, and paragraph flow. "
        f"{language_guard} "
        "Do not summarize. Do not omit content. "
        'Return ONLY a JSON array in the form [{"id": 0, "t": "translated text"}]. '
        'Keep each "id" unchanged and put translated text in key "t".'
        f"{style_instruction}"
    )


def _build_docx_context_user_prompt(batch: List[TranslationUnit]) -> str:
    return (
        "ITEMS:\n"
        + json.dumps(
            [
                {
                    "id": unit.translation_unit_id,
                    "context": unit.context_text,
                    "source": unit.text,
                }
                for unit in batch
            ],
            ensure_ascii=False,
        )
    )


def _build_xlsx_context_system_prompt(
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
) -> str:
    style_instruction = build_translation_style_instruction(target_lang, style_options)
    language_guard = build_target_language_guard(target_lang)
    return (
        "You are a professional spreadsheet translator. "
        f"Translate ONLY each CELL_TEXT item into {target_lang} naturally and accurately, "
        "following the selected translation purpose, style, and terminology requirements when provided. "
        "Use CELL_CONTEXT to understand table headers, row labels, sheet names, and nearby cells. "
        f"{language_guard} "
        "Preserve numbers, formulas, units, punctuation, and line breaks as much as possible. "
        "Do not translate sheet/cell labels unless they are inside CELL_TEXT. "
        "Do not infer or change script labels from nearby values: translate '한글' as 'Korean' and "
        "'한자' as 'Hanja' only when that exact source label appears in CELL_TEXT. "
        "If a Korean/Hanja currency display such as '일금...' or '一金...' appears, translate its meaning "
        f"into natural {target_lang} instead of copying the Korean or Hanja wording. "
        'Return ONLY a JSON array in the form [{"id": 0, "t": "translated text"}]. '
        'Keep each "id" unchanged and put translated text in key "t".'
        f"{style_instruction}"
    )


def _build_xlsx_context_user_prompt(
    batch: List[TranslationUnit],
    previous_items: Dict[int, str] | None = None,
) -> str:
    previous_items = previous_items or {}
    return (
        "CELLS:\n"
        + json.dumps(
            [
                {
                    "id": unit.translation_unit_id,
                    "cell_context": unit.context_text,
                    "cell_text": unit.text,
                    **(
                        {"previous_t": previous_items[unit.translation_unit_id]}
                        if previous_items.get(unit.translation_unit_id)
                        else {}
                    ),
                }
                for unit in batch
            ],
            ensure_ascii=False,
        )
    )


def _log_pptx_context_prompt(
    scope: str,
    units: List[TranslationUnit],
    system_prompt: str,
    user_prompt: str,
) -> None:
    print(
        "[PPTX 문맥 번역] "
        f"scope={scope} "
        f"items={len(units)} "
        f"targets={[[ (t.injection_unit_id, t.fragment_index, t.fragment_count) for t in unit.targets ] for unit in units[:5]]}"
    )
    if not _PPTX_CONTEXT_VERBOSE_LOG:
        return
    context_preview = ((units[0].context_text if units else "") or "").replace("\n", " ").strip()[:700]
    user_preview = user_prompt.replace("\n", " ").strip()[:1000]
    print(f"  system_prompt={system_prompt[:400]}")
    print(f"  context_preview={context_preview}")
    print(f"  user_prompt_preview={user_preview}")


def _validate_context_batch_items(
    parsed_items: Any,
    batch: List[TranslationUnit],
    *,
    log_prefix: str,
) -> tuple[Dict[int, str], list[str]]:
    expected = {unit.translation_unit_id: unit.text for unit in batch}
    validation = validate_translation_batch_response(parsed_items, expected)
    if validation.hard_errors:
        print(
            f"{log_prefix} hard validation failed: "
            + "; ".join(validation.hard_errors[:5])
        )
    if validation.soft_warnings:
        print(
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
    system_prompt = _build_docx_context_system_prompt(target_lang, style_options)
    results: Dict[int, str] = {}
    grouped_units: Dict[str, List[TranslationUnit]] = defaultdict(list)
    for unit in translation_units:
        grouped_units[unit.context_scope or f"unit:{unit.translation_unit_id}"].append(unit)

    pending = [unit for unit in translation_units if unit.text.strip()]
    for unit in translation_units:
        if not unit.text.strip():
            results[unit.translation_unit_id] = unit.text

    async def _safe_translate_single(unit: TranslationUnit) -> str:
        prompt = (
            "Translate the SOURCE_TEXT into "
            f"{target_lang}. Use the CONTEXT only for local meaning.\n\n"
            f"{build_translation_style_instruction(target_lang, style_options)}\n\n"
            f"CONTEXT:\n{unit.context_text}\n\n"
            f"SOURCE_TEXT:\n{unit.text}"
        )
        try:
            return await llm_call_async(sem, session, "", prompt)
        except Exception as exc:
            print(f"  [DOCX 문맥 번역] single fallback failed: {exc}")
            return unit.text

    def _split_batches(units: List[TranslationUnit]) -> List[List[TranslationUnit]]:
        batches: List[List[TranslationUnit]] = []
        current: List[TranslationUnit] = []
        current_chars = 0
        for unit in units:
            estimated_chars = len(unit.text) + len(unit.context_text) + 80
            if current and (
                len(current) >= _DOCX_CONTEXT_MAX_ITEMS_PER_BATCH
                or current_chars + estimated_chars > _DOCX_CONTEXT_MAX_CHARS_PER_BATCH
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(unit)
            current_chars += estimated_chars
        if current:
            batches.append(current)
        return batches

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
        print(
            "[DOCX 문맥 번역] "
            f"{label} start items={len(batch)} chars={char_count} "
            f"ids={unit_ids[:5]}{'...' if len(unit_ids) > 5 else ''}"
        )
        user_prompt = _build_docx_context_user_prompt(batch)
        try:
            raw = await llm_call_async(sem, session, system_prompt, user_prompt)
        except Exception as exc:
            print(
                "[DOCX 문맥 번역] "
                f"{label} failed {loop.time() - started_at:.2f}s: {exc}"
            )
            raw = ""
        if not raw:
            print(
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
                print(
                    "[DOCX 문맥 번역] "
                    f"{label} validation retry {attempt + 1}/{_LLM_VALIDATION_RETRY_COUNT}"
                )
                retry_raw = await llm_call_async(sem, session, system_prompt, user_prompt)
                retry_parsed = _parse_json_array_response(retry_raw)
                normalized, hard_errors = _validate_context_batch_items(
                    retry_parsed,
                    batch,
                    log_prefix=f"[DOCX 문맥 번역] {label} retry",
                )
                if not hard_errors:
                    break
        if hard_errors or not normalized:
            print(
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
        print(
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
        print(
            "[DOCX 문맥 번역] "
            f"{scope} {len(pending_units)}개 단위 -> {len(batches)}개 배치 "
            f"(max_items={_DOCX_CONTEXT_MAX_ITEMS_PER_BATCH}, "
            f"max_chars={_DOCX_CONTEXT_MAX_CHARS_PER_BATCH})"
        )
        start = asyncio.get_running_loop().time()
        batch_total = len(batches)
        batch_results = await asyncio.gather(
            *[
                _run_batch(batch, batch_index=index, batch_total=batch_total)
                for index, batch in enumerate(batches, start=1)
            ]
        )
        print(
            "[DOCX 문맥 번역] "
            f"{scope} LLM 병렬 배치 완료: {asyncio.get_running_loop().time() - start:.2f}s"
        )
        scope_result: Dict[int, str] = {}
        for batch_result in batch_results:
            scope_result.update(batch_result)
        return scope_result

    batches = _split_batches(pending)
    print(
        "[DOCX 문맥 번역] "
        f"{len(pending)}개 단위 -> {len(batches)}개 배치 "
        f"(max_items={_DOCX_CONTEXT_MAX_ITEMS_PER_BATCH}, "
        f"max_chars={_DOCX_CONTEXT_MAX_CHARS_PER_BATCH})"
    )
    sorted_scopes = sorted(grouped_units.keys(), key=_scope_sort_key)
    is_page_scoped_docx = all(scope.startswith("docx:page:") for scope in sorted_scopes)

    if is_page_scoped_docx and _DOCX_CONTEXT_SCOPE_CONCURRENCY > 1:
        scope_sem = asyncio.Semaphore(max(1, _DOCX_CONTEXT_SCOPE_CONCURRENCY))

        async def _run_scope_worker(scope: str) -> tuple[str, Dict[int, str]]:
            async with scope_sem:
                return scope, await _translate_scope(scope, grouped_units[scope])

        tasks = [asyncio.create_task(_run_scope_worker(scope)) for scope in sorted_scopes]
        print(
            "[DOCX 문맥 번역] "
            f"페이지 scope {len(sorted_scopes)}개를 최대 "
            f"{_DOCX_CONTEXT_SCOPE_CONCURRENCY}개 병렬로 선번역합니다."
        )
        for task in asyncio.as_completed(tasks):
            scope, scope_results = await task
            if on_scope_translated:
                await on_scope_translated(scope, scope_results)
            results.update(scope_results)
        return results

    for scope in sorted_scopes:
        if on_scope_started:
            await on_scope_started(scope)
        scope_results = await _translate_scope(scope, grouped_units[scope])
        if on_scope_translated:
            await on_scope_translated(scope, scope_results)
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
    system_prompt = _build_xlsx_context_system_prompt(target_lang, style_options)
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
        previous_text = ""
        for target in unit.targets:
            previous = previous_by_injection_id.get(target.injection_unit_id)
            if previous:
                previous_text = str(previous)
                break
        previous_section = f"\n\nPREVIOUS_TRANSLATION:\n{previous_text}" if previous_text else ""
        prompt = (
            "Translate CELL_TEXT into "
            f"{target_lang}. Use CELL_CONTEXT only to understand the spreadsheet table.\n\n"
            "Do not infer script labels from nearby values. Translate Korean/Hanja currency display text "
            f"into natural {target_lang}.\n\n"
            f"{build_translation_style_instruction(target_lang, style_options)}\n\n"
            f"CELL_CONTEXT:\n{unit.context_text}\n\n"
            f"CELL_TEXT:\n{unit.text}"
            f"{previous_section}"
        )
        try:
            return await llm_call_async(sem, session, "", prompt)
        except Exception as exc:
            print(f"  [XLSX 문맥 번역] single fallback failed: {exc}")
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
        previous_items: Dict[int, str] = {}
        for unit in batch:
            for target in unit.targets:
                previous = previous_by_injection_id.get(target.injection_unit_id)
                if previous:
                    previous_items[unit.translation_unit_id] = str(previous)
                    break
        user_prompt = _build_xlsx_context_user_prompt(batch, previous_items)
        try:
            raw = await llm_call_async(sem, session, system_prompt, user_prompt)
        except Exception as exc:
            print(f"  [XLSX 문맥 번역] batch call failed: {exc}")
            raw = ""
        if not raw:
            print("  [XLSX 문맥 번역] empty batch response; using original text for this batch")
            return {unit.translation_unit_id: unit.text for unit in batch}

        parsed = _parse_json_array_response(raw)
        normalized, hard_errors = _validate_context_batch_items(
            parsed,
            batch,
            log_prefix="[XLSX 문맥 번역]",
        )
        if hard_errors and _LLM_VALIDATION_RETRY_COUNT > 0:
            for attempt in range(_LLM_VALIDATION_RETRY_COUNT):
                print(
                    "  [XLSX 문맥 번역] "
                    f"validation retry {attempt + 1}/{_LLM_VALIDATION_RETRY_COUNT}"
                )
                retry_raw = await llm_call_async(sem, session, system_prompt, user_prompt)
                retry_parsed = _parse_json_array_response(retry_raw)
                normalized, hard_errors = _validate_context_batch_items(
                    retry_parsed,
                    batch,
                    log_prefix="[XLSX 문맥 번역] retry",
                )
                if not hard_errors:
                    break
        if hard_errors or not normalized:
            print("  [XLSX 문맥 번역] batch parse failed; splitting batch")
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
        return normalized

    async def _run_scope(scope: str, units: List[TranslationUnit]) -> Dict[int, str]:
        if on_scope_started:
            await on_scope_started(scope)
        pending = [unit for unit in units if unit.text.strip()]
        batches = _split_batches(pending)
        print(
            "[XLSX 문맥 번역] "
            f"{scope}: {len(pending)}개 셀 -> {len(batches)}개 배치 "
            f"(max_items={_XLSX_CONTEXT_MAX_ITEMS_PER_BATCH}, "
            f"max_chars={_XLSX_CONTEXT_MAX_CHARS_PER_BATCH})"
        )
        if pending:
            preview = pending[0].context_text.replace("\n", " ").strip()[:700]
            print(f"  context_preview={preview}")
        start = asyncio.get_running_loop().time()
        batch_results = await asyncio.gather(*[_run_batch(batch) for batch in batches])
        scope_result: Dict[int, str] = {}
        for batch_result in batch_results:
            scope_result.update(batch_result)
        print(f"[XLSX 문맥 번역] {scope} 배치 완료: {asyncio.get_running_loop().time() - start:.2f}s")
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
    system_prompt = _build_pptx_context_system_prompt(target_lang, style_options)
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

    async def _safe_translate_single(text: str) -> str:
        try:
            return await translate_single_async(sem, session, text, target_lang, style_options)
        except Exception as exc:
            print(f"  [PPTX 문맥 번역] single fallback failed: {exc}")
            return text

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
            target_items = [(unit.translation_unit_id, unit.text) for unit in batch]
            previous_items: Dict[int, str] = {}
            for unit in batch:
                for target in unit.targets:
                    previous = previous_by_injection_id.get(target.injection_unit_id)
                    if previous:
                        previous_items[unit.translation_unit_id] = str(previous)
                        break
            user_prompt = _build_pptx_context_user_prompt(
                batch[0].context_text or "",
                target_items,
                previous_items,
            )
            _log_pptx_context_prompt(scope, batch, system_prompt, user_prompt)
            try:
                raw = await llm_call_async(sem, session, system_prompt, user_prompt)
            except Exception as exc:
                print(f"  [PPTX 문맥 번역] batch call failed for scope={scope}: {exc}")
                raw = ""
            if not raw:
                print(
                    f"  [PPTX 문맥 번역] empty batch response for scope={scope}; "
                    "using original text for this batch"
                )
                return {unit.translation_unit_id: unit.text for unit in batch}
            if _PPTX_CONTEXT_VERBOSE_LOG:
                print(f"  raw_response_preview={raw[:700].replace(chr(10), ' ')}")
            parsed = _parse_json_array_response(raw)
            normalized, hard_errors = _validate_context_batch_items(
                parsed,
                batch,
                log_prefix=f"[PPTX 문맥 번역] scope={scope}",
            )
            if hard_errors and _LLM_VALIDATION_RETRY_COUNT > 0:
                for attempt in range(_LLM_VALIDATION_RETRY_COUNT):
                    print(
                        f"  [PPTX 문맥 번역] scope={scope} "
                        f"validation retry {attempt + 1}/{_LLM_VALIDATION_RETRY_COUNT}"
                    )
                    retry_raw = await llm_call_async(sem, session, system_prompt, user_prompt)
                    retry_parsed = _parse_json_array_response(retry_raw)
                    normalized, hard_errors = _validate_context_batch_items(
                        retry_parsed,
                        batch,
                        log_prefix=f"[PPTX 문맥 번역] scope={scope} retry",
                    )
                    if not hard_errors:
                        break
            if hard_errors or not normalized:
                print(f"  [PPTX 문맥 번역] batch parse failed for scope={scope}; splitting batch")
                if len(batch) > 1:
                    mid = max(1, len(batch) // 2)
                    left = await run_one_batch(batch[:mid])
                    right = await run_one_batch(batch[mid:])
                    return {**left, **right}
                return {
                    batch[0].translation_unit_id: await _safe_translate_single(batch[0].text)
                }
            for unit in batch:
                current = normalized.get(unit.translation_unit_id)
                if current is None:
                    normalized[unit.translation_unit_id] = await _safe_translate_single(unit.text)
                    continue
                if _needs_target_language_retry(unit.text, current, target_lang):
                    normalized[unit.translation_unit_id] = await _safe_translate_single(unit.text)
            return normalized

        if pending:
            batches = _split_batches(pending)
            print(
                "[PPTX 문맥 번역] "
                f"scope={scope} {len(pending)}개 단위 -> {len(batches)}개 배치 "
                f"(max_items={_PPTX_CONTEXT_MAX_ITEMS_PER_BATCH}, "
                f"max_chars={_PPTX_CONTEXT_MAX_CHARS_PER_BATCH})"
            )

            async def _run_indexed_batch(index: int, batch: List[TranslationUnit]) -> Dict[int, str]:
                start = asyncio.get_running_loop().time()
                print(f"[PPTX 문맥 번역] scope={scope} batch={index}/{len(batches)} start items={len(batch)}")
                result = await run_one_batch(batch)
                print(
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
        print(
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
        other_trans_map = await deps.batch_translate_async(
            sem,
            session,
            other_plain_texts,
            target_lang,
            style_options=style_options,
        )
        trans_map.update(other_trans_map)
        translated_by_unit_id.update(
            {
                unit.translation_unit_id: other_trans_map.get(unit.text, unit.text)
                for unit in other_plain_units
            }
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
) -> OfficeTranslationArtifacts:
    """추출된 Office 노드를 번역/주입 분리 구조로 처리한다."""

    injection_units = build_injection_units(nodes)
    translation_units = build_translation_units(injection_units)
    effective_style_options = style_options
    previous_by_node_id = (
        style_options.get("_previous_translation_by_node_id")
        if isinstance(style_options, dict)
        else None
    )
    if isinstance(previous_by_node_id, dict):
        previous_by_injection_id: Dict[int, str] = {}
        for injection in injection_units:
            previous = previous_by_node_id.get(injection.node_id)
            if previous:
                previous_by_injection_id[injection.injection_unit_id] = str(previous)
        effective_style_options = {
            **style_options,
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
        resolved_injections=resolved_injections,
        translation_error=translation_error,
    )
