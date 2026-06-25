"""Pre-translation judge for uncertain Document Term Memory candidates."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from translation_pipeline.common.document_term_memory_actions import apply_term_memory_actions
from translation_pipeline.common.document_term_memory_structure import (
    clean_memory_kind,
    is_target_kind,
    normalize_document_source,
    sanitize_document_term_memory,
)
from translation_pipeline.common.job_artifacts import job_artifact_path
from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.prompts import render_prompt
from translation_pipeline.common.retrieval import bm25_rank_documents
from translation_pipeline.common.term_memory_core import _clean_evidence_text, _short_snippet


_ENABLED_ENV_VAR = "AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_ENABLED"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_DEFAULT_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory_pre_judge"
_MAX_ENTRIES = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MAX_ENTRIES", "0"))
_CHUNK_SIZE = max(1, int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_CHUNK_SIZE", "40")))
_MAX_OCCURRENCES_PER_TERM = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MAX_OCCURRENCES", "4"))
_MAX_EVIDENCE_SOURCE_CHARS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MAX_EVIDENCE_SOURCE_CHARS", "420"))
_MIN_INFORMATIVE_EVIDENCE_CHARS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MIN_INFORMATIVE_EVIDENCE_CHARS", "60"))
_MIN_RELATED_BASE_COUNT = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MIN_RELATED_BASE_COUNT", "3"))
_REQUIRED_TITLECASE_MAX_WORDS = max(
    1,
    int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_REQUIRED_TITLECASE_MAX_WORDS", "5")),
)
_REQUIRED_RETRY_ATTEMPTS = max(
    0,
    int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_REQUIRED_RETRY_ATTEMPTS", "1")),
)
_JUDGE_ALL_PREFERRED_ENV_VAR = "AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_ALL_PREFERRED"
_JUDGE_RAW_CANDIDATES_ENV_VAR = "AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_RAW_CANDIDATES"
_PRE_JUDGE_LOCAL_ACTIONS = {"source_meaning_only", "drop_candidate"}
_TITLECASE_NAMED_ENTITY_RE = re.compile(
    r"^(?:[A-Z][a-z]+|[A-Z][a-z]+'s)(?:\s+(?:[A-Z][a-z]+|[A-Z][a-z]+'s|of|the|and|for|in))*$"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")
_HANGUL_TOKEN_RE = re.compile(r"[가-힣]+")
_ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9&/-]{1,12}$")
_TITLECASE_NOISE = {
    "A",
    "An",
    "And",
    "But",
    "For",
    "If",
    "In",
    "It",
    "No",
    "Not",
    "Of",
    "On",
    "Or",
    "So",
    "The",
    "This",
    "To",
    "What",
    "When",
    "Where",
    "Which",
    "Who",
    "Why",
    "With",
}
_TITLECASE_GENERIC_NOISE = {
    *_TITLECASE_NOISE,
    "Access",
    "Actually",
    "Adults",
    "Again",
    "Agreement",
    "Agreements",
    "Almost",
    "Alone",
    "Always",
    "Annex",
    "Anyway",
    "Assistant",
    "Attendant",
    "Auditorium",
    "Back",
    "Begin",
    "Beyond",
    "Book",
    "Books",
    "Both",
    "Call",
    "Can",
    "Caretaker",
    "Center",
    "Close",
    "Come",
    "Congratulations",
    "Content",
    "Courage",
    "Crew",
    "Crews",
    "December",
    "Delivery",
    "Department",
    "Director",
    "Doctor",
    "Downward",
    "Chapter",
    "Contents",
    "Copyright",
    "Foreword",
    "Introduction",
    "Page",
    "Part",
    "Section",
    "Table",
    "Title",
}
_RAW_PREFERRED_COMMON_WORDS = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "am",
    "an",
    "and",
    "are",
    "area",
    "as",
    "at",
    "attention",
    "be",
    "because",
    "been",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "finally",
    "from",
    "had",
    "hair",
    "has",
    "have",
    "he",
    "her",
    "his",
    "how",
    "if",
    "in",
    "into",
    "immediately",
    "indirect",
    "is",
    "it",
    "its",
    "it's",
    "lets",
    "let's",
    "maybe",
    "me",
    "my",
    "no",
    "not",
    "notice",
    "of",
    "on",
    "or",
    "other",
    "our",
    "page",
    "please",
    "recreation",
    "released",
    "reminder",
    "she",
    "so",
    "special",
    "that",
    "that's",
    "thats",
    "the",
    "their",
    "them",
    "then",
    "there",
    "there's",
    "theres",
    "they",
    "this",
    "to",
    "was",
    "we",
    "were",
    "what",
    "what's",
    "whats",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
}


def document_term_pre_judge_enabled() -> bool:
    value = os.getenv(_ENABLED_ENV_VAR, "1").strip().lower()
    return value not in _DISABLED_VALUES


def document_term_pre_judge_all_preferred_enabled() -> bool:
    value = os.getenv(_JUDGE_ALL_PREFERRED_ENV_VAR, "1").strip().lower()
    return value not in _DISABLED_VALUES


def document_term_pre_judge_raw_candidates_enabled() -> bool:
    value = os.getenv(_JUDGE_RAW_CANDIDATES_ENV_VAR, "1").strip().lower()
    return value not in _DISABLED_VALUES


def document_term_pre_judge_dump_dir() -> Path:
    value = os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_DUMP_DIR", "").strip()
    return Path(value) if value else _DEFAULT_DUMP_DIR


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _safe_filename_part(value: Any) -> str:
    safe = re.sub(r"[^0-9A-Za-z가-힣_.() -]+", "_", str(value or "").strip())
    safe = re.sub(r"\s+", "_", safe).strip("._- ")
    return safe[:120]


def _source_base(value: Any) -> str:
    parts = normalize_document_source(value).split()
    if not parts:
        return ""
    first = re.sub(r"(?:'s|’s)$", "", parts[0])
    if len(first) <= 2 or first.isdigit():
        return ""
    return first


def _entry_sources(entry: dict[str, Any]) -> list[str]:
    values = [entry.get("source_term"), *(entry.get("source_terms") or [])]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = normalize_document_source(text)
        if key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _target_candidates(entry: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in entry.get("target_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        target = _clean_text(candidate.get("target") or candidate.get("preferred_target"))
        key = normalize_document_source(target)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "target": target,
                "status": candidate.get("status"),
                "target_relation": candidate.get("target_relation"),
                "reason": candidate.get("reason"),
                "source": candidate.get("source"),
            }
        )
    preferred = _clean_text(entry.get("preferred_target"))
    preferred_key = normalize_document_source(preferred)
    if preferred and preferred_key not in seen:
        result.insert(0, {"target": preferred, "status": "preferred", "source": "document_term_memory"})
    return result


def _iter_evidence_entries(evidence_memory: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(evidence_memory, dict):
        return []
    entries: list[dict[str, Any]] = []
    for bucket in ("pending", "review", "soft_locked", "locked"):
        bucket_entries = evidence_memory.get(bucket) or {}
        if not isinstance(bucket_entries, dict):
            continue
        entries.extend(entry for entry in bucket_entries.values() if isinstance(entry, dict))
    return entries


def _evidence_document(occurrence: dict[str, Any], snippet: str) -> str:
    return " ".join(
        str(item or "")
        for item in (
            occurrence.get("section"),
            occurrence.get("table_title"),
            occurrence.get("matched_source"),
            occurrence.get("source_term"),
            snippet,
        )
    )


def _evidence_structural_score(occurrence: dict[str, Any]) -> int:
    element_type = str(occurrence.get("element_type") or "").strip().lower()
    structural_score = 0
    if element_type in {"heading", "title", "section_heading"}:
        structural_score += 4
    if element_type in {"table_header", "header", "cell"}:
        structural_score += 2
    if occurrence.get("table_title"):
        structural_score += 1
    return structural_score


def _evidence_informativeness_score(snippet: str) -> int:
    length = len(str(snippet or ""))
    if length >= 120:
        return 4
    if length >= 80:
        return 3
    if length >= _MIN_INFORMATIVE_EVIDENCE_CHARS:
        return 2
    if length >= 35:
        return 1
    return 0


def _snippet_has_complete_sentence(snippet: str) -> bool:
    text = _clean_text(snippet)
    if not text:
        return False
    return bool(re.search(r"[.!?。！？][\"')\]}”’]*$", text))


def _snippet_contains_source_term(snippet: str, source_terms: list[str]) -> bool:
    snippet_key = normalize_document_source(snippet)
    if not snippet_key:
        return False
    return any(normalize_document_source(source) in snippet_key for source in source_terms if source)


def _parse_occurrence_position(value: Any) -> int | None:
    text = str(value or "")
    if not text:
        return None
    for pattern in (r"page:(\d+)", r"slide:(\d+)", r"sheet:(\d+)", r"(\d+)"):
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return None
    return None


def _evidence_item_quality(snippet: str, source_terms: list[str], occurrence: dict[str, Any]) -> dict[str, Any]:
    position = _parse_occurrence_position(occurrence.get("chunk_id")) or _parse_occurrence_position(occurrence.get("unit_id"))
    return {
        "too_short": len(_clean_text(snippet)) < _MIN_INFORMATIVE_EVIDENCE_CHARS,
        "complete_sentence": _snippet_has_complete_sentence(snippet),
        "term_present": _snippet_contains_source_term(snippet, source_terms),
        "position": position,
    }


def _evidence_quality_summary(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    qualities = [
        item.get("evidence_quality")
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_quality"), dict)
    ]
    positions = sorted(
        {
            int(quality["position"])
            for quality in qualities
            if isinstance(quality.get("position"), int)
        }
    )
    position_span = (max(positions) - min(positions)) if len(positions) >= 2 else 0
    if not positions:
        spread = "unknown"
    elif len(positions) == 1 or position_span <= 2:
        spread = "single_area"
    elif len(positions) >= 3:
        spread = "distributed"
    else:
        spread = "multiple_areas"
    total = len(qualities)
    return {
        "selected_count": total,
        "too_short_count": sum(1 for quality in qualities if quality.get("too_short")),
        "complete_sentence_count": sum(1 for quality in qualities if quality.get("complete_sentence")),
        "term_present_count": sum(1 for quality in qualities if quality.get("term_present")),
        "document_spread": spread,
        "positions": positions[:8],
    }


def _source_language(memory: dict[str, Any] | None, evidence_memory: dict[str, Any] | None) -> str:
    for item in (evidence_memory, memory):
        if isinstance(item, dict) and item.get("source_term_language"):
            return str(item.get("source_term_language") or "").strip().lower()
    return ""


def _bm25_query_for_source_language(query: str, source_language: str) -> str:
    if source_language.startswith("en"):
        return re.sub(r"\s+", " ", _HANGUL_TOKEN_RE.sub(" ", query)).strip()
    return query


def _complete_evidence_snippet(raw: Any, source_term: str) -> str:
    """Prefer a complete source sentence as pre-judge evidence.

    Source snippets can be cropped around a match. That is useful for display
    but weak for deciding a term's meaning. For pre-judge we first look at the
    surrounding source text and return the shortest complete sentence containing
    the term when possible.
    """

    text = _clean_evidence_text(raw)
    if not text:
        return ""
    source_key = normalize_document_source(source_term)
    candidates = [part.strip() for part in _SENTENCE_BOUNDARY_RE.split(text) if part.strip()]
    if source_key and candidates:
        containing = [
            sentence
            for sentence in candidates
            if source_key in normalize_document_source(sentence)
        ]
        if containing:
            containing.sort(
                key=lambda sentence: (
                    len(sentence) < _MIN_INFORMATIVE_EVIDENCE_CHARS,
                    abs(min(len(sentence), _MAX_EVIDENCE_SOURCE_CHARS) - 160),
                )
            )
            sentence = containing[0]
            if len(sentence) <= _MAX_EVIDENCE_SOURCE_CHARS:
                return sentence
            return _short_snippet(sentence, source_term, limit=_MAX_EVIDENCE_SOURCE_CHARS)
    if len(text) <= _MAX_EVIDENCE_SOURCE_CHARS:
        return text
    return _short_snippet(text, source_term, limit=_MAX_EVIDENCE_SOURCE_CHARS)


def _evidence_for_entry(
    entry: dict[str, Any],
    evidence_entries: list[dict[str, Any]],
    *,
    source_language: str = "",
) -> list[dict[str, Any]]:
    # seed_analysis가 initial target을 결정할 때 본 문장을 먼저 포함한다.
    # pre_judge는 이 문장들을 보고 해당 번역어가 적합한지 검증해야 한다.
    source_terms = _entry_sources(entry)
    source_keys = {normalize_document_source(source) for source in source_terms}
    source_term = str(entry.get("source_term") or (source_terms or [""])[0] or "").strip()
    query = " ".join(
        str(item or "")
        for item in (
            *source_terms,
            entry.get("meaning"),
            entry.get("why_it_matters"),
            entry.get("target_language_risk"),
        )
    )
    evidence_candidates: list[dict[str, Any]] = []
    seen_snippets: set[str] = set()
    for evidence_entry in evidence_entries:
        evidence_source = normalize_document_source(evidence_entry.get("source_term"))
        aliases = {normalize_document_source(alias) for alias in (evidence_entry.get("aliases") or [])}
        if not source_keys.intersection({evidence_source, *aliases}):
            continue
        for occurrence in evidence_entry.get("occurrences") or []:
            if not isinstance(occurrence, dict):
                continue
            snippet = _complete_evidence_snippet(
                occurrence.get("surrounding_source")
                or occurrence.get("source")
                or occurrence.get("source_snippet"),
                source_term,
            )
            if not snippet:
                continue
            snippet_key = normalize_document_source(snippet)
            if not snippet_key or snippet_key in seen_snippets:
                continue
            seen_snippets.add(snippet_key)
            item = {
                "source": snippet,
                "section": occurrence.get("section"),
                "table_title": occurrence.get("table_title"),
                "element_type": occurrence.get("element_type"),
                "chunk_id": occurrence.get("chunk_id"),
                "unit_id": occurrence.get("unit_id"),
                "evidence_quality": _evidence_item_quality(snippet, source_terms, occurrence),
            }
            evidence_candidates.append(
                {
                    "item": {key: value for key, value in item.items() if value},
                    "document": _evidence_document(occurrence, snippet),
                    "structural_score": _evidence_structural_score(occurrence),
                    "informativeness_score": _evidence_informativeness_score(snippet),
                    "sequence": len(evidence_candidates),
                }
            )
    if not evidence_candidates:
        return []

    bm25_query = _bm25_query_for_source_language(query, source_language)
    bm25_scores = {
        index: score
        for score, index in bm25_rank_documents(
            bm25_query or " ".join(source_terms),
            [str(candidate["document"]) for candidate in evidence_candidates],
        )
    }
    evidence_candidates.sort(
        key=lambda candidate: (
            len(str(candidate["item"].get("source") or "")) < _MIN_INFORMATIVE_EVIDENCE_CHARS,
            -int(candidate.get("informativeness_score") or 0),
            -float(bm25_scores.get(int(candidate["sequence"]), 0.0)),
            -int(candidate["structural_score"]),
            len(str(candidate["item"].get("source") or "")),
            int(candidate["sequence"]),
        )
    )
    return [candidate["item"] for candidate in evidence_candidates[:_MAX_OCCURRENCES_PER_TERM]]


def _related_base_counts(entries: dict[str, Any]) -> dict[str, int]:
    sources_by_base: dict[str, set[str]] = {}
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        for source in _entry_sources(entry):
            base = _source_base(source)
            key = normalize_document_source(source)
            if base and key:
                sources_by_base.setdefault(base, set()).add(key)
    return {base: len(sources) for base, sources in sources_by_base.items()}


def _entry_needs_pre_judge(entry: dict[str, Any], base_counts: dict[str, int]) -> bool:
    if not is_target_kind(entry.get("memory_kind")):
        return False
    if not entry.get("preferred_target"):
        return document_term_pre_judge_raw_candidates_enabled()
    if document_term_pre_judge_all_preferred_enabled():
        return True
    status = str(entry.get("status") or "").strip().lower()
    if status == "review_required" or entry.get("needs_review"):
        return True
    try:
        confidence = float(entry.get("confidence"))
    except Exception:
        confidence = 1.0
    if confidence < 0.8:
        return True
    base = _source_base(entry.get("source_term"))
    return bool(base and base_counts.get(base, 0) >= _MIN_RELATED_BASE_COUNT)


def _is_core_concept_entry(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    if not is_target_kind(entry.get("memory_kind")):
        return False
    return bool(entry.get("core_concept") or entry.get("requires_preferred_target"))


def _entry_semantic_text(entry: dict[str, Any]) -> str:
    return " ".join(
        _clean_text(entry.get(key))
        for key in (
            "meaning",
            "why_it_matters",
            "target_language_risk",
            "document_local_meaning",
            "notes",
        )
    )


def _entry_evidence_text(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in entry.get("evidence") or []:
        if isinstance(item, dict):
            parts.append(_clean_text(item.get("source") or item.get("source_snippet")))
        else:
            parts.append(_clean_text(item))
    return " ".join(part for part in parts if part)


def _titlecase_content_words(source: Any) -> list[str]:
    text = _clean_text(source)
    return [
        word
        for word in re.findall(r"[A-Za-z]+'?s?", text)
        if word.lower() not in {"of", "the", "and", "for", "in"}
    ]


def _is_raw_common_source_word(source: Any) -> bool:
    return normalize_document_source(source).replace(" ", "") in _RAW_PREFERRED_COMMON_WORDS


def _canonical_source_family_key(source: Any) -> str:
    key = normalize_document_source(source)
    key = re.sub(r"^(?:the|a|an)\s+", "", key)
    key = re.sub(r"(?:'s|’s)$", "", key)
    return key


def _entry_source_family_keys(entry: dict[str, Any] | None) -> set[str]:
    if not isinstance(entry, dict):
        return set()
    keys: set[str] = set()
    for source in [entry.get("source_term"), *(entry.get("source_terms") or [])]:
        key = _canonical_source_family_key(source) or normalize_document_source(source)
        if key:
            keys.add(key)
    return keys


def _is_raw_noise_candidate(entry: dict[str, Any], source: str) -> bool:
    kind = clean_memory_kind(entry.get("memory_kind"), "term")
    if kind != "raw_evidence_candidate":
        return False
    if _is_raw_common_source_word(source):
        return True
    if source in _TITLECASE_NOISE or source in _TITLECASE_GENERIC_NOISE:
        return True
    words = _titlecase_content_words(source)
    return len(words) == 1 and words[0] in _TITLECASE_GENERIC_NOISE


def _raw_candidate_acronym_allowed(entry: dict[str, Any], source: str) -> bool:
    """Allow raw uppercase candidates only when they look code-like.

    Raw evidence candidates often include ALL-CAPS prose from announcements or
    tables. Those should not become preferred terms just because they are
    uppercase. Real acronyms usually have no vowels (DPA/TLS/ECSS) or contain
    code characters/digits (ECSS-Q-ST-60-13C).
    """

    if _is_raw_common_source_word(source):
        return False
    stripped = _clean_text(source)
    if not stripped or stripped != stripped.upper():
        return False
    if re.search(r"[0-9&/-]", stripped):
        return True
    letters = re.sub(r"[^A-Z]", "", stripped)
    if len(letters) < 2 or len(letters) > 8:
        return False
    if len(letters) <= 5:
        return True
    return not re.search(r"[AEIOU]", letters)


def _entry_should_require_preferred_target(
    entry: dict[str, Any],
    related_base_count: int,
    evidence_count: int,
) -> bool:
    if not isinstance(entry, dict) or not is_target_kind(entry.get("memory_kind")):
        return False
    kind = clean_memory_kind(entry.get("memory_kind"), "term")
    if kind == "raw_evidence_candidate":
        return False
    return bool(entry.get("core_concept") or entry.get("requires_preferred_target"))


def _pre_judge_entry_sort_key(entry: dict[str, Any]) -> tuple[int, int, int, int, str]:
    kind = clean_memory_kind(entry.get("memory_kind"), "term")
    kind_priority = {
        "analysis_candidate": 0,
        "name": 1,
        "proper_noun": 1,
        "acronym": 2,
        "term": 3,
        "raw_evidence_candidate": 4,
    }.get(kind, 9)
    source = _clean_text(entry.get("source_term"))
    return (
        0 if entry.get("requires_preferred_target") else 1,
        0 if entry.get("needs_review") else 1,
        0 if entry.get("preferred_target") else 1,
        kind_priority,
        source.lower(),
    )


def _merge_text_field(base: dict[str, Any], other: dict[str, Any], key: str) -> None:
    if base.get(key) or not other.get(key):
        return
    base[key] = other.get(key)


def _merge_unique_list(base: dict[str, Any], other: dict[str, Any], key: str, *, limit: int = 12) -> None:
    merged: list[Any] = []
    seen: set[str] = set()
    for item in [*(base.get(key) or []), *(other.get(key) or [])]:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(item)
        if len(merged) >= limit:
            break
    if merged:
        base[key] = merged


def _coalesce_pre_judge_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge exact/alias source variants before asking the LLM to judge them.

    The judge should decide once per reusable source family. Without this,
    entries such as "Giver" / "The Giver", "Socs" / "Soc" / "Socials", or
    source_note + raw candidate for the same term can receive conflicting
    actions in one run. Pre-analysis often provides these alias groups in
    source_terms, so coalescing must use the whole source family, not only the
    primary source_term.
    """

    if not entries:
        return []

    parent = list(range(len(entries)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    key_owner: dict[str, int] = {}
    for index, entry in enumerate(entries):
        keys = _entry_source_family_keys(entry)
        if not keys:
            continue
        for key in keys:
            if key in key_owner:
                union(key_owner[key], index)
            else:
                key_owner[key] = index

    grouped: dict[int, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        if not _entry_source_family_keys(entry):
            continue
        grouped.setdefault(find(index), []).append(entry)

    result: list[dict[str, Any]] = []
    for family_entries in grouped.values():
        family_entries.sort(key=_pre_judge_entry_sort_key)
        base = dict(family_entries[0])
        merged_ids: list[str] = []
        for other in family_entries:
            if other.get("term_id"):
                merged_ids.append(str(other.get("term_id")))
            _merge_text_field(base, other, "meaning")
            _merge_text_field(base, other, "why_it_matters")
            _merge_text_field(base, other, "target_language_risk")
            _merge_unique_list(base, other, "source_terms")
            _merge_unique_list(base, other, "target_candidates", limit=8)
            _merge_unique_list(base, other, "evidence", limit=max(_MAX_OCCURRENCES_PER_TERM, 4))
            base["core_concept"] = bool(base.get("core_concept") or other.get("core_concept"))
            base["requires_preferred_target"] = bool(
                base.get("requires_preferred_target") or other.get("requires_preferred_target")
            )
            base["needs_review"] = bool(base.get("needs_review") or other.get("needs_review"))
            if not base.get("preferred_target") and other.get("preferred_target"):
                base["preferred_target"] = other.get("preferred_target")
        if merged_ids:
            base["merged_term_ids"] = sorted(set(merged_ids))
        family_keys = sorted(
            key for entry in family_entries for key in _entry_source_family_keys(entry)
        )
        if family_keys:
            base["source_family_keys"] = sorted(set(family_keys))
        if isinstance(base.get("evidence"), list):
            base["evidence_quality"] = _evidence_quality_summary(base.get("evidence") or [])
        result.append(base)
    return result


def build_document_term_pre_judge_input(
    memory: dict[str, Any] | None,
    *,
    evidence_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(memory, dict) or not isinstance(memory.get("entries"), dict):
        return {"entries": []}
    entries = memory.get("entries") or {}
    base_counts = _related_base_counts(entries)
    evidence_entries = _iter_evidence_entries(evidence_memory)
    source_language = _source_language(memory, evidence_memory)
    selected: list[dict[str, Any]] = []
    for term_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        source_base = _source_base(entry.get("source_term"))
        related_base_count = base_counts.get(source_base, 0) if source_base else 0
        evidence = _evidence_for_entry(entry, evidence_entries, source_language=source_language)
        requires_preferred_target = _entry_should_require_preferred_target(entry, related_base_count, len(evidence))
        evidence_quality = _evidence_quality_summary(evidence)
        item = {
            "term_id": term_id,
            "source_term": entry.get("source_term"),
            "source_terms": _entry_sources(entry),
            "memory_kind": clean_memory_kind(entry.get("memory_kind"), "term"),
            "core_concept": requires_preferred_target,
            "requires_preferred_target": requires_preferred_target,
            "status": entry.get("status"),
            "needs_review": bool(entry.get("needs_review")),
            "preferred_target": entry.get("preferred_target"),
            "target_candidates": _target_candidates(entry),
            "meaning": entry.get("meaning"),
            "why_it_matters": entry.get("why_it_matters"),
            "target_language_risk": entry.get("target_language_risk"),
            "source_note_candidate": bool(entry.get("source_note_candidate")),
            "confidence": entry.get("confidence"),
            "related_source_base_count": related_base_count,
            "evidence": evidence,
            "evidence_quality": evidence_quality,
        }
        selected.append({key: value for key, value in item.items() if value not in ("", [], None)})
    selected = _coalesce_pre_judge_entries(selected)
    selected.sort(key=_pre_judge_entry_sort_key)
    if _MAX_ENTRIES > 0:
        selected = selected[:_MAX_ENTRIES]
    return {
        "judge_type": "document_term_memory_pre_judge",
        "selection_policy": "all_document_term_memory_entries",
        "source_only": True,
        "source_term_language": source_language or None,
        "job_id": memory.get("job_id"),
        "document_profile": memory.get("document_profile") or {},
        "domain_context": memory.get("domain_context") or [],
        "entries": selected,
    }


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _valid_source_terms(pre_judge_input: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for entry in pre_judge_input.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        for source in [entry.get("source_term"), *(entry.get("source_terms") or [])]:
            key = normalize_document_source(source)
            if key:
                result.add(key)
    return result


def _source_terms_with_preferred(pre_judge_input: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for entry in pre_judge_input.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("preferred_target"):
            continue
        for source in [entry.get("source_term"), *(entry.get("source_terms") or [])]:
            key = normalize_document_source(source)
            if key:
                result.add(key)
    return result


def _entries_by_source(pre_judge_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    alias_entries: dict[str, dict[str, Any]] = {}
    for entry in pre_judge_input.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        primary_key = normalize_document_source(entry.get("source_term"))
        if primary_key and primary_key not in result:
            result[primary_key] = entry
        for source in entry.get("source_terms") or []:
            key = normalize_document_source(source)
            if key and key not in result and key not in alias_entries:
                alias_entries[key] = entry
    for key, entry in alias_entries.items():
        result.setdefault(key, entry)
    return result


def _entries_by_term_id(pre_judge_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in pre_judge_input.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        for term_id in [entry.get("term_id"), *(entry.get("merged_term_ids") or [])]:
            if term_id:
                result.setdefault(str(term_id), entry)
    return result


def _entry_for_action(
    action: dict[str, Any],
    entries_by_source: dict[str, dict[str, Any]],
    entries_by_term_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    term_id = _clean_text(action.get("term_id"))
    if term_id and term_id in entries_by_term_id:
        return entries_by_term_id[term_id]
    return entries_by_source.get(normalize_document_source(action.get("source_term") or action.get("source")))


def _action_group_key(action: dict[str, Any], entry: dict[str, Any] | None = None) -> str:
    if isinstance(entry, dict):
        term_ids = sorted(
            {
                _clean_text(term_id)
                for term_id in [entry.get("term_id"), *(entry.get("merged_term_ids") or [])]
                if _clean_text(term_id)
            }
        )
        if term_ids:
            return f"entry_ids:{'|'.join(term_ids)}"
        family_keys = sorted(_entry_source_family_keys(entry))
        if family_keys:
            return f"family:{'|'.join(family_keys)}"
    term_id = _clean_text(action.get("term_id"))
    if term_id:
        return f"id:{term_id}"
    source_key = normalize_document_source(action.get("source_term") or action.get("source"))
    return f"source:{source_key}"


def _prefer_terminal_action(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
    entry: dict[str, Any] | None,
) -> dict[str, Any]:
    if current is None:
        return incoming
    current_type = _clean_text(current.get("type"))
    incoming_type = _clean_text(incoming.get("type"))
    priority = {
        "drop_candidate": 0,
        "source_meaning_only": 1,
        "mark_preferred": 2,
        "no_update": 3,
    }
    if priority.get(incoming_type, 99) < priority.get(current_type, 99):
        return incoming
    return current


def _dedupe_pre_judge_actions(
    actions: list[dict[str, Any]],
    entries_by_source: dict[str, dict[str, Any]],
    entries_by_term_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    terminal_by_key: dict[str, dict[str, Any]] = {}
    update_by_key: dict[str, dict[str, Any]] = {}
    for action in actions:
        action_type = _clean_text(action.get("type"))
        entry = _entry_for_action(action, entries_by_source, entries_by_term_id)
        key = _action_group_key(action, entry)
        if action_type == "update_sense":
            update_by_key.setdefault(key, action)
            continue
        if action_type in {"no_update", "mark_preferred", *_PRE_JUDGE_LOCAL_ACTIONS}:
            terminal_by_key[key] = _prefer_terminal_action(terminal_by_key.get(key), action, entry)
    return [*terminal_by_key.values(), *update_by_key.values()]


def _dedupe_sanitized_pre_judge_result(result: dict[str, Any], pre_judge_input: dict[str, Any]) -> None:
    entries_by_source = _entries_by_source(pre_judge_input)
    entries_by_term_id = _entries_by_term_id(pre_judge_input)
    result["actions"] = _dedupe_pre_judge_actions(
        [action for action in (result.get("actions") or []) if isinstance(action, dict)],
        entries_by_source,
        entries_by_term_id,
    )


def _source_covered_by_core_concept(pre_judge_input: dict[str, Any], source_key: str) -> bool:
    if not source_key:
        return False
    for entry in pre_judge_input.get("entries") or []:
        if not _is_core_concept_entry(entry):
            continue
        for source in entry.get("source_terms") or []:
            if normalize_document_source(source) == source_key:
                return True
    return False


def _entry_source_keys(entry: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for source in [entry.get("source_term"), *(entry.get("source_terms") or [])]:
        key = normalize_document_source(source)
        if key:
            keys.add(key)
    return keys


def _required_target_entries(pre_judge_input: dict[str, Any]) -> list[dict[str, Any]]:
    return []


def _action_satisfies_required_target(action: dict[str, Any]) -> bool:
    action_type = _clean_text(action.get("type"))
    if action_type != "mark_preferred":
        return False
    return bool(_clean_text(action.get("target") or action.get("preferred_target")))


def _missing_required_target_entries(
    pre_judge_input: dict[str, Any],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    satisfied_keys: set[str] = set()
    for action in actions:
        if not isinstance(action, dict) or not _action_satisfies_required_target(action):
            continue
        action_key = normalize_document_source(action.get("source_term") or action.get("source"))
        if action_key:
            satisfied_keys.add(action_key)

    missing: list[dict[str, Any]] = []
    for entry in _required_target_entries(pre_judge_input):
        source_keys = _entry_source_keys(entry)
        if source_keys and source_keys.intersection(satisfied_keys):
            continue
        missing.append(entry)
    return missing


def _focused_required_pre_judge_input(
    pre_judge_input: dict[str, Any],
    missing_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "judge_type": "document_term_memory_pre_judge_required_retry",
        "selection_policy": "missing_required_preferred_targets_only",
        "source_only": True,
        "job_id": pre_judge_input.get("job_id"),
        "document_profile": pre_judge_input.get("document_profile") or {},
        "domain_context": pre_judge_input.get("domain_context") or [],
        "entries": missing_entries,
        "retry_instruction": (
            "Every entry in this retry input is a target-capable core concept "
            "without preferred_target. Return exactly one mark_preferred action "
            "for each entry. Do not use source_meaning_only, drop_candidate, or no_update."
        ),
    }


def _mark_unresolved_required_entries(
    memory: dict[str, Any],
    missing_entries: list[dict[str, Any]],
    *,
    reason: str,
) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    now = time.time()
    for missing in missing_entries:
        entry = _find_entry_by_source(memory, missing.get("source_term"), action_type="mark_preferred")
        if not isinstance(entry, dict):
            continue
        entry["status"] = "unresolved_preferred_required"
        entry["target_decision_needed"] = True
        entry["needs_review"] = True
        entry["pre_judge_inject_policy"] = "unresolved"
        entry["updated_by"] = "term_pre_judge"
        entry["updated_at"] = now
        entry.setdefault("term_history", []).append(
            {
                "action": "pre_judge_required_target_unresolved",
                "status": "unresolved",
                "detail": reason,
                "created_at": now,
                "updated_by": "term_pre_judge",
            }
        )
        unresolved.append(
            {
                "term_id": entry.get("term_id"),
                "source_term": entry.get("source_term"),
                "source_terms": entry.get("source_terms"),
                "reason": reason,
            }
        )
    return unresolved


def _looks_like_titlecase_named_entity(source: Any) -> bool:
    text = _clean_text(source)
    if not text or text in _TITLECASE_NOISE:
        return False
    if not _TITLECASE_NAMED_ENTITY_RE.match(text):
        return False
    content_words = [
        word
        for word in re.findall(r"[A-Za-z]+'?s?", text)
        if word not in {"of", "the", "and", "for", "in"}
    ]
    return bool(content_words) and any(word not in _TITLECASE_NOISE for word in content_words)


def _drop_candidate_allowed(entry: dict[str, Any] | None) -> bool:
    return True


def _sanitize_pre_judge_actions(parsed: dict[str, Any], pre_judge_input: dict[str, Any]) -> dict[str, Any]:
    allowed_types = {"no_update", "mark_preferred", "update_sense", *_PRE_JUDGE_LOCAL_ACTIONS}
    allowed_relations = {"same_meaning_variant", "acceptable_variant", "different_sense"}
    valid_sources = _valid_source_terms(pre_judge_input)
    sources_with_preferred = _source_terms_with_preferred(pre_judge_input)
    entries_by_source = _entries_by_source(pre_judge_input)
    entries_by_term_id = _entries_by_term_id(pre_judge_input)
    actions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for action in parsed.get("actions") or []:
        if not isinstance(action, dict):
            continue
        action_type = _clean_text(action.get("type"))
        source_key = normalize_document_source(action.get("source_term") or action.get("source"))
        if action_type not in allowed_types:
            rejected.append({"action": action, "reason": "unsupported_action"})
            continue
        if source_key not in valid_sources:
            rejected.append({"action": action, "reason": "source_term_not_in_pre_judge_input"})
            continue
        if action_type == "no_update" and source_key not in sources_with_preferred:
            rejected.append({"action": action, "reason": "raw_candidate_no_update_not_allowed"})
            continue
        entry = _entry_for_action(action, entries_by_source, entries_by_term_id)
        if action_type == "mark_preferred" and entry is not None and not is_target_kind(entry.get("memory_kind")):
            rejected.append({"action": action, "reason": "context_entry_cannot_have_preferred_target"})
            continue
        if action_type == "drop_candidate" and not _drop_candidate_allowed(entry):
            rejected.append({"action": action, "reason": "drop_candidate_not_allowed"})
            continue
        relation = _clean_text(action.get("target_relation"))
        if relation and relation not in allowed_relations:
            rejected.append({"action": action, "reason": "unsupported_target_relation"})
            continue
        if action_type in {"add_target_candidate", "mark_preferred"} and not _clean_text(
            action.get("target") or action.get("preferred_target")
        ):
            rejected.append({"action": action, "reason": "target_required"})
            continue
        sanitized = dict(action)
        if action_type == "mark_preferred" and not relation:
            sanitized["target_relation"] = "same_meaning_variant"
        if action_type == "mark_preferred" and not _clean_text(sanitized.get("status")):
            sanitized["status"] = "preferred"
        if action_type == "add_target_candidate" and not relation:
            sanitized["target_relation"] = "acceptable_variant"
        actions.append(sanitized)
    actions = _dedupe_pre_judge_actions(actions, entries_by_source, entries_by_term_id)
    return {
        "judge_type": parsed.get("judge_type") or "document_term_memory_pre_judge",
        "actions": actions,
        "rejected_actions": rejected,
        "caveats": parsed.get("caveats") or [],
    }


def _find_entry_by_source(
    memory: dict[str, Any],
    source_term: Any,
    *,
    action_type: str = "",
) -> dict[str, Any] | None:
    source_key = normalize_document_source(source_term)
    if not source_key:
        return None
    entries = memory.get("entries")
    if not isinstance(entries, dict):
        return None
    exact_matches: list[dict[str, Any]] = []
    fallback: dict[str, Any] | None = None
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        if normalize_document_source(entry.get("source_term")) == source_key:
            exact_matches.append(entry)
            continue
        if fallback is None:
            for source in _entry_sources(entry):
                if normalize_document_source(source) == source_key:
                    fallback = entry
                    break
    if exact_matches:
        if action_type in _PRE_JUDGE_LOCAL_ACTIONS:
            for entry in exact_matches:
                if clean_memory_kind(entry.get("memory_kind"), "term") == "raw_evidence_candidate":
                    return entry
            for entry in exact_matches:
                if not _is_core_concept_entry(entry):
                    return entry
        for entry in exact_matches:
            if _is_core_concept_entry(entry):
                return entry
        return exact_matches[0]
    if fallback is not None:
        return fallback
    return None


def _family_target_entries(memory: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    family_keys = _entry_source_family_keys(entry)
    if not family_keys:
        return []
    entries = memory.get("entries")
    if not isinstance(entries, dict):
        return []
    result: list[dict[str, Any]] = []
    for candidate in entries.values():
        if not isinstance(candidate, dict) or candidate is entry:
            continue
        if not is_target_kind(candidate.get("memory_kind")):
            continue
        candidate_keys = _entry_source_family_keys(candidate)
        if family_keys.intersection(candidate_keys):
            result.append(candidate)
    return result


def _entry_authority_rank(entry: dict[str, Any] | None) -> int:
    if not isinstance(entry, dict):
        return 0
    kind = clean_memory_kind(entry.get("memory_kind"), "term")
    if entry.get("source_note_candidate") or kind == "analysis_candidate":
        return 4
    if kind in {"name", "proper_noun", "acronym", "term"}:
        return 3
    if kind == "raw_evidence_candidate":
        return 1
    return 2


def _preferred_family_target_from_stronger_entry(
    memory: dict[str, Any],
    entry: dict[str, Any],
) -> str:
    entry_rank = _entry_authority_rank(entry)
    for sibling in _family_target_entries(memory, entry):
        if _entry_authority_rank(sibling) <= entry_rank:
            continue
        preferred = _clean_text(sibling.get("preferred_target"))
        if preferred:
            return preferred
    return ""


def _ensure_target_candidate(entry: dict[str, Any], target: str, reason: str, now: float) -> None:
    target_candidates = entry.setdefault("target_candidates", [])
    if any(
        isinstance(candidate, dict)
        and normalize_document_source(candidate.get("target")) == normalize_document_source(target)
        for candidate in target_candidates
    ):
        return
    target_candidates.append(
        {
            "target": target,
            "status": "preferred",
            "source": "term_pre_judge",
            "reason": reason,
            "created_at": now,
            "updated_at": now,
        }
    )


def _mark_pre_judge_reviewed_entries(memory: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    now = time.time()
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = _clean_text(action.get("type"))
        if action_type not in {"mark_preferred", "no_update", *_PRE_JUDGE_LOCAL_ACTIONS}:
            continue
        entry = _find_entry_by_source(
            memory,
            action.get("source_term") or action.get("source"),
            action_type=action_type,
        )
        if not isinstance(entry, dict):
            continue
        target = _clean_text(action.get("target") or action.get("preferred_target") or entry.get("preferred_target"))
        if action_type == "no_update" and not entry.get("preferred_target"):
            continue
        if action_type == "mark_preferred" and not is_target_kind(entry.get("memory_kind")):
            continue
        if action_type == "source_meaning_only":
            entry["pre_judge_inject_policy"] = "source_meaning_only"
            entry["target_decision_needed"] = False
            entry["needs_review"] = False
            entry["status"] = "source_meaning_only"
            if action.get("meaning"):
                entry["meaning"] = _clean_text(action.get("meaning"))
            if action.get("reason"):
                entry["why_it_matters"] = _clean_text(action.get("reason"))
            for sibling in _family_target_entries(memory, entry):
                if _entry_authority_rank(sibling) > _entry_authority_rank(entry):
                    continue
                sibling["pre_judge_inject_policy"] = "source_meaning_only"
                sibling["target_decision_needed"] = False
                sibling["needs_review"] = False
                sibling["status"] = "source_meaning_only"
                sibling["updated_by"] = "term_pre_judge"
                sibling["updated_at"] = now
        elif action_type == "drop_candidate":
            entry["pre_judge_inject_policy"] = "blocked"
            entry["target_decision_needed"] = False
            entry["needs_review"] = False
            entry["status"] = "blocked"
            if action.get("reason"):
                entry["why_it_matters"] = _clean_text(action.get("reason"))
            for sibling in _family_target_entries(memory, entry):
                if _entry_authority_rank(sibling) > _entry_authority_rank(entry):
                    continue
                sibling["pre_judge_inject_policy"] = "blocked"
                sibling["target_decision_needed"] = False
                sibling["needs_review"] = False
                sibling["status"] = "blocked"
                sibling["updated_by"] = "term_pre_judge"
                sibling["updated_at"] = now
        else:
            entry.pop("pre_judge_inject_policy", None)
            if action_type == "mark_preferred" and target:
                stronger_family_target = _preferred_family_target_from_stronger_entry(memory, entry)
                if stronger_family_target:
                    target = stronger_family_target
                entry["preferred_target"] = target
                _ensure_target_candidate(entry, target, _clean_text(action.get("reason")), now)
                for sibling in _family_target_entries(memory, entry):
                    if _entry_authority_rank(sibling) > _entry_authority_rank(entry):
                        continue
                    sibling.pop("pre_judge_inject_policy", None)
                    sibling["preferred_target"] = target
                    sibling["needs_review"] = False
                    sibling["target_decision_needed"] = False
                    sibling["status"] = "preferred"
                    sibling["updated_by"] = "term_pre_judge"
                    sibling["updated_at"] = now
                    _ensure_target_candidate(sibling, target, _clean_text(action.get("reason")), now)
                    sibling.setdefault("term_history", []).append(
                        {
                            "action": "pre_judge_family_preferred",
                            "status": "applied",
                            "detail": target,
                            "payload": action,
                            "created_at": now,
                            "updated_by": "term_pre_judge",
                        }
                    )
        entry["needs_review"] = False
        entry["target_decision_needed"] = False
        entry["status"] = _clean_text(action.get("status")) or ("preferred" if action_type == "mark_preferred" else "initial_seed")
        if action_type == "source_meaning_only":
            entry["status"] = "source_meaning_only"
        elif action_type == "drop_candidate":
            entry["status"] = "blocked"
        entry["updated_by"] = "term_pre_judge"
        entry["updated_at"] = now
        for sense in entry.get("senses") or []:
            if not isinstance(sense, dict):
                continue
            if normalize_document_source(sense.get("preferred_target")) == normalize_document_source(target):
                sense["status"] = entry["status"]
                sense["updated_at"] = now
        for candidate in entry.get("target_candidates") or []:
            if not isinstance(candidate, dict):
                continue
            if normalize_document_source(candidate.get("target")) == normalize_document_source(target):
                candidate["source"] = "term_pre_judge"
                candidate["updated_at"] = now
            elif candidate.get("source") == "term_resolver":
                candidate["source"] = "term_pre_judge"
                candidate["updated_at"] = now
        entry.setdefault("term_history", []).append(
            {
                "action": "pre_judge_first_target" if action_type == "mark_preferred" else "pre_judge_confirm_target",
                "status": "applied",
                "detail": target,
                "payload": action,
                "created_at": now,
                "updated_by": "term_pre_judge",
            }
        )


def _save_pre_judge_snapshot(
    memory: dict[str, Any] | None,
    pre_judge_input: dict[str, Any],
    prompt: str,
    parsed: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> str:
    job_id = _safe_filename_part((memory or {}).get("job_id")) or f"pre-judge-{uuid.uuid4().hex[:12]}"
    artifact = _safe_filename_part((memory or {}).get("_artifact_label"))
    path = job_artifact_path(job_id, artifact, "pre_judge.json")
    payload = {
        "job_id": (memory or {}).get("job_id"),
        "artifact_label": (memory or {}).get("_artifact_label"),
        "pre_judge_input": pre_judge_input,
        "prompt": prompt,
        "proposal": parsed,
        "result": result,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def _input_with_entries(pre_judge_input: dict[str, Any], entries: list[dict[str, Any]], judge_type: str | None = None) -> dict[str, Any]:
    chunk_input = dict(pre_judge_input)
    chunk_input["entries"] = entries
    if judge_type:
        chunk_input["judge_type"] = judge_type
    return chunk_input


def _chunk_entries(entries: list[dict[str, Any]], size: int = _CHUNK_SIZE) -> list[list[dict[str, Any]]]:
    return [entries[index : index + size] for index in range(0, len(entries), size)]


def _merge_sanitized_actions(target: dict[str, Any], addition: dict[str, Any]) -> None:
    seen: set[tuple[str, str, str]] = {
        (
            _clean_text(action.get("type")),
            normalize_document_source(action.get("source_term") or action.get("source")),
            normalize_document_source(action.get("target") or action.get("preferred_target")),
        )
        for action in target.get("actions") or []
        if isinstance(action, dict)
    }
    for action in addition.get("actions") or []:
        if not isinstance(action, dict):
            continue
        key = (
            _clean_text(action.get("type")),
            normalize_document_source(action.get("source_term") or action.get("source")),
            normalize_document_source(action.get("target") or action.get("preferred_target")),
        )
        if key in seen:
            continue
        seen.add(key)
        target.setdefault("actions", []).append(action)
    target.setdefault("rejected_actions", []).extend(addition.get("rejected_actions") or [])
    target.setdefault("caveats", []).extend(addition.get("caveats") or [])


async def _judge_input_once(
    sem: Any,
    session: aiohttp.ClientSession,
    *,
    target_lang: str,
    pre_judge_input: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    prompt = render_prompt(
        "document_term_pre_judge.jinja",
        target_lang=target_lang,
        pre_judge_input_json=json.dumps(pre_judge_input, ensure_ascii=False, indent=2),
    )
    raw = await llm_call_async(sem, session, "", prompt)
    return prompt, _parse_json_object(raw)


async def _run_pre_judge_chunks(
    sem: Any,
    session: aiohttp.ClientSession,
    *,
    target_lang: str,
    pre_judge_input: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    sanitized: dict[str, Any] = {
        "judge_type": "document_term_memory_pre_judge",
        "actions": [],
        "rejected_actions": [],
        "caveats": [],
    }
    chunk_results: list[dict[str, Any]] = []
    first_prompt = ""
    entries = [entry for entry in (pre_judge_input.get("entries") or []) if isinstance(entry, dict)]
    chunks = _chunk_entries(entries)
    for chunk_index, chunk_entries in enumerate(chunks, start=1):
        chunk_input = _input_with_entries(pre_judge_input, chunk_entries)
        chunk_started_at = time.perf_counter()
        chunk_payload: dict[str, Any] = {
            "chunk_index": chunk_index,
            "chunk_total": len(chunks),
            "entry_count": len(chunk_entries),
        }
        try:
            prompt, parsed = await _judge_input_once(
                sem,
                session,
                target_lang=target_lang,
                pre_judge_input=chunk_input,
            )
            if not first_prompt:
                first_prompt = prompt
        except Exception as exc:
            parsed = None
            chunk_payload["error"] = str(exc)
        if parsed:
            chunk_sanitized = _sanitize_pre_judge_actions(parsed, chunk_input)
            _merge_sanitized_actions(sanitized, chunk_sanitized)
            chunk_payload["actions"] = len(chunk_sanitized.get("actions") or [])
            chunk_payload["rejected_actions"] = len(chunk_sanitized.get("rejected_actions") or [])
        else:
            chunk_payload["non_json"] = True
            chunk_payload["actions"] = 0
            chunk_payload["rejected_actions"] = 0
        chunk_payload["elapsed"] = round(time.perf_counter() - chunk_started_at, 3)
        chunk_results.append(chunk_payload)
    return sanitized, chunk_results, first_prompt


async def _run_required_retry_chunks(
    sem: Any,
    session: aiohttp.ClientSession,
    *,
    target_lang: str,
    pre_judge_input: dict[str, Any],
    sanitized: dict[str, Any],
    attempt: int,
) -> list[dict[str, Any]]:
    missing_required = _missing_required_target_entries(
        pre_judge_input,
        sanitized.get("actions") or [],
    )
    retry_results: list[dict[str, Any]] = []
    if not missing_required:
        return retry_results

    chunks = _chunk_entries(missing_required)
    for chunk_index, missing_chunk in enumerate(chunks, start=1):
        focused_input = _focused_required_pre_judge_input(pre_judge_input, missing_chunk)
        retry_started_at = time.perf_counter()
        retry_payload: dict[str, Any] = {
            "attempt": attempt,
            "chunk_index": chunk_index,
            "chunk_total": len(chunks),
            "missing_before_retry": [
                {
                    "term_id": entry.get("term_id"),
                    "source_term": entry.get("source_term"),
                    "source_terms": entry.get("source_terms"),
                }
                for entry in missing_chunk
            ],
        }
        try:
            _, retry_parsed = await _judge_input_once(
                sem,
                session,
                target_lang=target_lang,
                pre_judge_input=focused_input,
            )
        except Exception as exc:
            retry_parsed = None
            retry_payload["error"] = str(exc)
        if retry_parsed:
            retry_sanitized = _sanitize_pre_judge_actions(retry_parsed, focused_input)
            _merge_sanitized_actions(sanitized, retry_sanitized)
            retry_payload["proposal"] = retry_sanitized
        else:
            retry_payload["proposal"] = None
            retry_payload["non_json"] = True
        retry_payload["elapsed"] = round(time.perf_counter() - retry_started_at, 3)
        retry_results.append(retry_payload)
    return retry_results


async def run_document_term_pre_judge(
    sem: Any,
    session: aiohttp.ClientSession | None,
    memory: dict[str, Any] | None,
    *,
    target_lang: str,
    evidence_memory: dict[str, Any] | None = None,
    apply: bool = True,
) -> dict[str, Any] | None:
    if not document_term_pre_judge_enabled():
        log_info(f"[Document Term Pre-Judge] disabled: {_ENABLED_ENV_VAR}=0")
        return None
    if not isinstance(memory, dict) or sem is None or session is None:
        return None
    pre_judge_input = build_document_term_pre_judge_input(memory, evidence_memory=evidence_memory)
    if not pre_judge_input.get("entries"):
        log_info("[Document Term Pre-Judge] skipped: no uncertain initial terms")
        return None
    started_at = time.perf_counter()
    sanitized, chunk_results, first_prompt = await _run_pre_judge_chunks(
        sem,
        session,
        target_lang=target_lang,
        pre_judge_input=pre_judge_input,
    )
    _dedupe_sanitized_pre_judge_result(sanitized, pre_judge_input)
    retry_results: list[dict[str, Any]] = []
    for attempt in range(1, _REQUIRED_RETRY_ATTEMPTS + 1):
        attempt_results = await _run_required_retry_chunks(
            sem,
            session,
            target_lang=target_lang,
            pre_judge_input=pre_judge_input,
            sanitized=sanitized,
            attempt=attempt,
        )
        if not attempt_results:
            break
        retry_results.extend(attempt_results)
        _dedupe_sanitized_pre_judge_result(sanitized, pre_judge_input)

    unresolved_required_entries = _missing_required_target_entries(
        pre_judge_input,
        sanitized.get("actions") or [],
    )
    result: dict[str, Any] = {"proposal": sanitized, "chunk_results": chunk_results}
    if retry_results:
        result["required_retry_results"] = retry_results
    if apply:
        actions = sanitized.get("actions") or []
        resolver_actions = [
            action
            for action in actions
            if isinstance(action, dict) and _clean_text(action.get("type")) not in _PRE_JUDGE_LOCAL_ACTIONS
        ]
        result["apply_result"] = apply_term_memory_actions(memory, resolver_actions)
        _mark_pre_judge_reviewed_entries(memory, sanitized.get("actions") or [])
        if unresolved_required_entries:
            unresolved_terms = _mark_unresolved_required_entries(
                memory,
                unresolved_required_entries,
                reason="required_preferred_target_missing_after_pre_judge_retry",
            )
            result["unresolved_required_terms"] = unresolved_terms
            memory["_pre_judge_unresolved_required_terms"] = unresolved_terms
        memory["_last_pre_judge_result"] = result
        sanitize_document_term_memory(memory)
    snapshot_path = _save_pre_judge_snapshot(
        memory,
        pre_judge_input,
        first_prompt or f"<chunked document-term pre-judge: chunks={len(chunk_results)}>",
        sanitized,
        result,
    )
    if isinstance(memory, dict):
        memory["_pre_judge_dump_path"] = snapshot_path
    if sanitized.get("rejected_actions"):
        log_info(
            "[Document Term Pre-Judge] rejected actions "
            f"count={len(sanitized.get('rejected_actions') or [])}"
        )
    if retry_results:
        log_info(
            "[Document Term Pre-Judge] required target retry "
            f"attempts={len(retry_results)} "
            f"unresolved={len(unresolved_required_entries)}"
        )
    if unresolved_required_entries:
        log_info(
            "[Document Term Pre-Judge] unresolved required targets "
            f"count={len(unresolved_required_entries)}"
        )
    log_info(
        "[Document Term Pre-Judge] complete "
        f"entries={len(pre_judge_input.get('entries') or [])} "
        f"chunks={len(chunk_results)} "
        f"actions={len(sanitized.get('actions') or [])} "
        f"elapsed={time.perf_counter() - started_at:.2f}s "
        f"snapshot={snapshot_path}"
    )
    return result


__all__ = [
    "build_document_term_pre_judge_input",
    "document_term_pre_judge_enabled",
    "run_document_term_pre_judge",
]
