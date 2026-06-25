"""Active document-local term memory seeded from pre-translation analysis.

Temporary glossary stores raw evidence. Pre-translation analysis is a source-only
snapshot. This module owns the active, updatable memory seeded from that
snapshot and later intended for resolver updates.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from translation_pipeline.common.document_term_memory_structure import (
    clean_memory_kind,
    is_context_only_kind,
    is_target_kind,
    sanitize_document_term_memory,
    sanitize_terms_for_prompt,
)
from translation_pipeline.common.job_artifacts import job_artifact_path, next_numbered_artifact_path
from translation_pipeline.common.retrieval import bm25_rank_documents
from translation_pipeline.common.term_memory_core import _clean_evidence_text, _short_snippet


_SCHEMA_VERSION = "document_term_memory.v2"
_DEFAULT_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory"
_DEFAULT_RESOLVER_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory_resolver"
_MAX_RELEVANT_TERMS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_RELEVANT", "12"))
_MAX_EVIDENCE_SEED_TERMS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_EVIDENCE_SEEDS", "0"))
_MAX_EVIDENCE_SOURCES_PER_ENTRY = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_EVIDENCE_SOURCES", "4"))
_MAX_EVIDENCE_SOURCE_CHARS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_EVIDENCE_SOURCE_CHARS", "220"))
_MIN_INFORMATIVE_EVIDENCE_CHARS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MIN_INFORMATIVE_EVIDENCE_CHARS", "60"))
_AMBIGUOUS_BASE_MIN_TERMS = int(os.getenv("AI_TRANSLATION_INITIAL_GLOSSARY_AMBIGUOUS_BASE_MIN_TERMS", "4"))
_SOURCE_NOTE_ANALYSIS_CANDIDATES_ENABLED = os.getenv(
    "AI_TRANSLATION_DOCUMENT_TERM_MEMORY_SOURCE_NOTE_CANDIDATES",
    "1",
).strip().lower() not in {"0", "false", "no", "off"}
_SOURCE_NOTE_CORE_CONFIDENCE = float(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_SOURCE_NOTE_CORE_CONFIDENCE", "0.9"))
_SOURCE_NOTE_CORE_SIGNAL_RE = re.compile(
    r"\b(?:crucial|critical|central|core|specific|consistent|consistency|"
    r"distinguish|distinct|slang|document-local|world-specific|identity|"
    r"terminology|term|mechanism|plot|character|role|title|proper noun|"
    r"name|named|euphemism|recurring|repeated|persistent|must be consistent)\b",
    flags=re.IGNORECASE,
)
_SEED_EVIDENCE_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")
_KOREAN_VERB_ADJECTIVE_ENDING_RE = re.compile(r"(하다|되다|시키다|받다|주다|보다|있다|없다)$")
_INFLECTABLE_SOURCE_RE = re.compile(
    r"(?:\b\w+(?:ed|ing|er|est|s)\b|\s+)",
    flags=re.IGNORECASE,
)


def _normalize_source(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_document_source(value: Any) -> str:
    """Normalize a source term for document-term-memory matching."""

    return _normalize_source(value)


def _entry_sources(entry: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("source", "source_term", "term", "family_name"):
        value = str(entry.get(key) or "").strip()
        if value:
            values.append(value)
    for list_key in ("source_terms", "aliases"):
        source_terms = entry.get(list_key)
        if isinstance(source_terms, list):
            values.extend(str(item).strip() for item in source_terms if str(item).strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = _normalize_source(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(value)
    return deduped


def _target_group_for_kind(kind: str) -> str:
    return "context" if is_context_only_kind(kind) else "target"


def _source_note_should_force_target(entry: dict[str, Any]) -> bool:
    try:
        confidence = float(entry.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    if confidence < _SOURCE_NOTE_CORE_CONFIDENCE:
        return False
    text = " ".join(
        str(entry.get(key) or "")
        for key in ("document_local_meaning", "meaning", "why_it_matters", "target_language_risk")
    )
    return bool(_SOURCE_NOTE_CORE_SIGNAL_RE.search(text))


def _analysis_candidates_from_source_note(entry: dict[str, Any]) -> list[dict[str, Any]]:
    if not _SOURCE_NOTE_ANALYSIS_CANDIDATES_ENABLED:
        return []
    sources = _entry_sources(entry)
    if not sources:
        return []
    candidate = dict(entry)
    candidate["source"] = sources[0]
    candidate["source_terms"] = sources
    candidate["memory_kind"] = "analysis_candidate"
    candidate["status"] = "analysis_hint"
    candidate["target_decision_needed"] = True
    candidate["needs_review"] = True
    candidate["resolver_priority"] = candidate.get("resolver_priority") or "high"
    candidate["source_note_candidate"] = True
    if _source_note_should_force_target(entry):
        candidate["core_concept"] = True
        candidate["requires_preferred_target"] = True
    return [candidate]


def _source_note_from_participant_role(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Convert pre-analysis role notes into source-side DTM context.

    Pre-analysis may identify document-local roles in ``participants_and_roles``.
    Those notes are not target-language glossary decisions, but they should be
    available to pre-judge when the same source term appears as a DTM candidate.
    """

    if not isinstance(entry, dict):
        return None
    source = str(entry.get("source") or entry.get("source_term") or "").strip()
    if not source:
        return None
    note = dict(entry)
    note["source"] = source
    note["source_terms"] = _entry_sources(note) or [source]
    note["memory_kind"] = "source_meaning"
    note["document_local_meaning"] = str(
        note.get("document_local_meaning")
        or note.get("meaning")
        or note.get("document_local_role")
        or ""
    ).strip()
    note["target_decision_needed"] = False
    note["source_role_note"] = True
    return note


