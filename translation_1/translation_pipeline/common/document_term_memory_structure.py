"""Structural rules for Document Term Memory entries.

This module keeps schema-level constraints separate from LLM resolver logic.
It does not decide document meaning; it only prevents incompatible fields from
being stored or injected.
"""

from __future__ import annotations

import time
from typing import Any

from translation_pipeline.common.term_memory_core import _clean_evidence_text

TARGET_ENTRY_KINDS = {"term", "acronym", "raw_evidence_candidate", "analysis_candidate", "resolver_added"}
CONTEXT_ONLY_ENTRY_KINDS = {"term_family", "source_note", "source_meaning"}
CONTEXT_ONLY_ACTIONS = {
    "add_family_evidence",
    "update_family_pattern",
    "update_note",
    "no_update",
    "request_repair",
}
TARGET_ACTIONS = {
    "add_sense",
    "update_sense",
    "set_active_sense",
    "add_target_candidate",
    "mark_preferred",
    "mark_avoid",
    "request_repair",
    "no_update",
}


def clean_memory_kind(value: Any, fallback: str = "term") -> str:
    raw = str(value or "").strip()
    if raw in {"term_memory_seed", "initial_term_candidate"}:
        return "term"
    if raw == "source_meaning":
        return "source_note"
    if raw:
        return raw
    return fallback


