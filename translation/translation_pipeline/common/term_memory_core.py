"""Shared primitives for document-local term memory.

This module owns only constants and low-level helpers shared by extractor,
store, observer, and resolver modules. Persisted JSON schema remains unchanged.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any


_SCHEMA_VERSION = 1
_MAX_OCCURRENCES_PER_TERM = int(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_MAX_OCCURRENCES", "128"))
_MAX_RELEVANT_TERMS = int(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_MAX_RELEVANT_TERMS", "18"))
_REDIS_TTL_SECONDS = int(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_REDIS_TTL_SECONDS", str(60 * 60 * 6)))
_SOFT_LOCK_MIN_SCORE = float(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_SOFT_LOCK_MIN_SCORE", "0.55"))
_SOFT_LOCK_MIN_OBSERVED_SCORE = float(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_SOFT_LOCK_MIN_OBSERVED_SCORE", "0.4"))
_SOFT_LOCK_MIN_TARGET_COUNT = int(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_SOFT_LOCK_MIN_TARGET_COUNT", "2"))
_SOFT_LOCK_MIN_TARGET_SHARE = float(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_SOFT_LOCK_MIN_TARGET_SHARE", "0.6"))
_SOFT_LOCK_MAX_TARGET_ENTROPY = float(os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_MAX_TARGET_ENTROPY", "0.8"))

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9&'/-]*|[가-힣][가-힣0-9·/-]*")
_HANGUL_RE = re.compile(r"[가-힣]")
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9&/-]{1,}\b")
_PAREN_PAIR_RE = re.compile(
    r"\b(?P<full>[A-Z][A-Za-z0-9&'/-]*(?:\s+[A-Z][A-Za-z0-9&'/-]*){1,8})\s*"
    r"\((?P<abbr>[A-Z][A-Z0-9&/-]{1,})\)"
)

_LEADING_STOPWORDS = {"the", "this", "that", "these", "those", "a", "an"}
_TRAILING_STOPWORDS = {"and", "or", "of", "in", "to", "for", "with", "by", "from", "as"}
_STARTING_FUNCTION_WORDS = {"and", "or", "of", "in", "to", "for", "with", "by", "from", "as"}
_PLACEHOLDER_TERMS = {"blank", "n/a", "na", "none", "null"}
_SENTENCE_MARKERS = {
    "am",
    "are",
    "be",
    "been",
    "being",
    "can",
    "could",
    "did",
    "do",
    "does",
    "had",
    "has",
    "have",
    "is",
    "may",
    "might",
    "must",
    "shall",
    "should",
    "was",
    "were",
    "will",
    "would",
}
_ROMAN_NUMERAL_RE = re.compile(r"(?i)^(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)$")
_SOURCE_SEPARATORS = re.compile(r"\s*,\s*|\s*/\s*|\s+and\s+|\s+&\s+", flags=re.IGNORECASE)
_TARGET_SEPARATORS = re.compile(r"\s*,\s*|\s*/\s*|\s+및\s+|\s+과\s+|\s+와\s+")
_SEGMENT_SPLIT_RE = re.compile(r"[,;:.!?。！？()\[\]\n\r]+")
_TARGET_PAREN_TERM_RE_TEMPLATE = r"(?P<term>[가-힣A-Za-z0-9·/\-&\s]{{1,80}}\(\s*{abbr}\s*\))"
_TARGET_BOUNDARY_SPLIT_RE = re.compile(r"[.。,:;，；]\s*")
_KOREAN_TOPIC_PREFIX_RE = re.compile(r".*(?:은|는|이|가)\s+")
_KOREAN_OBJECT_PREFIX_RE = re.compile(r".*(?:을|를|에게|에서|으로|로)\s+")
_KOREAN_ENGLISH_POSSESSIVE_PREFIX_RE = re.compile(r"^[A-Za-z0-9&/-]+\s+의\s+")
_SENTENCE_END_RE = re.compile(r"[.!?。！？][\"'”’»)\]]*(?:\s+|$)")
_EVIDENCE_CONTEXT_LABEL_RE = re.compile(
    r"\b(?:PREVIOUS|NEXT|SECTION_HEADING|TABLE_TITLE|ABBREVIATION_HINTS):\s*",
    flags=re.IGNORECASE,
)


def normalize_source(value: Any) -> str:
    """Normalize source term text for deduplication and matching."""

    text = str(value or "").strip().lower()
    text = re.sub(r"[-_]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _clean_term(value: str) -> str:
    value = str(value or "").replace("–", "-").replace("—", "-")
    value = re.sub(r"^(Table|Figure)\s+\d+\.\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(Table|Figure)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\d+$", "", value).strip()
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,.;:()[]{}")
    parts = value.split()
    if parts and parts[0].lower() in _LEADING_STOPWORDS:
        value = " ".join(parts[1:]).strip()
    return value.strip()


def _token_count(value: str) -> int:
    return len(_WORD_RE.findall(value))


def _has_hangul(value: Any) -> bool:
    return bool(_HANGUL_RE.search(str(value or "")))


def _is_acronym(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2,}(?:/[A-Z]{2,})?", str(value or "").strip()))


def _is_acronym_noise(value: str) -> bool:
    return bool(_ROMAN_NUMERAL_RE.fullmatch(str(value or "").strip()))


def _is_mixed_case_identifier(value: str) -> bool:
    text = str(value or "").strip()
    return any(char.islower() for char in text) and any(char.isupper() for char in text) and not text.istitle()


def _single_word_can_be_term(value: str) -> bool:
    return (
        (_is_acronym(value) and not _is_acronym_noise(value))
        or _is_mixed_case_identifier(value)
        or (_has_hangul(value) and len(str(value or "").strip()) >= 2)
    )


def _extract_acronyms(value: Any) -> set[str]:
    return {
        match.group(0)
        for match in _ACRONYM_RE.finditer(str(value or ""))
        if not _is_acronym_noise(match.group(0))
    }


def _valid_acronym_candidate(
    source_term: str,
    *,
    aliases: Iterable[str],
    frequency: int,
    heading_count: int,
    table_header_count: int,
    standalone_count: int,
) -> bool:
    if not _is_acronym(source_term) or _is_acronym_noise(source_term):
        return False
    alias_list = [str(item).strip() for item in aliases if str(item).strip()]
    if alias_list:
        return True
    if len(source_term) <= 2:
        return heading_count >= 2 or table_header_count >= 2 or standalone_count >= 2
    return frequency >= 2 or heading_count >= 1 or table_header_count >= 1 or standalone_count >= 1


def _invalid_candidate_reason(source_term: str) -> str:
    raw = _clean_term(source_term)
    lower = raw.lower()
    if not raw:
        return "empty"
    if lower in _PLACEHOLDER_TERMS:
        return "placeholder"
    words = lower.split()
    if not words:
        return "empty"
    if words[-1] in _TRAILING_STOPWORDS:
        return "ends_with_function_word"
    if words[0] in _STARTING_FUNCTION_WORDS:
        return "starts_with_function_word"
    if _is_acronym_noise(raw):
        return "acronym_noise"
    if len(words) >= 8 and not _is_acronym(raw):
        return "too_long_sentence_like_phrase"
    if len(words) >= 4 and any(word in _SENTENCE_MARKERS for word in words):
        return "sentence_pattern"
    return ""


def _has_independent_term_shape(source_term: str) -> bool:
    words = source_term.split()
    if _has_hangul(source_term):
        return True
    if _is_acronym(source_term):
        return not _is_acronym_noise(source_term)
    if len(words) >= 2 and all(word[:1].isupper() or word.isupper() for word in words):
        return True
    if len(words) >= 2 and any(_is_mixed_case_identifier(word) for word in words):
        return True
    return False


def _is_title_like_phrase(source_term: str) -> bool:
    words = [word.strip("()[]:;,.") for word in source_term.split()]
    content_words = [word for word in words if word and word.lower() not in (_LEADING_STOPWORDS | _TRAILING_STOPWORDS)]
    if not content_words:
        return False
    if any(_has_hangul(word) for word in content_words):
        return True
    return all(
        word[:1].isupper()
        or word.isupper()
        or _is_mixed_case_identifier(word)
        for word in content_words
    )


def _is_bad_body_ngram_shape(source_term: str) -> bool:
    words = [word.strip("()[]:;,.") for word in source_term.split()]
    lowers = [word.lower() for word in words if word]
    if not lowers:
        return True
    if lowers[-1] in _TRAILING_STOPWORDS:
        return True
    if lowers[0] in _STARTING_FUNCTION_WORDS:
        return True
    if any(word in _LEADING_STOPWORDS for word in lowers[1:]):
        return True
    return not _is_title_like_phrase(source_term)


def _has_repeated_key_token(source_term: str) -> bool:
    seen: set[str] = set()
    for word in source_term.split():
        stripped = word.strip("()/")
        if not (stripped[:1].isupper() or stripped.isupper() or _is_mixed_case_identifier(stripped)):
            continue
        if stripped in seen:
            return True
        seen.add(stripped)
    return False


def _term_pattern(source_term: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9가-힣]){re.escape(source_term)}(?![A-Za-z0-9가-힣])",
        flags=re.IGNORECASE,
    )


def _contains_term(text: str, source_term: str) -> bool:
    if not text or not source_term:
        return False
    return bool(_term_pattern(source_term).search(text))


def _normalized_tokens(value: str) -> list[str]:
    return normalize_source(value).split()


def _contains_token_sequence(container: str, item: str) -> bool:
    container_tokens = _normalized_tokens(container)
    item_tokens = _normalized_tokens(item)
    if not item_tokens or len(item_tokens) >= len(container_tokens):
        return False
    width = len(item_tokens)
    return any(container_tokens[index : index + width] == item_tokens for index in range(len(container_tokens) - width + 1))


def _has_standalone_occurrence(text: str, source_term: str) -> bool:
    normalized_source = normalize_source(source_term)
    for segment in _SEGMENT_SPLIT_RE.split(str(text or "")):
        if normalize_source(_clean_term(segment)) == normalized_source:
            return True
    return normalize_source(_clean_term(text)) == normalized_source


def _short_snippet(text: str, source_term: str, limit: int = 220) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    match = _term_pattern(source_term).search(normalized)
    if not match:
        return normalized[:limit].rstrip()
    sentence = _sentence_containing_match(normalized, match)
    if sentence:
        if len(sentence) <= limit:
            return sentence
        sentence_match = _term_pattern(source_term).search(sentence)
        if sentence_match:
            return _window_around_match(sentence, sentence_match, limit)
    return _window_around_match(normalized, match, limit)


def _sample_occurrences_evenly(occurrences: list[dict[str, Any]], limit: int = _MAX_OCCURRENCES_PER_TERM) -> list[dict[str, Any]]:
    """Keep occurrence evidence from across the document instead of the first N hits."""

    items = [item for item in occurrences if isinstance(item, dict)]
    if limit <= 0 or len(items) <= limit:
        return items
    if limit == 1:
        return [items[0]]

    last_index = len(items) - 1
    selected_indices: list[int] = []
    seen: set[int] = set()
    for slot in range(limit):
        index = round(slot * last_index / (limit - 1))
        if index in seen:
            continue
        selected_indices.append(index)
        seen.add(index)
    cursor = 0
    while len(selected_indices) < limit and cursor < len(items):
        if cursor not in seen:
            selected_indices.append(cursor)
            seen.add(cursor)
        cursor += 1
    selected_indices.sort()
    return [items[index] for index in selected_indices[:limit]]


def _sentence_containing_match(text: str, match: re.Match[str]) -> str:
    """Return the complete sentence containing a term match when possible."""

    start = 0
    for boundary in _SENTENCE_END_RE.finditer(text[: match.start()]):
        start = boundary.end()
    next_boundary = _SENTENCE_END_RE.search(text, match.end())
    if not next_boundary:
        return ""
    end = next_boundary.end()
    sentence = text[start:end].strip()
    if not sentence:
        return ""
    if start == 0 and not _looks_like_sentence_start(sentence):
        return ""
    return sentence


def _looks_like_sentence_start(value: str) -> bool:
    stripped = str(value or "").lstrip("\"'“‘([")
    if not stripped:
        return False
    first = stripped[0]
    return first.isupper() or _HANGUL_RE.match(first) is not None or first.isdigit()


def _window_around_match(text: str, match: re.Match[str], limit: int) -> str:
    half = max(20, limit // 2)
    start = max(0, match.start() - half)
    end = min(len(text), match.end() + half)
    return text[start:end].strip()


def _clean_evidence_text(value: Any) -> str:
    """Remove context labels that should never become glossary/translation text."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _EVIDENCE_CONTEXT_LABEL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _chunk_id(unit: Any) -> str:
    scope = str(getattr(unit, "context_scope", "") or "").strip()
    return scope or f"chunk:{getattr(unit, 'translation_unit_id', '')}"


