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


_SCHEMA_VERSION = "document_term_memory.v1"
_DEFAULT_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory"
_MAX_RELEVANT_TERMS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_RELEVANT", "24"))
_MAX_EVIDENCE_SEED_TERMS = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_MEMORY_MAX_EVIDENCE_SEEDS", "160"))


def _normalize_source(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _entry_sources(entry: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("source", "source_term", "term", "family_name"):
        value = str(entry.get(key) or "").strip()
        if value:
            values.append(value)
    source_terms = entry.get("source_terms")
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


def _next_term_id(index: int) -> str:
    return f"dtm_{index + 1:03d}"


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

    seeded = {
        "term_id": term_id,
        "source_term": source_term,
        "source_terms": sources,
        "normalized_sources": [_normalize_source(item) for item in sources],
        "memory_kind": str(entry.get("memory_kind") or kind or "").strip(),
        "status": "analysis_candidate" if preferred_target else "analysis_hint",
        "meaning": str(
            entry.get("document_local_meaning")
            or entry.get("meaning")
            or entry.get("document_local_role")
            or ""
        ).strip(),
        "full_form": str(entry.get("full_form") or "").strip(),
        "document_local_role": str(entry.get("document_local_role") or "").strip(),
        "why_it_matters": str(entry.get("why_it_matters") or entry.get("reason") or "").strip(),
        "target_pattern": str(entry.get("target_pattern") or "").strip(),
        "do_not_confuse_with_source_terms": [
            str(item).strip()
            for item in do_not_confuse
            if str(item).strip()
        ],
        "target_decision_needed": bool(entry.get("target_decision_needed")),
        "resolver_priority": str(entry.get("resolver_priority") or "").strip(),
        "target_language_risk": str(entry.get("target_language_risk") or "").strip(),
        "confidence": entry.get("confidence"),
        "preferred_target": preferred_target or None,
        "target_candidates": (
            [{"target": preferred_target, "count": 0, "source": "pre_translation_analysis"}]
            if preferred_target
            else []
        ),
        "do_not_translate_as": [str(item).strip() for item in do_not_translate_as if str(item).strip()],
        "evidence": str(entry.get("evidence") or "").strip(),
        "evidence_refs": [
            str(item).strip()
            for item in (entry.get("evidence_refs") or [])
            if str(item).strip()
        ],
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
    for item in analysis.get("term_families") or []:
        if isinstance(item, dict):
            raw_entries.append(("term_family", item))
    for item in analysis.get("initial_document_terms") or []:
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
        )
        if not seeded:
            continue
        seen_sources.add(normalized_key)
        memory["entries"][seeded["term_id"]] = seeded

    return memory


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


def find_relevant_document_terms(
    memory: dict[str, Any] | None,
    texts: list[str],
    *,
    limit: int = _MAX_RELEVANT_TERMS,
) -> list[dict[str, Any]]:
    if not isinstance(memory, dict):
        return []
    lookup_text = _normalize_source("\n".join(str(text or "") for text in texts))
    if not lookup_text:
        return []
    entries = memory.get("entries")
    if not isinstance(entries, dict):
        return []

    relevant: list[dict[str, Any]] = []
    for entry in entries.values():
        if not isinstance(entry, dict) or not _entry_matches(entry, lookup_text):
            continue
        relevant.append(
            {
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
                "resolver_priority": entry.get("resolver_priority") or "",
                "target_language_risk": entry.get("target_language_risk") or "",
                "confidence": entry.get("confidence"),
                "preferred_target": entry.get("preferred_target"),
                "target_candidates": entry.get("target_candidates") or [],
                "do_not_translate_as": entry.get("do_not_translate_as") or [],
                "evidence": entry.get("evidence") or "",
            }
        )
        if len(relevant) >= limit:
            break
    return relevant


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


def save_document_term_memory_to_local_file(job_id: str, memory: dict[str, Any]) -> str:
    if not isinstance(memory, dict) or not memory:
        return ""
    dump_dir = document_term_memory_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id or "").strip())
    if not safe_job_id:
        safe_job_id = f"document-term-memory-{uuid.uuid4().hex[:12]}"
    path = dump_dir / f"{safe_job_id}-document-term-memory.json"
    payload = {
        **memory,
        "job_id": job_id or memory.get("job_id") or None,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


__all__ = [
    "create_document_term_memory",
    "document_term_memory_summary",
    "find_relevant_document_terms",
    "save_document_term_memory_to_local_file",
]