def normalize_document_source(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def is_context_only_kind(kind: Any) -> bool:
    return clean_memory_kind(kind) in CONTEXT_ONLY_ENTRY_KINDS


def is_target_kind(kind: Any) -> bool:
    return clean_memory_kind(kind) in TARGET_ENTRY_KINDS


def allowed_actions_for_kind(kind: Any) -> set[str]:
    return CONTEXT_ONLY_ACTIONS if is_context_only_kind(kind) else TARGET_ACTIONS


def target_action_allowed_for_entry(entry: dict[str, Any], action_type: str) -> bool:
    return action_type in allowed_actions_for_kind(entry.get("memory_kind"))


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _status_rank(status: Any) -> int:
    value = str(status or "").strip().lower()
    return {
        "locked": 100,
        "confirmed": 95,
        "preferred": 90,
        "active": 85,
        "soft_locked": 80,
        "initial_seed": 70,
        "review_required": 60,
        "analysis_candidate": 55,
        "analysis_hint": 20,
    }.get(value, 10)


def _kind_rank(kind: Any) -> int:
    value = clean_memory_kind(kind, "term")
    return {
        "term": 60,
        "acronym": 50,
        "resolver_added": 45,
        "raw_evidence_candidate": 30,
        "source_note": 20,
        "source_meaning": 20,
        "term_family": 10,
    }.get(value, 25)


def _prompt_entry_rank(entry: dict[str, Any]) -> tuple[int, int, int, int]:
    preferred_rank = 1 if entry.get("preferred_target") else 0
    source_len = len(str(entry.get("source") or entry.get("source_term") or ""))
    return (
        preferred_rank,
        _status_rank(entry.get("status")),
        _kind_rank(entry.get("memory_kind")),
        source_len,
    )


def _merge_prompt_entry(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in ("source_terms", "do_not_confuse_with_source_terms", "do_not_translate_as", "target_candidates", "avoid_targets", "senses"):
        values: list[Any] = []
        seen: set[str] = set()
        for source in (base.get(key) or []), (incoming.get(key) or []):
            if not isinstance(source, list):
                continue
            for item in source:
                marker = str(item)
                if marker in seen:
                    continue
                seen.add(marker)
                values.append(item)
        if values:
            merged[key] = values
    for key in ("meaning", "full_form", "document_local_role", "why_it_matters", "target_pattern", "target_language_risk", "evidence"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming.get(key)
    return merged


def _dedupe_terms_for_prompt(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for term in terms:
        source = term.get("source") or term.get("source_term")
        key = normalize_document_source(source)
        if not key:
            continue
        existing = by_source.get(key)
        if existing is None:
            by_source[key] = term
            order.append(key)
            continue
        if _prompt_entry_rank(term) > _prompt_entry_rank(existing):
            by_source[key] = _merge_prompt_entry(term, existing)
        else:
            by_source[key] = _merge_prompt_entry(existing, term)
    return [by_source[key] for key in order if key in by_source]


def _candidate_targets_for_prompt(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return target candidates that are safe to expose as an option set.

    A single review-required target is not a real choice; presenting it as a
    suggestion tends to poison the first translation and leaves the resolver
    with only echoed evidence. Multiple candidates are different: they give the
    translator a context-sensitive choice without forcing one target.
    """

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entry.get("target_candidates") or []:
        if not isinstance(item, dict):
            continue
        target = _clean_text(item.get("target") or item.get("preferred_target"))
        if not target:
            continue
        status = str(item.get("status") or "candidate").strip().lower()
        if status in {"avoid", "deprecated"}:
            continue
        key = normalize_document_source(target)
        if not key or key in seen:
            continue
        candidates.append(
            {
                "target": target,
                "status": status or "candidate",
                "source": _clean_text(item.get("source")),
                "confidence": item.get("confidence"),
                "reason": _clean_text(item.get("reason")),
            }
        )
        seen.add(key)

    preferred = _clean_text(entry.get("preferred_target"))
    preferred_key = normalize_document_source(preferred)
    if preferred and preferred_key and preferred_key not in seen:
        candidates.insert(
            0,
            {
                "target": preferred,
                "status": "unverified_initial",
                "source": "initial_glossary",
                "confidence": entry.get("confidence"),
                "reason": "initial target requires review",
            },
        )
    return candidates


def _entry_sources(entry: dict[str, Any]) -> list[str]:
    values = [entry.get("source_term"), *(entry.get("source_terms") or [])]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = normalize_document_source(text)
        if key and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _next_term_id(entries: dict[str, Any]) -> str:
    index = len(entries) + 1
    while f"dtm_{index:03d}" in entries:
        index += 1
    return f"dtm_{index:03d}"


def _strip_target_fields(entry: dict[str, Any]) -> None:
    entry.pop("preferred_target", None)
    entry.pop("active_sense_id", None)
    entry["target_candidates"] = [
        candidate
        for candidate in (entry.get("target_candidates") or [])
        if isinstance(candidate, dict)
        and str(candidate.get("status") or "").strip().lower() not in {"preferred", "avoid", "deprecated"}
    ]
    for sense in entry.get("senses") or []:
        if not isinstance(sense, dict):
            continue
        sense.pop("preferred_target", None)
        if str(sense.get("status") or "").strip().lower() in {"preferred", "active", "confirmed", "analysis_candidate"}:
            sense["status"] = "analysis_hint"
    entry["status"] = "analysis_hint"


def _remove_preferred_from_avoid(entry: dict[str, Any]) -> None:
    preferred = normalize_document_source(entry.get("preferred_target"))
    if not preferred:
        return
    entry["do_not_translate_as"] = [
        item
        for item in (entry.get("do_not_translate_as") or [])
        if normalize_document_source(item) != preferred
    ]
    entry["avoid_targets"] = [
        item
        for item in (entry.get("avoid_targets") or [])
        if not isinstance(item, dict) or normalize_document_source(item.get("target")) != preferred
    ]


def _family_child_terms(entry: dict[str, Any]) -> list[str]:
    family_key = normalize_document_source(entry.get("source_term"))
    return [
        source
        for source in _entry_sources(entry)
        if normalize_document_source(source) != family_key
    ]


def _index_exact_term_entries(entries: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for term_id, entry in entries.items():
        if not isinstance(entry, dict) or is_context_only_kind(entry.get("memory_kind")):
            continue
        source = normalize_document_source(entry.get("source_term"))
        if source and source not in index:
            index[source] = str(term_id)
    return index


def _attach_family_to_existing_children(entries: dict[str, Any], family_id: str, family: dict[str, Any]) -> None:
    exact = _index_exact_term_entries(entries)
    for child in _family_child_terms(family):
        child_id = exact.get(normalize_document_source(child))
        if not child_id:
            continue
        child_entry = entries.get(child_id)
        if not isinstance(child_entry, dict):
            continue
        child_entry.setdefault("family_ids", [])
        if family_id not in child_entry["family_ids"]:
            child_entry["family_ids"].append(family_id)
        child_entry.setdefault("do_not_confuse_with_source_terms", [])
        for sibling in _family_child_terms(family):
            if normalize_document_source(sibling) == normalize_document_source(child):
                continue
            if sibling not in child_entry["do_not_confuse_with_source_terms"]:
                child_entry["do_not_confuse_with_source_terms"].append(sibling)


def _create_missing_family_children(entries: dict[str, Any], family_id: str, family: dict[str, Any], now: float) -> None:
    exact = _index_exact_term_entries(entries)
    for child in _family_child_terms(family):
        child_key = normalize_document_source(child)
        if not child_key or child_key in exact:
            continue
        term_id = _next_term_id(entries)
        entries[term_id] = {
            "term_id": term_id,
            "source_term": child,
            "source_terms": [child],
            "normalized_sources": [child_key],
            "memory_kind": "term",
            "status": "analysis_hint",
            "meaning": family.get("meaning") or "",
            "why_it_matters": family.get("why_it_matters") or "",
            "target_pattern": family.get("target_pattern") or "",
            "target_decision_needed": True,
            "resolver_priority": family.get("resolver_priority") or "",
            "target_language_risk": family.get("target_language_risk") or "",
            "family_ids": [family_id],
            "do_not_confuse_with_source_terms": [
                sibling
                for sibling in _family_child_terms(family)
                if normalize_document_source(sibling) != child_key
            ],
            "senses": [],
            "target_candidates": [],
            "do_not_translate_as": [],
            "avoid_targets": [],
            "term_history": [],
            "repair_requests": [],
            "applied_scope_refs": [],
            "created_at": now,
            "updated_at": now,
            "updated_by": "dtm_structure",
        }
        exact[child_key] = term_id


def _remove_sibling_targets_from_avoid(entries: dict[str, Any]) -> None:
    for entry in entries.values():
        if not isinstance(entry, dict) or is_context_only_kind(entry.get("memory_kind")):
            continue
        sibling_targets: set[str] = set()
        for family_id in entry.get("family_ids") or []:
            family = entries.get(family_id)
            if not isinstance(family, dict):
                continue
            for sibling in _family_child_terms(family):
                sibling_key = normalize_document_source(sibling)
                if sibling_key == normalize_document_source(entry.get("source_term")):
                    continue
                for candidate_entry in entries.values():
                    if (
                        isinstance(candidate_entry, dict)
                        and normalize_document_source(candidate_entry.get("source_term")) == sibling_key
                        and candidate_entry.get("preferred_target")
                    ):
                        sibling_targets.add(normalize_document_source(candidate_entry.get("preferred_target")))
        if not sibling_targets:
            continue
        entry["do_not_translate_as"] = [
            item
            for item in (entry.get("do_not_translate_as") or [])
            if normalize_document_source(item) not in sibling_targets
        ]
        entry["avoid_targets"] = [
            item
            for item in (entry.get("avoid_targets") or [])
            if not isinstance(item, dict) or normalize_document_source(item.get("target")) not in sibling_targets
        ]


def sanitize_document_term_memory(memory: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(memory, dict):
        return memory
    entries = memory.get("entries")
    if not isinstance(entries, dict):
        return memory
    now = time.time()
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        entry["memory_kind"] = clean_memory_kind(entry.get("memory_kind"), "term")
        if is_context_only_kind(entry.get("memory_kind")):
            _strip_target_fields(entry)
        else:
            _remove_preferred_from_avoid(entry)
    family_ids = [
        str(term_id)
        for term_id, entry in entries.items()
        if isinstance(entry, dict) and clean_memory_kind(entry.get("memory_kind")) == "term_family"
    ]
    for family_id in family_ids:
        family = entries.get(family_id)
        if not isinstance(family, dict):
            continue
        _attach_family_to_existing_children(entries, family_id, family)
        _create_missing_family_children(entries, family_id, family, now)
    _remove_sibling_targets_from_avoid(entries)
    memory["updated_at"] = now
    return memory


def sanitize_terms_for_prompt(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for term in terms:
        if not isinstance(term, dict):
            continue
        item = dict(term)
        for text_key in (
            "meaning",
            "full_form",
            "document_local_role",
            "why_it_matters",
            "target_pattern",
            "target_language_risk",
            "evidence",
        ):
            if item.get(text_key):
                item[text_key] = _clean_evidence_text(item.get(text_key))
        item["memory_kind"] = clean_memory_kind(item.get("memory_kind"), "term")
        if is_context_only_kind(item.get("memory_kind")):
            item.pop("preferred_target", None)
            item.pop("suggested_target", None)
            item.pop("active_sense_id", None)
            item["target_candidates"] = []
            item["do_not_translate_as"] = []
            active_sense = item.get("active_sense")
            if isinstance(active_sense, dict):
                active_sense = dict(active_sense)
                for text_key in ("meaning", "target_language_risk", "evidence"):
                    if active_sense.get(text_key):
                        active_sense[text_key] = _clean_evidence_text(active_sense.get(text_key))
                active_sense.pop("preferred_target", None)
                item["active_sense"] = active_sense
        else:
            preferred = normalize_document_source(item.get("preferred_target"))
            if preferred:
                item["do_not_translate_as"] = [
                    value
                    for value in (item.get("do_not_translate_as") or [])
                    if normalize_document_source(value) != preferred
                ]
            if item.get("needs_review") or str(item.get("status") or "").strip().lower() == "review_required":
                candidate_targets = _candidate_targets_for_prompt(item)
                if len(candidate_targets) >= 2:
                    item["candidate_targets"] = candidate_targets
                else:
                    item["candidate_targets"] = []
                item.pop("suggested_target", None)
                item.pop("preferred_target", None)
                item["do_not_translate_as"] = []
                item["avoid_targets"] = []
        sanitized.append(item)
    return _dedupe_terms_for_prompt(sanitized)


def validate_document_term_memory_snapshot(memory: dict[str, Any] | None) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not isinstance(memory, dict) or not isinstance(memory.get("entries"), dict):
        return problems
    for term_id, entry in memory["entries"].items():
        if not isinstance(entry, dict):
            continue
        kind = clean_memory_kind(entry.get("memory_kind"), "term")
        if is_context_only_kind(kind):
            if entry.get("preferred_target"):
                problems.append({"term_id": term_id, "reason": "context_entry_has_preferred_target"})
            for sense in entry.get("senses") or []:
                if isinstance(sense, dict) and sense.get("preferred_target"):
                    problems.append({"term_id": term_id, "reason": "context_entry_sense_has_preferred_target"})
        preferred = normalize_document_source(entry.get("preferred_target"))
        if preferred:
            for value in entry.get("do_not_translate_as") or []:
                if normalize_document_source(value) == preferred:
                    problems.append({"term_id": term_id, "reason": "preferred_target_in_avoid"})
    return problems