def _compact_evidence(value: Any) -> str:
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = (
                    item.get("text")
                    or item.get("source")
                    or item.get("snippet")
                    or item.get("evidence")
                    or item.get("sentence")
                    or ""
                )
            else:
                text = item
            text = str(text or "").strip()
            if text:
                parts.append(_clean_evidence_text(text)[:_MAX_EVIDENCE_SOURCE_CHARS].rstrip())
        return " | ".join(parts[:_MAX_EVIDENCE_SOURCES_PER_ENTRY])
    return _clean_evidence_text(value)[:_MAX_EVIDENCE_SOURCE_CHARS].rstrip()


def _source_base(value: Any) -> str:
    parts = _normalize_source(value).split()
    if not parts:
        return ""
    first = parts[0]
    if len(first) <= 2 or first.isdigit():
        return ""
    return first


def _ambiguous_source_bases(entries: list[dict[str, Any]]) -> set[str]:
    by_base: dict[str, set[str]] = {}
    for entry in entries:
        for source in _entry_sources(entry):
            base = _source_base(source)
            normalized = _normalize_source(source)
            if not base or not normalized:
                continue
            by_base.setdefault(base, set()).add(normalized)
    return {
        base
        for base, sources in by_base.items()
        if len(sources) >= _AMBIGUOUS_BASE_MIN_TERMS
    }


def _cap_review_confidence(value: Any) -> Any:
    try:
        confidence = float(value)
    except Exception:
        return value
    return min(confidence, 0.74)


def _next_term_id(index: int) -> str:
    return f"dtm_{index + 1:03d}"


def _next_sense_id(index: int) -> str:
    return f"sense_{index + 1:03d}"


def _target_candidate(
    target: str,
    *,
    status: str,
    source: str,
    count: int = 0,
    evidence_refs: list[str] | None = None,
    reason: str = "",
    now: float,
) -> dict[str, Any]:
    candidate = {
        "target": target,
        "status": status,
        "count": count,
        "source": source,
        "evidence_refs": evidence_refs or [],
        "reason": reason,
        "created_at": now,
        "updated_at": now,
    }
    return {key: value for key, value in candidate.items() if value not in ("", [], None)}


