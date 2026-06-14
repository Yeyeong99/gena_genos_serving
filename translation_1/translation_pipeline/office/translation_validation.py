"""Office 번역 응답 파싱/검증 helper."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.validation import validate_translation_batch_response

from .types import TranslationUnit


def parse_json_array_response(raw: str) -> Any:
    """LLM 응답에서 JSON array/object를 최대한 복구해 파싱한다."""

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


def validate_context_batch_items(
    parsed_items: Any,
    batch: List[TranslationUnit],
    *,
    log_prefix: str,
) -> tuple[Dict[int, str], list[str]]:
    """문맥 번역 batch 응답을 검증하고 normalized map을 반환한다."""

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


def _contains_hangul(text: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in text)


def _contains_latin(text: str) -> bool:
    return any(("a" <= char.lower() <= "z") for char in text)


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

_CONTEXT_LABEL_LEAK_RE = re.compile(
    r"\b(?:PREVIOUS|NEXT|SECTION_HEADING|TABLE_TITLE|ABBREVIATION_HINTS):",
    flags=re.IGNORECASE,
)


def needs_context_label_retry(original: str, translated: str) -> bool:
    """Retry when context-only labels leak into translated output."""

    if not translated.strip():
        return False
    if not _CONTEXT_LABEL_LEAK_RE.search(translated):
        return False
    return not _CONTEXT_LABEL_LEAK_RE.search(original or "")


def needs_target_language_retry(
    original: str,
    translated: str,
    target_lang: str,
) -> bool:
    """타겟 언어 대비 결과에 source-language script가 남아 있으면 재시도를 요청한다."""

    target = _RETRY_TARGET_ALIASES.get((target_lang or "").strip().lower(), "")
    if target in ("", "ko"):
        return False
    if not original.strip() or not translated.strip():
        return False
    source_has_cjk = _contains_hangul(original) or _contains_han(original) or _contains_kana(original)
    if not source_has_cjk:
        return False

    if target == "en":
        return _contains_hangul(translated) or _contains_han(translated) or _contains_kana(translated)
    if target == "ja":
        if not _contains_hangul(translated):
            return False
        return not _contains_kana(translated)
    if target == "zh":
        if not _contains_hangul(translated):
            return False
        return not _contains_han(translated)
    return False
