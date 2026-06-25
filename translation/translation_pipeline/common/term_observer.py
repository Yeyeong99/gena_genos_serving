"""Target-side observation updates for document-local term memory."""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from translation_pipeline.common.term_memory_core import (
    _PAREN_PAIR_RE,
    _SOURCE_SEPARATORS,
    _TARGET_BOUNDARY_SPLIT_RE,
    _TARGET_PAREN_TERM_RE_TEMPLATE,
    _TARGET_SEPARATORS,
    _chunk_id,
    _clean_term,
    _contains_term,
    _has_unrelated_acronym,
    _is_acronym,
    _is_bad_target_candidate,
    _is_target_too_short_for_source,
    _matched_entry_source,
    _short_snippet,
    _strip_korean_clause_prefix,
    normalize_source,
)

_CJK_OR_CYRILLIC_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u0400-\u04ff]"
)
_NON_KOREAN_CJK_OR_CYRILLIC_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u0400-\u04ff]"
)


def _target_language(value: Any) -> str:
    return str(value or "").strip().lower()


def _target_text_has_unexpected_script(target_text: str, target_lang: str) -> bool:
    """Return whether observed target text is unsafe to persist as glossary evidence."""

    text = str(target_text or "")
    if not text:
        return False
    lang = _target_language(target_lang)
    if lang in {"korean", "ko", "kor", "한국어"}:
        return bool(_NON_KOREAN_CJK_OR_CYRILLIC_RE.search(text))
    if lang in {"english", "en", "eng", "영어"}:
        return bool(_CJK_OR_CYRILLIC_RE.search(text))
    return False


def _stored_translation_payload(
    translated: str,
    target_candidate: str | None,
    target_lang: str,
) -> tuple[str | None, str]:
    if not translated:
        return None, "empty_translation"
    translated_text = str(translated or "").strip()
    candidate_text = str(target_candidate or "").strip()
    if translated_text.startswith("[번역 실패") or translated_text.startswith("[Translation failed"):
        return None, "failed_translation_marker"
    if candidate_text.startswith("[번역 실패") or candidate_text.startswith("[Translation failed"):
        return None, "failed_translation_marker"
    if _target_text_has_unexpected_script(translated, target_lang):
        return None, "unexpected_script_in_translation"
    if target_candidate and _target_text_has_unexpected_script(target_candidate, target_lang):
        return None, "unexpected_script_in_target_candidate"
    if not target_candidate:
        return None, "no_target_candidate"
    return translated, ""


def _extract_target_candidate_by_parallel_split(
    source_text: str,
    target_text: str,
    source_term: str,
) -> str | None:
    source_parts = [part.strip() for part in _SOURCE_SEPARATORS.split(source_text) if part.strip()]
    target_parts = [part.strip() for part in _TARGET_SEPARATORS.split(target_text) if part.strip()]
    if len(source_parts) != len(target_parts):
        return None
    normalized_term = normalize_source(source_term)
    for source_part, target_part in zip(source_parts, target_parts):
        if normalize_source(source_part) == normalized_term and target_part:
            return target_part
    return None


def _extract_target_candidate_by_colon(
    source_text: str,
    target_text: str,
    source_term: str,
) -> str | None:
    if ":" not in source_text or ":" not in target_text:
        return None
    source_parts = [part.strip() for part in source_text.split(":", 1)]
    target_parts = [part.strip() for part in target_text.split(":", 1)]
    if len(source_parts) != 2 or len(target_parts) != 2:
        return None
    normalized_term = normalize_source(source_term)
    for source_part, target_part in zip(source_parts, target_parts):
        if target_part and (
            normalize_source(source_part) == normalized_term
            or _contains_term(source_part, source_term)
        ):
            return target_part
    return None


def _parenthetical_abbr(source_text: str, source_term: str) -> str:
    if _is_acronym(source_term):
        return source_term.strip()
    for text in (source_term, source_text):
        match = _PAREN_PAIR_RE.search(str(text or ""))
        if match:
            return _clean_term(match.group("abbr"))
    return ""


def _extract_target_candidate_by_parenthetical(
    source_text: str,
    target_text: str,
    source_term: str,
) -> str | None:
    abbr = _parenthetical_abbr(source_text, source_term)
    if not abbr:
        return None
    paren_pattern = re.compile(rf"\(\s*{re.escape(abbr)}\s*\)", flags=re.IGNORECASE)
    candidates: list[str] = []
    for match in paren_pattern.finditer(target_text):
        prefix = target_text[: match.start()].strip()
        if not prefix:
            continue
        segment_parts = [part.strip() for part in _TARGET_BOUNDARY_SPLIT_RE.split(prefix) if part.strip()]
        raw = segment_parts[-1] if segment_parts else prefix
        raw = _strip_korean_clause_prefix(raw).strip(" \t\r\n,.;:()[]{}")
        raw = re.sub(r"\s+", " ", raw)
        if not raw:
            continue
        candidate = f"{raw}({abbr})"
        if _is_bad_target_candidate(source_term, candidate):
            continue
        candidates.append(candidate)
    if candidates:
        candidates.sort(key=lambda item: (len(item), item))
        return candidates[0]
    fallback_pattern = re.compile(
        _TARGET_PAREN_TERM_RE_TEMPLATE.format(abbr=re.escape(abbr)),
        flags=re.IGNORECASE,
    )
    matches = [match.group("term").strip(" ,.;:") for match in fallback_pattern.finditer(target_text)]
    matches = [item for item in matches if not _is_bad_target_candidate(source_term, item)]
    if not matches:
        return None
    matches.sort(key=lambda item: (len(item), item))
    return matches[0]