def _sense_entry(
    *,
    sense_id: str,
    meaning: str,
    status: str,
    preferred_target: str,
    confidence: Any,
    evidence: str,
    evidence_refs: list[str],
    target_language_risk: str,
    resolver_priority: str,
    source: str,
    now: float,
) -> dict[str, Any]:
    sense = {
        "sense_id": sense_id,
        "meaning": meaning,
        "status": status,
        "preferred_target": preferred_target or None,
        "confidence": confidence,
        "evidence": evidence,
        "evidence_refs": evidence_refs,
        "target_language_risk": target_language_risk,
        "resolver_priority": resolver_priority,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    return {key: value for key, value in sense.items() if value not in ("", [], None)}


def _iter_evidence_entries(memory: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(memory, dict):
        return []
    entries: list[dict[str, Any]] = []
    for bucket in ("pending", "review", "soft_locked", "locked"):
        bucket_entries = memory.get(bucket) or {}
        if not isinstance(bucket_entries, dict):
            continue
        for entry in bucket_entries.values():
            if isinstance(entry, dict) and entry.get("source_term"):
                entries.append(entry)
    entries.sort(
        key=lambda item: (
            -float(item.get("candidate_score") or item.get("confidence") or 0.0),
            -int(item.get("frequency") or 0),
            -int(item.get("token_count") or 0),
            str(item.get("source_term") or ""),
        )
    )
    return entries


def _source_terms_for_seed_evidence(entry: dict[str, Any]) -> list[str]:
    values = [entry.get("source_term"), *(entry.get("aliases") or [])]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        normalized = _normalize_source(text)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result


def _list_texts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _seed_evidence_contains_source(sentence: str, source: str) -> bool:
    normalized_source = _normalize_source(source)
    if len(normalized_source) <= 1:
        return False
    normalized_sentence = _normalize_source(sentence)
    if not normalized_sentence:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_source)}(?![a-z0-9])",
            normalized_sentence,
        )
    )


def _complete_seed_evidence(raw: Any, source_terms: list[str]) -> str:
    text = _clean_evidence_text(raw)
    if not text:
        return ""
    sentences = [part.strip() for part in _SEED_EVIDENCE_SENTENCE_BOUNDARY_RE.split(text) if part.strip()]
    containing = [
        sentence
        for sentence in sentences
        if any(_seed_evidence_contains_source(sentence, source) for source in source_terms)
    ]
    if containing:
        containing.sort(
            key=lambda sentence: (
                len(sentence) < _MIN_INFORMATIVE_EVIDENCE_CHARS,
                abs(min(len(sentence), _MAX_EVIDENCE_SOURCE_CHARS) - 140),
            )
        )
        text = containing[0]
    if len(text) <= _MAX_EVIDENCE_SOURCE_CHARS:
        return text
    return _short_snippet(text, source_terms[0] if source_terms else "", limit=_MAX_EVIDENCE_SOURCE_CHARS)


def _seed_evidence_document(occurrence: dict[str, Any], snippet: str) -> str:
    return " ".join(
        str(item or "")
        for item in (
            occurrence.get("section"),
            occurrence.get("table_title"),
            occurrence.get("element_type"),
            occurrence.get("matched_source"),
            occurrence.get("source_term"),
            snippet,
        )
    )


def _seed_evidence_structural_score(occurrence: dict[str, Any]) -> int:
    element_type = str(occurrence.get("element_type") or "").strip().lower()
    score = 0
    if element_type in {"heading", "title", "section_heading"}:
        score += 4
    if element_type in {"table_header", "header", "column_header", "row_header", "cell"}:
        score += 2
    if occurrence.get("is_header"):
        score += 2
    if occurrence.get("table_title"):
        score += 1
    if occurrence.get("section"):
        score += 1
    return score


def _seed_evidence_informativeness_score(snippet: str) -> int:
    length = len(str(snippet or ""))
    if length >= 80:
        return 4
    if 60 <= length < 80:
        return 3
    if 35 <= length < 60:
        return 1
    return 0


def _representative_seed_evidence(entry: dict[str, Any]) -> list[str]:
    occurrences = entry.get("occurrences") or []
    if not isinstance(occurrences, list):
        return []
    source_terms = _source_terms_for_seed_evidence(entry)
    query = " ".join(
        str(item or "")
        for item in (
            *source_terms,
            *_list_texts(entry.get("candidate_types")),
            *_list_texts(entry.get("reason")),
        )
    )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        snippet = _complete_seed_evidence(
            occurrence.get("surrounding_source")
            or occurrence.get("source_snippet")
            or "",
            source_terms,
        )
        if source_terms and not any(_seed_evidence_contains_source(snippet, source) for source in source_terms):
            continue
        normalized = _normalize_source(snippet)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(
            {
                "snippet": snippet,
                "document": _seed_evidence_document(occurrence, snippet),
                "structural_score": _seed_evidence_structural_score(occurrence),
                "informativeness_score": _seed_evidence_informativeness_score(snippet),
                "sequence": len(candidates),
            }
        )
    if not candidates:
        return []

    bm25_scores = {
        index: score
        for score, index in bm25_rank_documents(
            query,
            [str(candidate["document"]) for candidate in candidates],
        )
    }
    candidates.sort(
        key=lambda candidate: (
            len(str(candidate["snippet"])) < _MIN_INFORMATIVE_EVIDENCE_CHARS,
            -int(candidate["informativeness_score"]),
            -float(bm25_scores.get(int(candidate["sequence"]), 0.0)),
            -int(candidate["structural_score"]),
            abs(min(len(str(candidate["snippet"])), _MAX_EVIDENCE_SOURCE_CHARS) - 140),
            int(candidate["sequence"]),
        )
    )
    return [
        str(candidate["snippet"])
        for candidate in candidates[:_MAX_EVIDENCE_SOURCES_PER_ENTRY]
        if str(candidate["snippet"]).strip()
    ]