def _target_unit_count(target: str) -> int:
    return len(re.findall(r"[가-힣A-Za-z0-9]+", str(target or "")))


def _target_has_clause_shape(target: str) -> bool:
    text = str(target or "").strip()
    if len(text) <= 20:
        return False
    return bool(_KOREAN_TOPIC_PREFIX_RE.search(text) or _KOREAN_OBJECT_PREFIX_RE.search(text))


def _strip_korean_clause_prefix(value: str) -> str:
    text = _KOREAN_TOPIC_PREFIX_RE.sub("", str(value or "").strip())
    text = _KOREAN_OBJECT_PREFIX_RE.sub("", text)
    text = _KOREAN_ENGLISH_POSSESSIVE_PREFIX_RE.sub("", text)
    return text.strip()


def _source_target_length_ratio_bad(source_term: str, target: str) -> bool:
    source_units = max(1, _token_count(source_term))
    target_units = _target_unit_count(target)
    if _is_acronym(source_term):
        if re.search(rf"\(\s*{re.escape(source_term)}\s*\)", target, flags=re.IGNORECASE):
            return target_units >= 8 or len(target) > 45
        return target_units >= 5 or len(target) > 35
    if source_units <= 2 and target_units >= 8:
        return True
    return len(target) > max(45, len(source_term) * 4)


