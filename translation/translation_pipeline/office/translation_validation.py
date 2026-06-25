"""Office 번역 응답 파싱/검증 helper."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
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


def _contains_cyrillic(text: str) -> bool:
    for char in text:
        code = ord(char)
        if 0x0400 <= code <= 0x04FF or 0x0500 <= code <= 0x052F:
            return True
    return False


def _has_unexpected_cjk_or_foreign_script(original: str, translated: str) -> bool:
    """Korean output should not invent Han/Kana/Cyrillic script fragments."""

    original_chars = set(original or "")
    for char in translated or "":
        code = ord(char)
        is_han = 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
        is_kana = 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF
        is_cyrillic = 0x0400 <= code <= 0x04FF or 0x0500 <= code <= 0x052F
        if (is_han or is_kana or is_cyrillic) and char not in original_chars:
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
_LATIN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z][A-Za-z'-]*\b")
_LONG_LATIN_RUN_RE = re.compile(r"\b[A-Za-z][A-Za-z ,;:'\"()\-]{24,}[.!?]?")
_TECH_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9./_+() -]*$")
_TITLECASE_LATIN_PHRASE_RE = re.compile(
    r"^(?:[A-Z][A-Za-z0-9'&/-]*|[A-Z0-9&/-]{2,})(?:\s+(?:[A-Z][A-Za-z0-9'&/-]*|[A-Z0-9&/-]{2,}|of|the|and|for|in))*$"
)


def needs_context_label_retry(original: str, translated: str) -> bool:
    """Retry when context-only labels leak into translated output."""

    if not translated.strip():
        return False
    if not _CONTEXT_LABEL_LEAK_RE.search(translated):
        return False
    return not _CONTEXT_LABEL_LEAK_RE.search(original or "")


def _is_code_like_latin_fragment(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return True
    if _TECH_CODE_RE.fullmatch(stripped):
        return True
    words = _LATIN_WORD_RE.findall(stripped)
    if not words:
        return True
    # Mostly short all-caps tokens, identifiers, standard numbers, or units.
    lowercase_words = [word for word in words if any(char.islower() for char in word)]
    if not lowercase_words:
        return True
    meaningful_lowercase = [
        word
        for word in lowercase_words
        if len(word) >= 4 and not re.search(r"\d", word)
    ]
    return not meaningful_lowercase


def _is_preservable_latin_name_or_identifier(text: str) -> bool:
    stripped = normalize_space(str(text or "").strip(" .,:;!?\"'()[]{}"))
    if not stripped:
        return True
    words = _LATIN_WORD_RE.findall(stripped)
    if not words or len(words) > 6:
        return False
    if _is_code_like_latin_fragment(stripped):
        return True
    return bool(_TITLECASE_LATIN_PHRASE_RE.fullmatch(stripped))


def _has_untranslated_latin_phrase_for_korean(original: str, translated: str) -> bool:
    """Detect long source-language English fragments left in Korean output.

    This intentionally ignores compact codes, acronyms, standard identifiers,
    numbers, and units. It is aimed at descriptive phrases/definition cells
    that contain no Korean at all.
    """

    text = str(translated or "").strip()
    if not text:
        return False
    if not _contains_latin(text):
        return False
    if _is_preservable_latin_name_or_identifier(text):
        return False
    if _is_code_like_latin_fragment(text):
        return False

    embedded_words = _LATIN_WORD_RE.findall(text)
    embedded_lowercase_words = [
        word
        for word in embedded_words
        if word[:1].islower()
        and len(word) >= 4
        and not re.search(r"\d", word)
    ]
    if _contains_hangul(text) and embedded_lowercase_words:
        return True
    if _contains_hangul(text):
        return False

    source = str(original or "").strip()
    source_words = _LATIN_WORD_RE.findall(source)
    translated_words = _LATIN_WORD_RE.findall(text)
    if len(translated_words) < 2:
        return False
    if source and normalize_space(source).lower() == normalize_space(text).lower():
        return True
    return len([word for word in translated_words if len(word) >= 4 and any(char.islower() for char in word)]) >= 2


def _unexpected_foreign_script_samples(original: str, translated: str) -> list[str]:
    original_chars = set(original or "")
    samples: list[str] = []
    seen: set[str] = set()
    for char in translated or "":
        code = ord(char)
        is_han = 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
        is_kana = 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF
        is_cyrillic = 0x0400 <= code <= 0x04FF or 0x0500 <= code <= 0x052F
        if not (is_han or is_kana or is_cyrillic):
            continue
        if char in original_chars or char in seen:
            continue
        seen.add(char)
        samples.append(char)
        if len(samples) >= 8:
            break
    return samples


def _untranslated_latin_phrase_samples_for_korean(original: str, translated: str) -> list[str]:
    text = str(translated or "").strip()
    if not text or not _contains_latin(text):
        return []
    samples: list[str] = []
    for match in _LONG_LATIN_RUN_RE.finditer(text):
        fragment = normalize_space(match.group(0))
        if _is_preservable_latin_name_or_identifier(fragment):
            continue
        if _is_code_like_latin_fragment(fragment):
            continue
        words = _LATIN_WORD_RE.findall(fragment)
        meaningful = [
            word
            for word in words
            if len(word) >= 4 and any(char.islower() for char in word) and not re.search(r"\d", word)
        ]
        if len(meaningful) < 2:
            continue
        samples.append(fragment[:120])
        if len(samples) >= 3:
            break
    if samples:
        return samples

    if _has_untranslated_latin_phrase_for_korean(original, translated):
        words = _LATIN_WORD_RE.findall(text)
        if words:
            return [" ".join(words[:12])]
    return []


_FORMAL_HAMNIDA_BAD_ENDING_RE = re.compile(
    r"(?<!니)다[.!?…]*[\"'”’»)\]]*(?=\s|$)"
)
_FORMAL_HAMNIDA_ALLOWED_ENDINGS = ("합니다", "습니다", "했습니다", "입니까", "습니까", "하십시오")
_QUOTED_SPAN_RE = re.compile(r"([\"“”'‘’])(?:\\.|(?!\1).)*\1")
_BROKEN_REPLACEMENT_RE = re.compile(r"\ufffd")
_REPEATED_FOREIGN_SCRIPT_RE = re.compile(r"[\u0400-\u052f\ufffd]{3,}")
_SYMBOL_JUNK_RE = re.compile(r"^[!\"#$%&'()*+,./0-9:;<=>?@\[\]^_`{|}~\\\s-]{3,}$")
_SYMBOL_JUNK_MARKER_RE = re.compile(r"[!\"#$%&'()*+]{3,}")


def needs_formality_retry(
    translated: str,
    target_lang: str,
    formality: str | None,
    *,
    element_type: str = "",
) -> bool:
    """Retry obvious Korean sentence-ending violations for strict 합니다체."""

    if str(formality or "").strip() != "formal_hamnida":
        return False
    target = _RETRY_TARGET_ALIASES.get((target_lang or "").strip().lower(), "")
    if target != "ko":
        return False
    if str(element_type or "").strip().lower() in {
        "table_cell",
        "column_header",
        "row_header",
        "placeholder",
        "heading",
        "title",
        "section_heading",
    }:
        return False
    text = normalize_space(str(translated or ""))
    if not text or not _contains_hangul(text):
        return False
    text = normalize_space(_QUOTED_SPAN_RE.sub("", text))
    if not text or not _contains_hangul(text):
        return False
    if text.endswith(_FORMAL_HAMNIDA_ALLOWED_ENDINGS):
        return False
    for match in _FORMAL_HAMNIDA_BAD_ENDING_RE.finditer(text):
        prefix = text[: match.end()].rstrip(".!?…\"'”’»)] ")
        if prefix.endswith(_FORMAL_HAMNIDA_ALLOWED_ENDINGS):
            continue
        return True
    return False


def needs_corruption_retry(original: str, translated: str) -> bool:
    """Retry output with clear generated text corruption or symbol-only junk."""

    text = str(translated or "").strip()
    if not text:
        return False
    source = str(original or "").strip()
    if _looks_like_serialized_json_text(text) and not _looks_like_serialized_json_text(source):
        return True
    if _BROKEN_REPLACEMENT_RE.search(text) and not _BROKEN_REPLACEMENT_RE.search(source):
        return True
    if _REPEATED_FOREIGN_SCRIPT_RE.search(text) and not _REPEATED_FOREIGN_SCRIPT_RE.search(source):
        return True
    if _SYMBOL_JUNK_RE.fullmatch(text) and not _SYMBOL_JUNK_RE.fullmatch(source):
        return True
    return False


def is_symbol_junk_source(text: str) -> bool:
    """Return True for non-language symbol artifacts that should skip LLM."""

    value = str(text or "").strip()
    if len(value) < 6:
        return False
    if not _SYMBOL_JUNK_RE.fullmatch(value):
        return False
    if _contains_latin(value) or _contains_hangul(value) or _contains_han(value) or _contains_kana(value):
        return False
    return bool(_SYMBOL_JUNK_MARKER_RE.search(value))


def _looks_like_serialized_json_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value or value[0] not in "[{":
        return False
    try:
        parsed = json.loads(value)
    except Exception:
        return False
    return isinstance(parsed, (list, dict))


def needs_structure_retry(original: str, translated: str) -> bool:
    """Retry obvious source/target structure drift."""

    source = str(original or "").strip()
    target = str(translated or "").strip()
    if not source or not target:
        return False
    source_dialogue = _looks_like_direct_dialogue(source)
    target_dialogue = _looks_like_direct_dialogue(target)
    if not source_dialogue and not _contains_dialogue_marker(source) and target_dialogue:
        return True
    return False


def _semantic_similarity(left: str, right: str) -> float:
    left_raw = str(left or "")
    right_raw = str(right or "")
    lhs = re.sub(r"[\W_]+", "", left_raw.lower())
    rhs = re.sub(r"[\W_]+", "", right_raw.lower())
    has_cjk = (
        _contains_hangul(left_raw)
        or _contains_hangul(right_raw)
        or _contains_han(left_raw)
        or _contains_han(right_raw)
        or _contains_kana(left_raw)
        or _contains_kana(right_raw)
    )
    min_len = 12 if has_cjk else 24
    if len(lhs) < min_len or len(rhs) < min_len:
        return 0.0
    return SequenceMatcher(None, lhs, rhs).ratio()


_TARGET_DUPLICATE_SIMILARITY_THRESHOLD = 0.88
_SOURCE_DUPLICATE_SIMILARITY_THRESHOLD = 0.78
_DIALOGUE_OPENERS = ('"', "'", "“", "‘", "「", "『", "«")
_DIALOGUE_MARKERS = ('"', "“", "”", "‘", "’", "「", "」", "『", "』", "«", "»")


def _looks_like_direct_dialogue(text: str) -> bool:
    stripped = str(text or "").lstrip()
    return bool(stripped) and stripped[0] in _DIALOGUE_OPENERS


def _contains_dialogue_marker(text: str) -> bool:
    return any(marker in str(text or "") for marker in _DIALOGUE_MARKERS)


def _compact_for_overlap(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def _longest_common_substring_length(left: str, right: str) -> int:
    lhs = _compact_for_overlap(left)
    rhs = _compact_for_overlap(right)
    if not lhs or not rhs:
        return 0
    previous = [0] * (len(rhs) + 1)
    best = 0
    for left_index, left_char in enumerate(lhs, start=1):
        current = [0] * (len(rhs) + 1)
        for right_index, right_char in enumerate(rhs, start=1):
            if left_char != right_char:
                continue
            current[right_index] = previous[right_index - 1] + 1
            if current[right_index] > best:
                best = current[right_index]
        previous = current
    return best


def _shares_repeated_dialogue_phrase(left: str, right: str) -> bool:
    """Detect repeated short dialogue phrases that ratio thresholds can miss."""

    if _semantic_similarity(left, right) >= _TARGET_DUPLICATE_SIMILARITY_THRESHOLD:
        return True
    has_cjk = (
        _contains_hangul(left)
        or _contains_hangul(right)
        or _contains_han(left)
        or _contains_han(right)
        or _contains_kana(left)
        or _contains_kana(right)
    )
    return _longest_common_substring_length(left, right) >= (6 if has_cjk else 12)


def _adjacent_leakage_retry_id(
    previous: TranslationUnit,
    current: TranslationUnit,
    previous_text: str,
    current_text: str,
    *,
    source_similarity: float,
) -> int | None:
    """Return the unit that likely copied an adjacent unit's translation.

    This catches cases where a narrative unit is translated as the neighboring
    direct dialogue. A plain duplicate check would retry the later unit, but in
    these cases the earlier narrative unit is the corrupted one.
    """

    if source_similarity >= _SOURCE_DUPLICATE_SIMILARITY_THRESHOLD:
        return None

    previous_source_dialogue = _looks_like_direct_dialogue(previous.text)
    current_source_dialogue = _looks_like_direct_dialogue(current.text)
    previous_target_dialogue = _looks_like_direct_dialogue(previous_text)
    current_target_dialogue = _looks_like_direct_dialogue(current_text)
    if not _shares_repeated_dialogue_phrase(previous_text, current_text):
        return None

    if (
        not previous_source_dialogue
        and not _contains_dialogue_marker(previous.text)
        and current_source_dialogue
        and previous_target_dialogue
        and current_target_dialogue
    ):
        return previous.translation_unit_id
    if (
        previous_source_dialogue
        and not current_source_dialogue
        and not _contains_dialogue_marker(current.text)
        and previous_target_dialogue
        and current_target_dialogue
    ):
        return current.translation_unit_id
    return None


def duplicate_like_translation_unit_ids(
    batch: List[TranslationUnit],
    normalized: Dict[int, str],
) -> list[int]:
    """Find adjacent target duplicates/leaks that are not duplicates in source."""

    duplicate_ids: list[int] = []
    ordered = [unit for unit in batch if unit.translation_unit_id in normalized]
    for previous, current in zip(ordered, ordered[1:]):
        previous_text = normalized.get(previous.translation_unit_id, "")
        current_text = normalized.get(current.translation_unit_id, "")
        target_similarity = _semantic_similarity(previous_text, current_text)
        source_similarity = _semantic_similarity(previous.text, current.text)

        if source_similarity >= _SOURCE_DUPLICATE_SIMILARITY_THRESHOLD:
            continue

        retry_id = _adjacent_leakage_retry_id(
            previous,
            current,
            previous_text,
            current_text,
            source_similarity=source_similarity,
        )
        if retry_id is not None:
            duplicate_ids.append(retry_id)
            continue

        if target_similarity >= _TARGET_DUPLICATE_SIMILARITY_THRESHOLD:
            duplicate_ids.append(current.translation_unit_id)
    return duplicate_ids


def normalize_space(value: str) -> str:
    return " ".join(str(value or "").split())


def needs_target_language_retry(
    original: str,
    translated: str,
    target_lang: str,
) -> bool:
    """타겟 언어 대비 결과에 source-language script가 남아 있으면 재시도를 요청한다."""

    return bool(target_language_retry_reasons(original, translated, target_lang))


def target_language_retry_reasons(
    original: str,
    translated: str,
    target_lang: str,
) -> list[str]:
    """Return concrete target-language validation failures for retry prompts."""

    target = _RETRY_TARGET_ALIASES.get((target_lang or "").strip().lower(), "")
    if target == "":
        return []
    if not original.strip() or not translated.strip():
        return []
    if target == "ko":
        reasons: list[str] = []
        foreign_samples = _unexpected_foreign_script_samples(original, translated)
        if foreign_samples:
            reasons.append("unexpected_foreign_script=" + "".join(foreign_samples))
        latin_samples = _untranslated_latin_phrase_samples_for_korean(original, translated)
        if latin_samples:
            reasons.append("untranslated_english_phrase=" + " | ".join(latin_samples))
        return reasons
    source_has_cjk = _contains_hangul(original) or _contains_han(original) or _contains_kana(original)
    if not source_has_cjk:
        return []

    if target == "en":
        reasons = []
        if _contains_hangul(translated):
            reasons.append("hangul_remaining")
        if _contains_han(translated):
            reasons.append("han_remaining")
        if _contains_kana(translated):
            reasons.append("kana_remaining")
        if _contains_cyrillic(translated):
            reasons.append("cyrillic_remaining")
        return reasons
    if target == "ja":
        if not _contains_hangul(translated):
            return []
        return ["hangul_remaining_without_japanese_kana"] if not _contains_kana(translated) else []
    if target == "zh":
        if not _contains_hangul(translated):
            return []
        return ["hangul_remaining_without_chinese_han"] if not _contains_han(translated) else []
    return []