def _evidence_entry_to_seed(entry: dict[str, Any]) -> dict[str, Any]:
    evidence = _representative_seed_evidence(entry)
    aliases = [str(item).strip() for item in entry.get("aliases") or [] if str(item).strip()]
    source = str(entry.get("source_term") or "").strip()
    return {
        "source": source,
        "source_terms": [source, *aliases],
        "memory_kind": "raw_evidence_candidate",
        "status": "analysis_hint",
        "document_local_meaning": "",
        "target_decision_needed": True,
        "resolver_priority": "low",
        "confidence": entry.get("candidate_score") or entry.get("confidence"),
        "evidence": evidence,
        "evidence_refs": [str(entry.get("term_id") or "").strip()] if entry.get("term_id") else [],
    }


def _seed_entry(
    *,
    term_id: str,
    entry: dict[str, Any],
    kind: str,
    now: float,
    ambiguous_bases: set[str] | None = None,
) -> dict[str, Any] | None:
    sources = _entry_sources(entry)
    if not sources:
        return None

    source_term = sources[0]
    preferred_target = str(
        entry.get("preferred_target")
        or entry.get("target")
        or ""
    ).strip()
    do_not_translate_as = entry.get("do_not_translate_as")
    if not isinstance(do_not_translate_as, list):
        do_not_translate_as = entry.get("avoid") or []
    if not isinstance(do_not_translate_as, list):
        do_not_translate_as = []
    do_not_confuse = entry.get("do_not_confuse_with_source_terms") or []
    if not isinstance(do_not_confuse, list):
        do_not_confuse = [do_not_confuse]

    source_base = _source_base(source_term)
    base_is_ambiguous = bool(source_base and source_base in (ambiguous_bases or set()))
    needs_review = bool(entry.get("needs_review")) or base_is_ambiguous
    status = "review_required" if preferred_target and needs_review else "initial_seed" if preferred_target else "analysis_hint"
    requested_status = str(entry.get("status") or "").strip()
    if requested_status and requested_status not in {"locked", "soft_locked"}:
        status = requested_status
    if preferred_target and status in {"analysis_hint", "analysis_candidate"}:
        status = "review_required" if needs_review else "initial_seed"
    if preferred_target and needs_review and status == "initial_seed":
        status = "review_required"
    meaning = str(
        entry.get("document_local_meaning")
        or entry.get("meaning")
        or entry.get("document_local_role")
        or ""
    ).strip()
    evidence = _compact_evidence(entry.get("evidence"))
    evidence_refs = [
        str(item).strip()
        for item in (entry.get("evidence_refs") or [])
        if str(item).strip()
    ]
    sense = _sense_entry(
        sense_id=_next_sense_id(0),
        meaning=meaning,
        status=status,
        preferred_target=preferred_target,
        confidence=_cap_review_confidence(entry.get("confidence")) if base_is_ambiguous else entry.get("confidence"),
        evidence=evidence,
        evidence_refs=evidence_refs,
        target_language_risk=str(entry.get("target_language_risk") or "").strip(),
        resolver_priority=str(entry.get("resolver_priority") or "").strip(),
        source="pre_translation_analysis",
        now=now,
    )
    target_candidates: list[dict[str, Any]] = []
    for candidate in entry.get("target_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        target = str(candidate.get("target") or candidate.get("preferred_target") or "").strip()
        if not target:
            continue
        target_candidates.append(
            _target_candidate(
                target,
                status=str(candidate.get("status") or "candidate").strip() or "candidate",
                count=int(candidate.get("count") or 0),
                source="pre_translation_analysis",
                evidence_refs=evidence_refs,
                reason=str(candidate.get("reason") or "").strip(),
                now=now,
            )
        )
    if preferred_target and not any(item.get("target") == preferred_target for item in target_candidates):
        target_candidates.insert(
            0,
            _target_candidate(
                preferred_target,
                status="preferred",
                count=0,
                source="pre_translation_analysis",
                evidence_refs=evidence_refs,
                now=now,
            ),
        )
    seeded = {
        "term_id": term_id,
        "source_term": source_term,
        "source_terms": sources,
        "normalized_sources": [_normalize_source(item) for item in sources],
        "memory_kind": clean_memory_kind(entry.get("memory_kind") or kind, "term"),
        "status": status,
        "meaning": meaning,
        "full_form": str(entry.get("full_form") or "").strip(),
        "document_local_role": str(entry.get("document_local_role") or "").strip(),
        "why_it_matters": str(entry.get("why_it_matters") or entry.get("reason") or "").strip(),
        "target_pattern": str(entry.get("target_pattern") or "").strip(),
        "do_not_confuse_with_source_terms": [
            str(item).strip()
            for item in do_not_confuse
            if str(item).strip()
        ],
        "target_decision_needed": bool(entry.get("target_decision_needed")) or needs_review or not preferred_target,
        "needs_review": needs_review,
        "source_note_candidate": bool(entry.get("source_note_candidate")),
        "core_concept": bool(entry.get("core_concept")),
        "requires_preferred_target": bool(entry.get("requires_preferred_target")),
        "resolver_priority": str(entry.get("resolver_priority") or ("medium" if needs_review else "")).strip(),
        "target_language_risk": str(
            entry.get("target_language_risk")
            or (
                "The source base appears in multiple related document expressions; verify the initial target against local context."
                if base_is_ambiguous
                else ""
            )
        ).strip(),
        "confidence": _cap_review_confidence(entry.get("confidence")) if base_is_ambiguous else entry.get("confidence"),
        "preferred_target": preferred_target or None,
        "active_sense_id": sense.get("sense_id") if sense else None,
        "senses": [sense] if sense else [],
        "target_candidates": target_candidates,
        "do_not_translate_as": [str(item).strip() for item in do_not_translate_as if str(item).strip()],
        "avoid_targets": [
            {
                "target": str(item).strip(),
                "status": "avoid",
                "source": "pre_translation_analysis",
                "created_at": now,
                "updated_at": now,
            }
            for item in do_not_translate_as
            if str(item).strip()
        ],
        "evidence": evidence,
        "evidence_refs": evidence_refs,
        "term_history": [],
        "repair_requests": [],
        "applied_scope_refs": [],
        "created_at": now,
        "updated_at": now,
        "updated_by": "pre_translation_analysis",
    }
    return {key: value for key, value in seeded.items() if value not in ("", [], None)}


def create_document_term_memory(
    pre_analysis: dict[str, Any] | None,
    *,
    job_id: str = "",
    target_lang: str = "",
    evidence_memory: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    analysis = pre_analysis if isinstance(pre_analysis, dict) else {}
    evidence_entries = _iter_evidence_entries(evidence_memory)
    if not analysis and not evidence_entries:
        return None

    now = time.time()
    memory = {
        "schema_version": _SCHEMA_VERSION,
        "job_id": job_id,
        "target_lang": target_lang,
        "source_term_language": (evidence_memory or {}).get("source_term_language") or "",
        "created_at": now,
        "updated_at": now,
        "source": "pre_translation_analysis" if analysis else "temporary_glossary_evidence",
        "document_profile": analysis.get("document_profile") or {},
        "domain_context": analysis.get("domain_context") or [],
        "style_guidance": analysis.get("style_guidance") or [],
        "entries": {},
    }

    raw_entries: list[tuple[str, dict[str, Any]]] = []
    for item in analysis.get("term_memory_seeds") or []:
        if isinstance(item, dict):
            raw_entries.append(("term_memory_seed", item))
    for item in (analysis.get("term_families") or analysis.get("term_groups") or []):
        if isinstance(item, dict):
            raw_entries.append(("term_family", item))
    for item in (analysis.get("initial_document_terms") or analysis.get("entries") or []):
        if isinstance(item, dict):
            raw_entries.append(("initial_term_candidate", item))
    for item in analysis.get("source_meaning_notes") or []:
        if isinstance(item, dict):
            raw_entries.append(("source_meaning", item))
            for candidate in _analysis_candidates_from_source_note(item):
                raw_entries.append(("analysis_candidate", candidate))
    for item in analysis.get("participants_and_roles") or []:
        role_note = _source_note_from_participant_role(item)
        if role_note:
            raw_entries.append(("source_meaning", role_note))
            for candidate in _analysis_candidates_from_source_note(role_note):
                raw_entries.append(("analysis_candidate", candidate))
    for item in analysis.get("acronym_notes") or []:
        if isinstance(item, dict):
            raw_entries.append(("acronym", item))
    selected_evidence_entries = (
        evidence_entries
        if _MAX_EVIDENCE_SEED_TERMS <= 0
        else evidence_entries[:_MAX_EVIDENCE_SEED_TERMS]
    )
    for entry in selected_evidence_entries:
        raw_entries.append(("raw_evidence_candidate", _evidence_entry_to_seed(entry)))

    ambiguous_bases = _ambiguous_source_bases([entry for _kind, entry in raw_entries if isinstance(entry, dict)])
    seen_sources: set[tuple[str, str]] = set()
    for _raw_index, (kind, entry) in enumerate(raw_entries):
        sources = _entry_sources(entry)
        normalized_key = "|".join(sorted(_normalize_source(item) for item in sources))
        kind_group = _target_group_for_kind(clean_memory_kind(entry.get("memory_kind") or kind, "term"))
        dedupe_key = (kind_group, normalized_key)
        if not normalized_key or dedupe_key in seen_sources:
            continue
        seeded = _seed_entry(
            term_id=_next_term_id(len(memory["entries"])),
            entry=entry,
            kind=kind,
            now=now,
            ambiguous_bases=ambiguous_bases,
        )
        if not seeded:
            continue
        seen_sources.add(dedupe_key)
        memory["entries"][seeded["term_id"]] = seeded

    return sanitize_document_term_memory(memory)


def _contains_source(lookup_text: str, source: str) -> bool:
    normalized_source = _normalize_source(source)
    if not normalized_source:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_source)}(?![a-z0-9])",
            lookup_text,
        )
    )