def _is_bad_target_candidate(source_term: str, target: str) -> bool:
    target = str(target or "").strip()
    if not target:
        return True
    if _source_target_length_ratio_bad(source_term, target):
        return True
    if _target_has_clause_shape(target):
        return True
    if _target_expands_short_source(source_term, target):
        return True
    return False


def _is_target_too_short_for_source(source_term: str, target: str) -> bool:
    if not target:
        return True
    if _is_acronym(source_term):
        return False
    source_tokens = _token_count(source_term)
    target_tokens = _target_unit_count(target)
    if source_tokens >= 4 and target_tokens <= 1:
        return True
    if source_tokens >= 3 and target_tokens <= 1 and not re.search(r"\([A-Z0-9&/-]{2,}\)", target):
        return True
    return False


def _target_expands_short_source(source_term: str, target: str) -> bool:
    source = str(source_term or "").strip()
    target_text = str(target or "").strip()
    if not source or not target_text:
        return False
    if _is_acronym(source):
        return False
    if _token_count(source) != 1:
        return False
    if normalize_source(source) == normalize_source(target_text):
        return False
    if not _contains_term(target_text, source):
        return False
    return _target_unit_count(target_text) >= 2


def _entry_source_terms(entry: dict[str, Any]) -> list[str]:
    terms = [str(entry.get("source_term") or "").strip()]
    terms.extend(str(item).strip() for item in entry.get("aliases") or [] if str(item).strip())
    deduped = sorted({term for term in terms if term}, key=lambda item: (-_token_count(item), -len(item), item))
    return deduped


