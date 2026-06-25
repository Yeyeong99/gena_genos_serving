"""Source-side term extraction for document-local term memory."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from translation_pipeline.common.term_memory_core import (
    _ACRONYM_RE,
    _MAX_OCCURRENCES_PER_TERM,
    _PAREN_PAIR_RE,
    _SCHEMA_VERSION,
    _SEGMENT_SPLIT_RE,
    _WORD_RE,
    _chunk_id,
    _clean_term,
    _contains_token_sequence,
    _has_independent_term_shape,
    _has_hangul,
    _has_repeated_key_token,
    _has_standalone_occurrence,
    _invalid_candidate_reason,
    _is_acronym,
    _is_acronym_noise,
    _is_bad_body_ngram_shape,
    _sample_occurrences_evenly,
    _short_snippet,
    _single_word_can_be_term,
    _term_pattern,
    _token_count,
    _valid_acronym_candidate,
    normalize_source,
)


_KOREAN_PAREN_PAIR_RE = re.compile(
    r"(?P<full>[가-힣A-Za-z0-9·/\-& ]{2,80}?)\s*"
    r"\((?P<abbr>[A-Z][A-Z0-9&/-]{1,})\)"
)
_KOREAN_TRAILING_PARTICLE_RE = re.compile(
    r"(?:에서|으로|부터|까지|에게|께서|은|는|이|가|을|를|과|와|의|에|로|도|만)$"
)
_KOREAN_PREDICATE_ENDINGS = ("합니다", "한다", "했다", "된다", "이다", "있다", "없다")
_SINGLE_TITLECASE_NAME_RE = re.compile(r"^[A-Z][A-Za-z'/-]{2,}$")
_CONTEXT_LINE_RE = re.compile(r"^(PREVIOUS|NEXT):\s*(.*)$", flags=re.IGNORECASE)
_SINGLE_TITLECASE_STOPWORDS = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "although",
    "an",
    "and",
    "another",
    "any",
    "as",
    "at",
    "because",
    "before",
    "but",
    "by",
    "chapter",
    "could",
    "did",
    "do",
    "does",
    "during",
    "each",
    "even",
    "every",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "here",
    "his",
    "how",
    "however",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "more",
    "most",
    "must",
    "no",
    "not",
    "now",
    "of",
    "on",
    "one",
    "only",
    "or",
    "other",
    "our",
    "shall",
    "she",
    "should",
    "since",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "under",
    "was",
    "we",
    "were",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "you",
}


def _source_is_likely_korean(target_lang: str) -> bool:
    normalized = str(target_lang or "").strip().lower()
    return normalized in {"english", "en", "eng", "영어"}


def _token_has_source_term_signal(token: str, *, source_is_korean: bool) -> bool:
    if source_is_korean:
        return _has_hangul(token) or token[:1].isupper() or token.isupper()
    return token[:1].isupper() or token.isupper()


def _is_single_titlecase_name_like(token: str) -> bool:
    cleaned = str(token or "").strip(" \t\r\n,.;:()[]{}\"“”‘’")
    if not _SINGLE_TITLECASE_NAME_RE.fullmatch(cleaned):
        return False
    if cleaned.isupper() or _is_acronym(cleaned):
        return False
    return cleaned.lower() not in _SINGLE_TITLECASE_STOPWORDS


def _source_words_for_segment(segment: str, *, source_is_korean: bool) -> list[str]:
    words = _WORD_RE.findall(segment)
    if not source_is_korean:
        return words
    cleaned: list[str] = []
    for word in words:
        token = word
        if _has_hangul(token):
            token = _KOREAN_TRAILING_PARTICLE_RE.sub("", token)
            for ending in _KOREAN_PREDICATE_ENDINGS:
                if token.endswith(ending) and len(token) > len(ending) + 1:
                    token = token[: -len(ending)]
                    break
        token = token.strip()
        if len(token) >= 2:
            cleaned.append(token)
    return cleaned


def _candidate_frequency_in_text(source_term: str, text: str, *, source_is_korean: bool) -> int:
    if not source_is_korean:
        return len(_term_pattern(source_term).findall(text))
    source_tokens = normalize_source(source_term).split()
    if not source_tokens:
        return 0
    count = 0
    width = len(source_tokens)
    for segment in _SEGMENT_SPLIT_RE.split(str(text or "")):
        words = [normalize_source(word) for word in _source_words_for_segment(segment, source_is_korean=True)]
        if len(words) < width:
            continue
        count += sum(1 for index in range(0, len(words) - width + 1) if words[index : index + width] == source_tokens)
    return count


def _container_type(node: dict[str, Any]) -> str:
    element_type = str(node.get("element_type") or "").strip()
    if element_type in {"table_cell", "column_header", "row_header", "placeholder"}:
        return "table"
    if node.get("table_index") is not None:
        return "table"
    if node.get("slide_index") is not None:
        return "slide"
    if node.get("sheet_name"):
        return "sheet"
    if str(node.get("group") or "").startswith("chart_") or element_type.startswith("chart_"):
        return "chart"
    if element_type in {"text_box", "slide_title"} or node.get("shape_name"):
        return "text_box"
    source = str(node.get("source") or "").strip()
    if source in {"header", "footer"}:
        return source
    return "paragraph"


def _container_id(node: dict[str, Any]) -> str:
    doc_format = str(node.get("doc_format") or "").strip()
    if node.get("table_index") is not None:
        prefix = doc_format or "office"
        if node.get("slide_index") is not None:
            return f"{prefix}:slide:{node.get('slide_index')}:table:{node.get('table_index')}"
        if node.get("sheet_name"):
            return f"{prefix}:sheet:{node.get('sheet_name')}:table:{node.get('table_index')}"
        return f"{prefix}:table:{node.get('table_index')}"
    if node.get("slide_index") is not None:
        return f"pptx:slide:{node.get('slide_index')}"
    if node.get("sheet_name"):
        return f"xlsx:sheet:{node.get('sheet_name')}"
    if node.get("shape_name"):
        return f"{doc_format or 'office'}:shape:{node.get('shape_name')}"
    source = str(node.get("source") or "").strip()
    return f"{doc_format}:{source}" if doc_format and source else source


def _unit_metadata(unit: Any, injection_by_id: dict[int, Any]) -> dict[str, Any]:
    target = unit.targets[0] if getattr(unit, "targets", None) else None
    injection = injection_by_id.get(target.injection_unit_id) if target else None
    node = getattr(injection, "node", {}) if injection is not None else {}
    return node if isinstance(node, dict) else {}


def _candidate_types_for_text(term: str, occurrences: list[dict[str, Any]]) -> set[str]:
    types: set[str] = set()
    if _ACRONYM_RE.fullmatch(term) and not _is_acronym_noise(term):
        types.add("acronym")
    if _token_count(term) > 1:
        types.add("repeated_phrase")
    if any(item.get("element_type") in {"heading", "slide_title"} for item in occurrences):
        types.add("heading_term")
    if any(item.get("is_header") for item in occurrences):
        types.add("table_header_term")
    if any(str(item.get("element_type") or "") in {"table_cell", "column_header", "row_header"} for item in occurrences):
        types.add("table_term")
    return types or {"proper_noun"}


def _score_candidate(
    *,
    source_term: str,
    frequency: int,
    chunk_count: int,
    section_count: int,
    candidate_types: set[str],
) -> tuple[float, dict[str, float], list[str]]:
    token_count = _token_count(source_term)
    frequency_score = min(0.35, frequency / 5.0 * 0.35)
    chunk_score = min(0.15, chunk_count / 5.0 * 0.15)
    section_score = min(0.10, section_count / 6.0 * 0.10)
    heading_bonus = 0.20 if "heading_term" in candidate_types else 0.0
    table_bonus = 0.12 if candidate_types & {"table_header_term", "table_term"} else 0.0
    acronym_bonus = 0.12 if "acronym" in candidate_types else 0.0
    parenthetical_bonus = 0.08 if "parenthetical_pair" in candidate_types else 0.0
    name_bonus = 0.10 if "single_titlecase_proper_noun" in candidate_types else 0.0
    phrase_bonus = min(0.18, max(0, token_count - 1) * 0.08)
    generic_penalty = (
        -0.25
        if token_count == 1
        and not _single_word_can_be_term(source_term)
        and "single_titlecase_proper_noun" not in candidate_types
        else 0.0
    )

    score = max(
        0.0,
        min(
            1.0,
            frequency_score
            + chunk_score
            + section_score
            + heading_bonus
            + table_bonus
            + acronym_bonus
            + parenthetical_bonus
            + name_bonus
            + phrase_bonus
            + generic_penalty,
        ),
    )
    breakdown = {
        "frequency": round(frequency_score, 4),
        "chunk_coverage": round(chunk_score, 4),
        "section_coverage": round(section_score, 4),
        "heading_bonus": round(heading_bonus, 4),
        "table_bonus": round(table_bonus, 4),
        "acronym_bonus": round(acronym_bonus, 4),
        "parenthetical_bonus": round(parenthetical_bonus, 4),
        "name_bonus": round(name_bonus, 4),
        "phrase_bonus": round(phrase_bonus, 4),
        "generic_penalty": round(generic_penalty, 4),
    }
    reasons = [
        key
        for key, value in breakdown.items()
        if value > 0 and key not in {"generic_penalty"}
    ]
    if generic_penalty:
        reasons.append("generic_penalty")
    return round(score, 4), breakdown, reasons


def _should_exclude(source_term: str, frequency: int, candidate_types: set[str]) -> str:
    token_count = _token_count(source_term)
    normalized = normalize_source(source_term)
    invalid_reason = _invalid_candidate_reason(source_term)
    if invalid_reason:
        return invalid_reason
    if _has_repeated_key_token(source_term):
        return "cross_boundary_repeated_token"
    if not source_term or token_count == 0:
        return "empty_or_non_word"
    if len(source_term) < 2:
        return "too_short"
    if (
        token_count == 1
        and not _single_word_can_be_term(source_term)
        and "single_titlecase_proper_noun" not in candidate_types
    ):
        return "generic_single_word"
    if token_count > 1 and not _has_independent_term_shape(source_term):
        return "not_independent_term_shape"
    if "body_ngram" in candidate_types and _is_bad_body_ngram_shape(source_term):
        return "bad_body_ngram_shape"
    words = source_term.split()
    if token_count > 1 and words and _is_acronym(words[-1]):
        return "alias_joined_ngram"
    if token_count == 1 and frequency < 2 and not (
        candidate_types & {"acronym", "heading_term", "table_header_term"}
    ):
        return "single_word_low_frequency"
    if frequency < 2 and not (
        candidate_types
        & {"acronym", "heading_term", "table_header_term", "parenthetical_pair"}
    ):
        return "low_frequency"
    return ""


def _nested_partial_reason(candidate: dict[str, Any], candidates: dict[str, dict[str, Any]]) -> str:
    source = str(candidate.get("source_term") or "")
    if not source or int(candidate.get("standalone_occurrence_count") or 0) > 0:
        return ""
    if candidate.get("aliases"):
        return ""
    token_count = int(candidate.get("token_count") or 0)
    if token_count <= 0:
        return ""
    for other in candidates.values():
        if other is candidate:
            continue
        other_source = str(other.get("source_term") or "")
        if int(other.get("token_count") or 0) <= token_count:
            continue
        if not _contains_token_sequence(other_source, source):
            continue
        if int(other.get("frequency") or 0) < max(1, int(candidate.get("frequency") or 0) * 0.8):
            continue
        return "nested_partial_term"
    return ""


def _prune_candidates(candidates: dict[str, dict[str, Any]], excluded: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pruned: dict[str, dict[str, Any]] = {}
    for term_id, candidate in candidates.items():
        reason = _nested_partial_reason(candidate, candidates)
        if reason == "nested_partial_term":
            excluded.append(
                {
                    "source_term": candidate.get("source_term"),
                    "normalized_source": candidate.get("normalized_source"),
                    "filter_reason": reason,
                    "frequency": candidate.get("frequency"),
                }
            )
            continue
        if reason:
            candidate["review_reason"] = reason
        pruned[term_id] = candidate
    return pruned


def _add_candidate(raw: dict[str, dict[str, Any]], term: str, candidate_type: str) -> str:
    cleaned = _clean_term(term)
    if not cleaned:
        return ""
    normalized = normalize_source(cleaned)
    item = raw.setdefault(normalized, {"source": cleaned, "types": set()})
    if len(cleaned) > len(str(item.get("source") or "")) or cleaned.isupper():
        item["source"] = cleaned
    item.setdefault("types", set()).add(candidate_type)
    return normalized


def _extract_candidates_from_text(text: str, *, source_is_korean: bool = False) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return found

    parenthetical_patterns = [_PAREN_PAIR_RE]
    if source_is_korean:
        parenthetical_patterns.append(_KOREAN_PAREN_PAIR_RE)
    for pattern in parenthetical_patterns:
        for match in pattern.finditer(normalized_text):
            full = _clean_term(match.group("full"))
            abbr = _clean_term(match.group("abbr"))
            full_key = _add_candidate(found, full, "parenthetical_pair")
            abbr_key = _add_candidate(found, abbr, "acronym")
            combined = f"{full} ({abbr})" if full and abbr else ""
            if full_key:
                found[full_key].setdefault("aliases", set()).update(item for item in (abbr, combined) if item)
            if abbr_key:
                found[abbr_key].setdefault("aliases", set()).update(item for item in (full, combined) if item)

    for match in _ACRONYM_RE.finditer(normalized_text):
        term = match.group(0)
        if not _is_acronym_noise(term):
            _add_candidate(found, term, "acronym")

    for segment in _SEGMENT_SPLIT_RE.split(normalized_text):
        words = _source_words_for_segment(segment, source_is_korean=source_is_korean)
        if not source_is_korean:
            for word in words:
                if _is_single_titlecase_name_like(word):
                    _add_candidate(found, word, "single_titlecase_proper_noun")
        min_size = 1 if source_is_korean else 2
        for size in range(min_size, min(6, len(words)) + 1):
            for index in range(0, len(words) - size + 1):
                phrase_words = words[index : index + size]
                if not any(_token_has_source_term_signal(word, source_is_korean=source_is_korean) for word in phrase_words):
                    continue
                candidate_type = "proper_noun" if size == 1 else "repeated_phrase"
                _add_candidate(found, " ".join(phrase_words), candidate_type)
    return found


def _occurrence_payload(unit: Any, node: dict[str, Any], source_term: str) -> dict[str, Any]:
    text = str(getattr(unit, "text", "") or "")
    surrounding_text = _term_evidence_context_text(unit, text, source_term)
    section = str(node.get("section") or "").strip()
    section_path = node.get("section_path")
    if not isinstance(section_path, list):
        section_path = [section] if section else []
    return {
        "chunk_id": _chunk_id(unit),
        "unit_id": getattr(unit, "translation_unit_id", None),
        "section": section or None,
        "section_path": section_path,
        "element_type": str(getattr(unit, "element_type", "") or node.get("element_type") or ""),
        "container_type": _container_type(node),
        "container_id": _container_id(node),
        "table_title": node.get("table_title"),
        "row_index": node.get("row_index") if node.get("row_index") is not None else node.get("row"),
        "col_index": node.get("col_index") if node.get("col_index") is not None else node.get("col"),
        "is_header": bool(node.get("is_header", False)),
        "source_snippet": _short_snippet(surrounding_text or text, source_term, limit=360),
        "surrounding_source": _short_snippet(surrounding_text or text, source_term, limit=520),
        "translated_snippet": None,
        "target_candidate": None,
        "evidence_type": "source_occurrence",
    }


def _term_evidence_context_text(unit: Any, text: str, source_term: str) -> str:
    previous_texts: list[str] = []
    next_texts: list[str] = []
    for line in str(getattr(unit, "context_text", "") or "").splitlines():
        match = _CONTEXT_LINE_RE.match(line.strip())
        if not match:
            continue
        label = match.group(1).upper()
        value = match.group(2).strip()
        if not value:
            continue
        if label == "PREVIOUS":
            previous_texts.append(value)
        elif label == "NEXT":
            next_texts.append(value)
    current_text = str(text or "").strip()
    if _looks_like_mid_sentence_fragment(current_text) and any(
        _term_pattern(source_term).search(item)
        for item in [*previous_texts[-1:], *next_texts[:1]]
    ):
        current_text = ""
    return " ".join(_dedupe_evidence_context_parts([*previous_texts[-1:], current_text, *next_texts[:1]])).strip()


def _looks_like_mid_sentence_fragment(text: str) -> bool:
    stripped = str(text or "").lstrip("\"'“‘([")
    if not stripped:
        return False
    return stripped[:1].islower()


def _dedupe_evidence_context_parts(parts: list[str]) -> list[str]:
    result: list[str] = []
    normalized_result: list[str] = []
    for part in parts:
        text = re.sub(r"\s+", " ", str(part or "")).strip()
        if not text:
            continue
        normalized = normalize_source(text)
        if not normalized:
            continue
        if any(normalized in existing or existing in normalized for existing in normalized_result):
            continue
        result.append(text)
        normalized_result.append(normalized)
    return result


def scan_terms(
    translation_units: Iterable[Any],
    injection_units: Iterable[Any] | None = None,
    *,
    target_lang: str = "",
) -> dict[str, Any]:
    """Scan translation units and return candidate terms plus evidence."""

    units = list(translation_units)
    source_is_korean = _source_is_likely_korean(target_lang)
    injection_by_id = {
        int(getattr(injection, "injection_unit_id")): injection
        for injection in (injection_units or [])
        if getattr(injection, "injection_unit_id", None) is not None
    }
    raw_candidate_types: dict[str, set[str]] = {}
    source_by_normalized: dict[str, str] = {}
    aliases_by_term: dict[str, set[str]] = defaultdict(set)
    occurrence_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    frequency = Counter()
    chunks_by_term: dict[str, set[str]] = defaultdict(set)
    sections_by_term: dict[str, set[str]] = defaultdict(set)
    heading_counts = Counter()
    table_header_counts = Counter()
    table_counts = Counter()
    standalone_counts = Counter()

    for unit in units:
        text = str(getattr(unit, "text", "") or "").strip()
        if not text:
            continue
        node = _unit_metadata(unit, injection_by_id)
        extracted = _extract_candidates_from_text(text, source_is_korean=source_is_korean)
        element_type = str(getattr(unit, "element_type", "") or node.get("element_type") or "")
        if element_type in {"heading", "slide_title"}:
            for normalized in extracted:
                extracted[normalized].setdefault("types", set()).add("heading_term")
        if bool(node.get("is_header")):
            for normalized in extracted:
                extracted[normalized].setdefault("types", set()).add("table_header_term")
        if element_type not in {"heading", "slide_title", "table_cell", "column_header", "row_header"} and not bool(node.get("is_header")):
            for candidate in extracted.values():
                types = candidate.setdefault("types", set())
                if "repeated_phrase" in types and not (types & {"acronym", "parenthetical_pair"}):
                    types.add("body_ngram")
                if "single_titlecase_proper_noun" in types:
                    types.add("body_proper_noun")

        for normalized, candidate in extracted.items():
            source_term = source_by_normalized.setdefault(normalized, str(candidate.get("source") or normalized))
            raw_candidate_types.setdefault(normalized, set()).update(candidate.get("types") or set())
            aliases_by_term.setdefault(normalized, set()).update(candidate.get("aliases") or set())
            frequency[normalized] += _candidate_frequency_in_text(
                source_term,
                text,
                source_is_korean=source_is_korean,
            ) or 1
            chunks_by_term[normalized].add(_chunk_id(unit))
            section = str(node.get("section") or "").strip()
            if section:
                sections_by_term[normalized].add(section)
            if element_type in {"heading", "slide_title"}:
                heading_counts[normalized] += 1
            if bool(node.get("is_header")):
                table_header_counts[normalized] += 1
            if element_type in {"table_cell", "column_header", "row_header"}:
                table_counts[normalized] += 1
            if _has_standalone_occurrence(text, source_term):
                standalone_counts[normalized] += 1
            occurrence_map[normalized].append(_occurrence_payload(unit, node, source_term))

    candidates: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    sorted_terms = sorted(
        frequency.keys(),
        key=lambda item: (-frequency[item], -_token_count(source_by_normalized[item]), item),
    )
    next_id = 1
    for normalized in sorted_terms:
        source_term = source_by_normalized[normalized]
        occurrences = _sample_occurrences_evenly(occurrence_map.get(normalized, []), _MAX_OCCURRENCES_PER_TERM)
        candidate_types = _candidate_types_for_text(source_term, occurrences)
        candidate_types.update(raw_candidate_types.get(normalized, set()))
        aliases = sorted(str(item) for item in aliases_by_term.get(normalized, set()) if str(item).strip())
        filter_reason = _should_exclude(source_term, frequency[normalized], candidate_types)
        if not filter_reason and _is_acronym(source_term) and not _valid_acronym_candidate(
            source_term,
            aliases=aliases,
            frequency=frequency[normalized],
            heading_count=heading_counts[normalized],
            table_header_count=table_header_counts[normalized],
            standalone_count=standalone_counts[normalized],
        ):
            filter_reason = "unvalidated_acronym"
        if filter_reason:
            excluded.append(
                {
                    "source_term": source_term,
                    "normalized_source": normalized,
                    "filter_reason": filter_reason,
                    "frequency": frequency[normalized],
                }
            )
            continue
        score, breakdown, score_reasons = _score_candidate(
            source_term=source_term,
            frequency=frequency[normalized],
            chunk_count=len(chunks_by_term[normalized]),
            section_count=len(sections_by_term[normalized]),
            candidate_types=candidate_types,
        )
        token_count = _token_count(source_term)
        term_id = f"term_{next_id:03d}"
        next_id += 1
        candidates[term_id] = {
            "term_id": term_id,
            "source_term": source_term,
            "normalized_source": normalized,
            "aliases": aliases,
            "status": "pending",
            "candidate_types": sorted(candidate_types),
            "frequency": frequency[normalized],
            "chunk_count": len(chunks_by_term[normalized]),
            "section_count": len(sections_by_term[normalized]),
            "heading_count": heading_counts[normalized],
            "table_header_count": table_header_counts[normalized],
            "table_count": table_counts[normalized],
            "standalone_occurrence_count": standalone_counts[normalized],
            "token_count": token_count,
            "match_priority": int(token_count * 100 + score * 100),
            "candidate_score": score,
            "score_breakdown": breakdown,
            "reason": sorted(set(score_reasons) | set(raw_candidate_types.get(normalized, set()))),
            "confidence": score,
            "version": 1,
            "target_candidates": [],
            "occurrences": occurrences,
        }

    candidates = _prune_candidates(candidates, excluded)

    return {
        "schema_version": _SCHEMA_VERSION,
        "target_lang": target_lang,
        "source_term_language": "ko" if source_is_korean else "en",
        "candidates": candidates,
        "excluded": excluded,
    }


__all__ = ["scan_terms"]