def _entry_matches(entry: dict[str, Any], lookup_text: str) -> bool:
    for source in entry.get("source_terms") or [entry.get("source_term")]:
        if _contains_source(lookup_text, str(source or "")):
            return True
    return False


def _entry_match_score(entry: dict[str, Any], lookup_text: str, primary_lookup_text: str) -> tuple[int, int, int, int]:
    sources = [str(item or "") for item in (entry.get("source_terms") or [entry.get("source_term")])]
    primary_match = any(_contains_source(primary_lookup_text, source) for source in sources)
    context_match = any(_contains_source(lookup_text, source) for source in sources)
    preferred = bool(entry.get("preferred_target"))
    status = str(entry.get("status") or "").strip().lower()
    status_rank = {
        "locked": 100,
        "confirmed": 95,
        "preferred": 90,
        "active": 85,
        "soft_locked": 80,
        "initial_seed": 70,
        "review_required": 55,
        "analysis_candidate": 50,
        "analysis_hint": 20,
    }.get(status, 10)
    longest_source = max((len(source) for source in sources), default=0)
    return (
        2 if primary_match else 1 if context_match else 0,
        1 if preferred else 0,
        status_rank,
        longest_source,
    )


def _entry_has_source_context(entry: dict[str, Any]) -> bool:
    return any(
        str(entry.get(key) or "").strip()
        for key in (
            "meaning",
            "full_form",
            "document_local_role",
        )
    )