def _entry_source_norms(entry: dict[str, Any]) -> set[str]:
    return {normalize_source(term) for term in _entry_source_terms(entry) if term}


def _has_ambiguous_acronym_aliases(entry: dict[str, Any]) -> bool:
    source = str(entry.get("source_term") or "").strip()
    if not _is_acronym(source):
        return False
    full_aliases = [
        str(item).strip()
        for item in entry.get("aliases") or []
        if str(item).strip() and "(" not in str(item)
    ]
    normalized_aliases = sorted({normalize_source(item) for item in full_aliases if item})
    if len(normalized_aliases) <= 1:
        return False
    for index, left in enumerate(normalized_aliases):
        for right in normalized_aliases[index + 1 :]:
            if _contains_token_sequence(left, right) or _contains_token_sequence(right, left):
                continue
            return True
    return False


def _entry_source_acronyms(entry: dict[str, Any], matched_source: str = "") -> set[str]:
    acronyms: set[str] = set()
    for source in [matched_source, *_entry_source_terms(entry)]:
        acronyms.update(_extract_acronyms(source))
    return acronyms


def _has_unrelated_acronym(entry: dict[str, Any], target: str, matched_source: str = "") -> bool:
    target_acronyms = _extract_acronyms(target)
    if not target_acronyms:
        return False
    source_acronyms = _entry_source_acronyms(entry, matched_source)
    return bool(target_acronyms - source_acronyms)


def _entries_are_related(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_norms = _entry_source_norms(left)
    right_norms = _entry_source_norms(right)
    if not left_norms or not right_norms:
        return False
    if left_norms & right_norms:
        return True
    for left_norm in left_norms:
        for right_norm in right_norms:
            if _contains_token_sequence(left_norm, right_norm) or _contains_token_sequence(right_norm, left_norm):
                return True
    return False


def _matched_entry_source(entry: dict[str, Any], source_text: str) -> str:
    for source_term in _entry_source_terms(entry):
        if _contains_term(source_text, source_term):
            return source_term
    return ""