def _extract_target_candidate(
    source_text: str,
    target_text: str,
    source_term: str,
) -> str | None:
    if not target_text or normalize_source(source_text) == normalize_source(target_text):
        return None
    parenthetical = _extract_target_candidate_by_parenthetical(source_text, target_text, source_term)
    if parenthetical:
        return parenthetical
    if normalize_source(source_text) == normalize_source(source_term):
        target = target_text.strip()
        return None if _is_bad_target_candidate(source_term, target) else target
    return (
        _extract_target_candidate_by_colon(source_text, target_text, source_term)
        or _extract_target_candidate_by_parallel_split(source_text, target_text, source_term)
    )


def extract_target_candidate(
    source_text: str,
    target_text: str,
    source_term: str,
) -> str | None:
    """Public helper for resolver provenance checks."""

    return _extract_target_candidate(source_text, target_text, source_term)


def _record_target_candidate(entry: dict[str, Any], target: str, chunk_id: str) -> None:
    if not target:
        return
    candidates = entry.setdefault("target_candidates", [])
    for item in candidates:
        if isinstance(item, dict) and item.get("target") == target:
            item["count"] = int(item.get("count") or 0) + 1
            chunks = item.setdefault("chunks", [])
            if chunk_id not in chunks:
                chunks.append(chunk_id)
            return
    candidates.append({"target": target, "count": 1, "chunks": [chunk_id]})


def _observation_key(unit: Any, translated: str, target_candidate: str | None) -> str:
    return "|".join(
        [
            _chunk_id(unit),
            str(getattr(unit, "translation_unit_id", "")),
            translated or "",
            target_candidate or "",
        ]
    )


def _matching_occurrence(
    occurrences: list[Any],
    unit: Any,
    source_term: str,
) -> dict[str, Any] | None:
    unit_id = getattr(unit, "translation_unit_id", None)
    chunk_id = _chunk_id(unit)
    fallback: dict[str, Any] | None = None
    for item in occurrences:
        if not isinstance(item, dict):
            continue
        if item.get("unit_id") != unit_id or item.get("chunk_id") != chunk_id:
            continue
        snippet = str(item.get("source_snippet") or "")
        if snippet and _contains_term(snippet, source_term):
            return item
        fallback = item
    return fallback


def record_observed_translations(
    memory: dict[str, Any] | None,
    units: Iterable[Any],
    translated_by_unit_id: dict[int, str],
) -> None:
    if not isinstance(memory, dict):
        return
    target_lang = str(memory.get("target_lang") or "")
    unit_list = list(units)
    entries_by_bucket = {
        bucket: list((memory.get(bucket) or {}).items())
        for bucket in ("pending", "review", "soft_locked")
    }
    for bucket in ("pending", "review", "soft_locked"):
        entries = entries_by_bucket[bucket]
        for term_id, entry in entries:
            if not isinstance(entry, dict):
                continue
            current_entry = (memory.get(bucket) or {}).get(term_id)
            if current_entry is not entry:
                continue
            source = str(entry.get("source_term") or "").strip()
            if not source:
                continue
            occurrences = entry.setdefault("occurrences", [])
            for unit in unit_list:
                source_text = str(getattr(unit, "text", "") or "")
                matched_source = _matched_entry_source(entry, source_text)
                if not matched_source:
                    continue
                translated = str(
                    translated_by_unit_id.get(
                        int(getattr(unit, "translation_unit_id", -1)),
                        "",
                    )
                    or ""
                )
                target_candidate = _extract_target_candidate(source_text, translated, matched_source)
                if target_candidate and _is_bad_target_candidate(matched_source, target_candidate):
                    target_candidate = None
                    entry["review_reason"] = "bad_target_candidate_shape"
                if target_candidate and _is_target_too_short_for_source(matched_source, target_candidate):
                    target_candidate = None
                    entry["review_reason"] = "target_too_short_for_source"
                if target_candidate and _has_unrelated_acronym(entry, target_candidate, matched_source):
                    target_candidate = None
                    entry["review_reason"] = "target_contains_unrelated_acronym"
                stored_translation, storage_skip_reason = _stored_translation_payload(
                    translated,
                    target_candidate,
                    target_lang,
                )
                if storage_skip_reason in {
                    "unexpected_script_in_translation",
                    "unexpected_script_in_target_candidate",
                }:
                    target_candidate = None
                    entry["review_reason"] = storage_skip_reason
                chunk_id = _chunk_id(unit)
                observation_key = _observation_key(unit, stored_translation or "", target_candidate)
                occurrence = _matching_occurrence(occurrences, unit, matched_source)
                already_observed = (
                    isinstance(occurrence, dict)
                    and occurrence.get("observation_key") == observation_key
                )
                if occurrence is not None:
                    occurrence.update(
                        {
                            "translated_snippet": stored_translation,
                            "target_candidate": target_candidate,
                            "observed_translation": True,
                            "translation_storage_status": "stored" if stored_translation else "skipped",
                            "translation_storage_skip_reason": storage_skip_reason or None,
                            "observation_key": observation_key,
                            "observed_at": time.time(),
                        }
                    )
                    if not occurrence.get("source_snippet"):
                        occurrence["source_snippet"] = _short_snippet(source_text, matched_source, limit=180)
                    if not occurrence.get("element_type"):
                        occurrence["element_type"] = str(getattr(unit, "element_type", "") or "")
                    occurrence["matched_source"] = matched_source
                else:
                    entry["untracked_observed_count"] = int(entry.get("untracked_observed_count") or 0) + 1

                if target_candidate and not storage_skip_reason and not already_observed:
                    _record_target_candidate(entry, target_candidate, chunk_id)
    memory["updated_at"] = time.time()


__all__ = ["extract_target_candidate", "record_observed_translations"]