def _is_compact_code_like_source(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text) > 16 or re.search(r"\s", text):
        return False
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    return all(not char.islower() for char in letters)


def _term_preferred_scope(entry: dict[str, Any]) -> str:
    """Describe when a preferred target should be applied in translation prompts.

    A compact code/acronym entry can carry full-form aliases for lookup. When
    its preferred target is the same compact code, the preferred value should
    lock the code token itself, not force the full-form alias to remain
    untranslated.
    """

    source = str(entry.get("source_term") or "").strip()
    preferred = str(entry.get("preferred_target") or "").strip()
    if not source or not preferred:
        return ""
    if normalize_document_source(source) != normalize_document_source(preferred):
        return ""
    if not _is_compact_code_like_source(source):
        return ""
    for alias in entry.get("source_terms") or []:
        alias_text = str(alias or "").strip()
        if not alias_text or normalize_document_source(alias_text) == normalize_document_source(source):
            continue
        if not _is_compact_code_like_source(alias_text):
            return "exact_source_term_only"
    return ""


def _preferred_application(entry: dict[str, Any]) -> str:
    """Return how strictly a preferred target should be applied.

    Proper nouns, named groups, and compact codes should stay stable. Slang,
    verbs, and common phrases often need the target meaning to be fixed without
    forcing one Korean surface form into every grammar slot.
    """

    preferred_scope = _term_preferred_scope(entry)
    if preferred_scope:
        return preferred_scope
    source = str(entry.get("source_term") or "").strip()
    preferred = str(entry.get("preferred_target") or "").strip()
    if not source or not preferred:
        return ""
    if _is_compact_code_like_source(source):
        return "exact"
    if re.fullmatch(r"[A-Z][A-Za-z0-9'_-]*(?:\s+[A-Z][A-Za-z0-9'_-]*)*", source):
        return "exact"
    source_has_lower = any(char.islower() for char in source)
    source_is_phrase_or_inflected = bool(_INFLECTABLE_SOURCE_RE.search(source))
    preferred_looks_inflectable = bool(_KOREAN_VERB_ADJECTIVE_ENDING_RE.search(preferred))
    if source_has_lower and (source_is_phrase_or_inflected or preferred_looks_inflectable):
        return "contextual_meaning"
    return "exact"


