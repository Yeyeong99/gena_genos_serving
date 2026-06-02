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
    sanitize_document_term_memory,
    sanitize_terms_for_prompt,
)


_SCHEMA_VERSION = "document_term_memory.v2"
_DEFAULT_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory"
_DEFAULT_RESOLVER_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory_resolver"
_MAX_RELEVANT_TERMS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_RELEVANT", "12"))
_MAX_EVIDENCE_SEED_TERMS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_EVIDENCE_SEEDS", "160"))
_AMBIGUOUS_BASE_MIN_TERMS = int(os.getenv("AI_TRANSLATION_INITIAL_GLOSSARY_AMBIGUOUS_BASE_MIN_TERMS", "4"))


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
                parts.append(text)
        return " | ".join(parts[:3])
    return str(value or "").strip()


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


def _evidence_entry_to_seed(entry: dict[str, Any]) -> dict[str, Any]:
    occurrences = entry.get("occurrences") or []
    evidence = ""
    if occurrences and isinstance(occurrences[0], dict):
        evidence = str(
            occurrences[0].get("surrounding_source")
            or occurrences[0].get("source_snippet")
            or ""
        ).strip()
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
    for item in analysis.get("acronym_notes") or []:
        if isinstance(item, dict):
            raw_entries.append(("acronym", item))
    for entry in evidence_entries[:_MAX_EVIDENCE_SEED_TERMS]:
        raw_entries.append(("raw_evidence_candidate", _evidence_entry_to_seed(entry)))

    ambiguous_bases = _ambiguous_source_bases([entry for _kind, entry in raw_entries if isinstance(entry, dict)])
    seen_sources: set[str] = set()
    for _raw_index, (kind, entry) in enumerate(raw_entries):
        sources = _entry_sources(entry)
        normalized_key = "|".join(sorted(_normalize_source(item) for item in sources))
        if not normalized_key or normalized_key in seen_sources:
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
        seen_sources.add(normalized_key)
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
        if not isinstance(entry, dict) or not _entry_matches(entry, lookup_text):
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
    dump_dir = document_term_memory_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    prefix = _artifact_prefix(job_id, artifact_label or str(memory.get("_artifact_label") or ""))
    path = dump_dir / f"{prefix}-document-term-memory.json"
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
    dump_dir = document_term_resolver_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    prefix = _artifact_prefix(job_id, artifact_label or str(memory.get("_artifact_label") or ""))
    snapshot_index = _next_resolver_snapshot_index(dump_dir, prefix)
    path = dump_dir / f"{prefix}-resolver({snapshot_index}).json"
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