def _entry_injectable(entry: dict[str, Any]) -> bool:
    policy = str(entry.get("pre_judge_inject_policy") or "").strip().lower()
    if policy in {"blocked", "drop", "drop_candidate", "do_not_inject", "unresolved"}:
        return False
    if entry.get("preferred_target"):
        return True
    kind = str(entry.get("memory_kind") or "").strip().lower()
    status = str(entry.get("status") or "").strip().lower()
    if status in {"blocked", "drop_candidate"}:
        return False
    if kind == "raw_evidence_candidate" and not _entry_has_source_context(entry):
        return False
    return True


def find_relevant_document_terms(
    memory: dict[str, Any] | None,
    texts: list[str],
    *,
    primary_texts: list[str] | None = None,
    limit: int = _MAX_RELEVANT_TERMS,
) -> list[dict[str, Any]]:
    if not isinstance(memory, dict):
        return []
    lookup_text = _normalize_source("\n".join(str(text or "") for text in texts))
    primary_lookup_text = _normalize_source("\n".join(str(text or "") for text in (primary_texts or [])))
    if not lookup_text:
        return []
    entries = memory.get("entries")
    if not isinstance(entries, dict):
        return []

    relevant: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    for entry in entries.values():
        if not isinstance(entry, dict) or not _entry_injectable(entry) or not _entry_matches(entry, lookup_text):
            continue
        item = {
            "source": entry.get("source_term"),
            "source_terms": entry.get("source_terms") or [],
            "status": entry.get("status") or "analysis_hint",
            "memory_kind": entry.get("memory_kind") or "",
            "meaning": entry.get("meaning") or "",
            "full_form": entry.get("full_form") or "",
            "document_local_role": entry.get("document_local_role") or "",
            "why_it_matters": entry.get("why_it_matters") or "",
            "target_pattern": entry.get("target_pattern") or "",
            "do_not_confuse_with_source_terms": entry.get("do_not_confuse_with_source_terms") or [],
            "target_decision_needed": bool(entry.get("target_decision_needed")),
            "needs_review": bool(entry.get("needs_review")),
            "resolver_priority": entry.get("resolver_priority") or "",
            "target_language_risk": entry.get("target_language_risk") or "",
            "pre_judge_inject_policy": entry.get("pre_judge_inject_policy") or "",
            "preferred_scope": _term_preferred_scope(entry),
            "preferred_application": _preferred_application(entry),
            "confidence": entry.get("confidence"),
            "preferred_target": entry.get("preferred_target"),
            "active_sense_id": entry.get("active_sense_id"),
            "active_sense": next(
                (
                    sense
                    for sense in (entry.get("senses") or [])
                    if isinstance(sense, dict)
                    and sense.get("sense_id") == entry.get("active_sense_id")
                ),
                None,
            ),
            "senses": entry.get("senses") or [],
            "target_candidates": entry.get("target_candidates") or [],
            "do_not_translate_as": entry.get("do_not_translate_as") or [],
            "avoid_targets": entry.get("avoid_targets") or [],
            "evidence": entry.get("evidence") or "",
        }
        relevant.append(
            (
                _entry_match_score(entry, lookup_text, primary_lookup_text),
                item,
            )
        )
    relevant.sort(key=lambda item: item[0], reverse=True)
    sanitized = sanitize_terms_for_prompt([item for _score, item in relevant])
    return sanitized[:limit]


def document_term_memory_summary(memory: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(memory, dict):
        return {"entries": 0, "analysis_hint": 0, "analysis_candidate": 0, "soft_locked": 0, "locked": 0}
    entries = memory.get("entries") if isinstance(memory.get("entries"), dict) else {}
    statuses: dict[str, int] = {
        "entries": len(entries),
        "analysis_hint": 0,
        "analysis_candidate": 0,
        "soft_locked": 0,
        "locked": 0,
    }
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "analysis_hint")
        statuses[status] = statuses.get(status, 0) + 1
    return statuses


def document_term_memory_dump_dir() -> Path:
    value = os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_DUMP_DIR", "").strip()
    return Path(value) if value else _DEFAULT_DUMP_DIR


def document_term_resolver_dump_dir() -> Path:
    value = os.getenv("AI_TRANSLATION_DOCUMENT_TERM_RESOLVER_DUMP_DIR", "").strip()
    return Path(value) if value else _DEFAULT_RESOLVER_DUMP_DIR


def _safe_filename_part(value: Any) -> str:
    safe = re.sub(r"[^0-9A-Za-z가-힣_.() -]+", "_", str(value or "").strip())
    safe = re.sub(r"\s+", "_", safe).strip("._- ")
    return safe[:120]


def _artifact_prefix(job_id: str, artifact_label: str = "") -> str:
    safe_job_id = _safe_filename_part(job_id)
    safe_label = _safe_filename_part(artifact_label)
    if safe_label and safe_job_id:
        return f"{safe_label}__{safe_job_id}"
    if safe_job_id:
        return safe_job_id
    if safe_label:
        return safe_label
    return f"document-term-memory-{uuid.uuid4().hex[:12]}"


def _next_resolver_snapshot_index(dump_dir: Path, prefix: str) -> int:
    pattern = re.compile(rf"^{re.escape(prefix)}-resolver\((\d+)\)\.json$")
    max_index = 0
    for path in dump_dir.glob(f"{prefix}-resolver(*).json"):
        match = pattern.match(path.name)
        if not match:
            continue
        max_index = max(max_index, int(match.group(1)))
    return max_index + 1


def save_document_term_memory_to_local_file(
    job_id: str,
    memory: dict[str, Any],
    *,
    artifact_label: str = "",
) -> str:
    if not isinstance(memory, dict) or not memory:
        return ""
    artifact = artifact_label or str(memory.get("_artifact_label") or "")
    path = job_artifact_path(job_id, artifact, "document_term_memory.json")
    payload = {
        **memory,
        "job_id": job_id or memory.get("job_id") or None,
        "artifact_label": artifact_label or memory.get("_artifact_label") or None,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def save_document_term_resolver_snapshot_to_local_file(
    job_id: str,
    memory: dict[str, Any],
    *,
    artifact_label: str = "",
    scope: str = "",
    resolver_result: dict[str, Any] | None = None,
) -> str:
    if not isinstance(memory, dict) or not memory:
        return ""
    artifact = artifact_label or str(memory.get("_artifact_label") or "")
    path = next_numbered_artifact_path(
        job_id,
        artifact,
        subdir="document_term_resolver",
        stem="resolver",
    )
    snapshot_index = int(path.stem.rsplit("_", 1)[-1])
    payload = {
        **memory,
        "job_id": job_id or memory.get("job_id") or None,
        "artifact_label": artifact_label or memory.get("_artifact_label") or None,
        "resolver_scope": scope or None,
        "resolver_snapshot_index": snapshot_index,
        "resolver_result": resolver_result or None,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


__all__ = [
    "create_document_term_memory",
    "document_term_memory_summary",
    "find_relevant_document_terms",
    "normalize_document_source",
    "save_document_term_memory_to_local_file",
    "save_document_term_resolver_snapshot_to_local_file",
]
