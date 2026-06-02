"""Validated action applier for Document Term Memory resolver updates."""

from __future__ import annotations

import time
from typing import Any
import re

from translation_pipeline.common.document_term_memory_structure import (
    TARGET_ACTIONS,
    clean_memory_kind,
    is_context_only_kind,
    sanitize_document_term_memory,
    target_action_allowed_for_entry,
    normalize_document_source,
)

ALLOWED_TERM_MEMORY_ACTIONS = {
    "add_sense",
    "update_sense",
    "set_active_sense",
    "add_target_candidate",
    "add_child_term",
    "add_family_evidence",
    "update_family_pattern",
    "update_note",
    "mark_preferred",
    "mark_avoid",
    "deprecate_target",
    "request_repair",
    "no_update",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _clean_target(value: Any) -> str:
    text = _clean_text(value)
    return re.sub(r"\s+\(", "(", text)


def _list_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = _clean_text(item)
        if not text:
            continue
        key = normalize_document_source(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _entries(memory: dict[str, Any]) -> dict[str, Any]:
    entries = memory.setdefault("entries", {})
    return entries if isinstance(entries, dict) else {}


def _next_term_id(memory: dict[str, Any]) -> str:
    entries = _entries(memory)
    index = len(entries) + 1
    while f"dtm_{index:03d}" in entries:
        index += 1
    return f"dtm_{index:03d}"


def _next_sense_id(entry: dict[str, Any]) -> str:
    senses = entry.setdefault("senses", [])
    index = len(senses) + 1 if isinstance(senses, list) else 1
    existing = {
        str(item.get("sense_id") or "")
        for item in senses
        if isinstance(item, dict)
    }
    while f"sense_{index:03d}" in existing:
        index += 1
    return f"sense_{index:03d}"


def _find_exact_entry(
    memory: dict[str, Any],
    source_term: str,
    *,
    prefer_target_kind: bool = False,
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    normalized = normalize_document_source(source_term)
    if not normalized:
        return None, None
    fallback: tuple[str, dict[str, Any]] | tuple[None, None] = (None, None)
    for term_id, entry in _entries(memory).items():
        if not isinstance(entry, dict):
            continue
        if normalize_document_source(entry.get("source_term")) == normalized:
            if prefer_target_kind and is_context_only_kind(entry.get("memory_kind")):
                fallback = (str(term_id), entry)
                continue
            return str(term_id), entry
    return fallback


def _find_target_entry_by_alias(memory: dict[str, Any], source_term: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    normalized = normalize_document_source(source_term)
    if not normalized:
        return None, None
    for term_id, entry in _entries(memory).items():
        if not isinstance(entry, dict) or is_context_only_kind(entry.get("memory_kind")):
            continue
        sources = entry.get("source_terms") or [entry.get("source_term")]
        for source in sources:
            if normalize_document_source(source) == normalized:
                return str(term_id), entry
    return None, None


def _ensure_entry(memory: dict[str, Any], action: dict[str, Any], now: float) -> tuple[str, dict[str, Any]]:
    source_term = _clean_text(action.get("source_term") or action.get("source"))
    if not source_term:
        raise ValueError("source_term is required")
    action_type = _clean_text(action.get("type"))
    term_id, entry = _find_exact_entry(
        memory,
        source_term,
        prefer_target_kind=action_type in TARGET_ACTIONS - {"no_update", "request_repair"},
    )
    if entry is None:
        term_id, entry = _find_target_entry_by_alias(memory, source_term)
    if entry is not None and term_id is not None:
        _ensure_resolution_fields(entry)
        return term_id, entry
    term_id = _next_term_id(memory)
    source_terms = _list_texts(action.get("source_terms")) or [source_term]
    if source_term not in source_terms:
        source_terms.insert(0, source_term)
    entry = {
        "term_id": term_id,
        "source_term": source_term,
        "source_terms": source_terms,
        "normalized_sources": [normalize_document_source(item) for item in source_terms],
        "memory_kind": clean_memory_kind(action.get("memory_kind"), "term"),
        "status": "analysis_hint",
        "senses": [],
        "target_candidates": [],
        "do_not_translate_as": [],
        "avoid_targets": [],
        "term_history": [],
        "repair_requests": [],
        "applied_scope_refs": [],
        "created_at": now,
        "updated_at": now,
        "updated_by": "term_resolver",
    }
    _entries(memory)[term_id] = entry
    return term_id, entry


def _ensure_resolution_fields(entry: dict[str, Any]) -> None:
    entry["memory_kind"] = clean_memory_kind(entry.get("memory_kind"), "term")
    entry.setdefault("senses", [])
    entry.setdefault("target_candidates", [])
    entry.setdefault("do_not_translate_as", [])
    entry.setdefault("avoid_targets", [])
    entry.setdefault("term_history", [])
    entry.setdefault("repair_requests", [])
    entry.setdefault("applied_scope_refs", [])
    if "source_terms" not in entry:
        source = _clean_text(entry.get("source_term"))
        entry["source_terms"] = [source] if source else []
    entry["normalized_sources"] = [
        normalize_document_source(item)
        for item in entry.get("source_terms") or []
        if normalize_document_source(item)
    ]


def _find_sense(entry: dict[str, Any], sense_id: str) -> dict[str, Any] | None:
    for sense in entry.get("senses") or []:
        if isinstance(sense, dict) and str(sense.get("sense_id") or "") == sense_id:
            return sense
    return None


def _upsert_target_candidate(
    entry: dict[str, Any],
    target: str,
    *,
    status: str,
    source: str,
    reason: str,
    evidence_refs: list[str],
    superseded_by: str = "",
    now: float,
) -> dict[str, Any]:
    target = _clean_target(target)
    superseded_by = _clean_target(superseded_by)
    candidates = entry.setdefault("target_candidates", [])
    for item in candidates:
        if isinstance(item, dict) and item.get("target") == target:
            item["status"] = status
            item["source"] = source
            item["reason"] = reason or item.get("reason") or ""
            item["updated_at"] = now
            if evidence_refs:
                refs = item.setdefault("evidence_refs", [])
                for ref in evidence_refs:
                    if ref not in refs:
                        refs.append(ref)
            if superseded_by:
                item["superseded_by"] = superseded_by
            return item
    candidate = {
        "target": target,
        "status": status,
        "count": 0,
        "source": source,
        "reason": reason,
        "evidence_refs": evidence_refs,
        "superseded_by": superseded_by,
        "created_at": now,
        "updated_at": now,
    }
    candidate = {key: value for key, value in candidate.items() if value not in ("", [], None)}
    candidates.append(candidate)
    return candidate


def _history(entry: dict[str, Any], action: dict[str, Any], *, status: str, now: float, detail: str = "") -> None:
    entry.setdefault("term_history", []).append(
        {
            "action": action.get("type"),
            "status": status,
            "detail": detail,
            "payload": action,
            "created_at": now,
            "updated_by": "term_resolver",
        }
    )


def _action_evidence_refs(action: dict[str, Any]) -> list[str]:
    return _list_texts(action.get("evidence_refs"))


def _deprecate_previous_preferred(
    entry: dict[str, Any],
    new_target: str,
    *,
    reason: str,
    evidence_refs: list[str],
    now: float,
) -> None:
    previous_target = _clean_target(entry.get("preferred_target"))
    if not previous_target or normalize_document_source(previous_target) == normalize_document_source(new_target):
        return
    _upsert_target_candidate(
        entry,
        previous_target,
        status="deprecated",
        source="term_resolver",
        reason=reason or f"superseded by resolver preferred target: {new_target}",
        evidence_refs=evidence_refs,
        superseded_by=new_target,
        now=now,
    )
    avoid = entry.setdefault("do_not_translate_as", [])
    if previous_target not in avoid:
        avoid.append(previous_target)


def _target_in_avoid(entry: dict[str, Any], target: str) -> bool:
    normalized = normalize_document_source(target)
    if not normalized:
        return False
    for item in entry.get("do_not_translate_as") or []:
        if normalize_document_source(item) == normalized:
            return True
    for item in entry.get("avoid_targets") or []:
        if isinstance(item, dict) and normalize_document_source(item.get("target")) == normalized:
            return True
    for item in entry.get("target_candidates") or []:
        if (
            isinstance(item, dict)
            and normalize_document_source(item.get("target")) == normalized
            and str(item.get("status") or "") in {"avoid", "deprecated"}
        ):
            return True
    return False


def _validate_target_action(entry: dict[str, Any], action: dict[str, Any], action_type: str, target: str) -> str:
    if not target_action_allowed_for_entry(entry, action_type):
        return "action_not_allowed_for_memory_kind"
    if not target:
        return ""
    preferred = _clean_target(entry.get("preferred_target"))
    if action_type in {"mark_preferred", "add_sense", "update_sense"} and _target_in_avoid(entry, target):
        return "target_is_already_avoid_or_deprecated"
    if action_type == "mark_avoid" and preferred and normalize_document_source(preferred) == normalize_document_source(target):
        return "cannot_avoid_current_preferred_target"
    if action_type == "deprecate_target" and preferred and normalize_document_source(preferred) == normalize_document_source(target):
        superseded_by = _clean_target(action.get("superseded_by"))
        if not superseded_by or normalize_document_source(superseded_by) == normalize_document_source(target):
            return "cannot_deprecate_current_preferred_without_superseded_by"
    return ""


def _apply_add_sense(entry: dict[str, Any], action: dict[str, Any], now: float) -> str:
    sense_id = _clean_text(action.get("sense_id")) or _next_sense_id(entry)
    if _find_sense(entry, sense_id):
        sense_id = _next_sense_id(entry)
    preferred_target = _clean_target(action.get("preferred_target"))
    status = _clean_text(action.get("status")) or ("analysis_candidate" if preferred_target else "analysis_hint")
    sense = {
        "sense_id": sense_id,
        "meaning": _clean_text(action.get("meaning") or action.get("document_local_meaning")),
        "status": status,
        "preferred_target": preferred_target or None,
        "confidence": action.get("confidence"),
        "evidence": _clean_text(action.get("evidence")),
        "evidence_refs": _action_evidence_refs(action),
        "target_language_risk": _clean_text(action.get("target_language_risk")),
        "resolver_priority": _clean_text(action.get("resolver_priority")),
        "source": "term_resolver",
        "created_at": now,
        "updated_at": now,
    }
    sense = {key: value for key, value in sense.items() if value not in ("", [], None)}
    entry.setdefault("senses", []).append(sense)
    if action.get("set_active") or not entry.get("active_sense_id"):
        entry["active_sense_id"] = sense_id
    if preferred_target:
        _deprecate_previous_preferred(
            entry,
            preferred_target,
            reason=_clean_text(action.get("reason")),
            evidence_refs=_action_evidence_refs(action),
            now=now,
        )
        _upsert_target_candidate(
            entry,
            preferred_target,
            status="preferred",
            source="term_resolver",
            reason=_clean_text(action.get("reason")),
            evidence_refs=_action_evidence_refs(action),
            now=now,
        )
        entry["preferred_target"] = preferred_target
        entry["status"] = status
    return sense_id


def _apply_update_sense(entry: dict[str, Any], action: dict[str, Any], now: float) -> str:
    sense_id = _clean_text(action.get("sense_id") or entry.get("active_sense_id"))
    sense = _find_sense(entry, sense_id) if sense_id else None
    if sense is None:
        return _apply_add_sense(entry, {**action, "set_active": bool(action.get("set_active"))}, now)
    for key, source_key in (
        ("meaning", "meaning"),
        ("preferred_target", "preferred_target"),
        ("status", "status"),
        ("target_language_risk", "target_language_risk"),
        ("resolver_priority", "resolver_priority"),
        ("evidence", "evidence"),
    ):
        value = _clean_target(action.get(source_key)) if source_key == "preferred_target" else _clean_text(action.get(source_key))
        if value:
            sense[key] = value
    if action.get("confidence") is not None:
        sense["confidence"] = action.get("confidence")
    refs = sense.setdefault("evidence_refs", [])
    for ref in _action_evidence_refs(action):
        if ref not in refs:
            refs.append(ref)
    sense["updated_at"] = now
    if action.get("set_active"):
        entry["active_sense_id"] = sense_id
    if sense.get("preferred_target"):
        _deprecate_previous_preferred(
            entry,
            str(sense["preferred_target"]),
            reason=_clean_text(action.get("reason")),
            evidence_refs=_action_evidence_refs(action),
            now=now,
        )
        entry["preferred_target"] = sense["preferred_target"]
        _upsert_target_candidate(
            entry,
            str(sense["preferred_target"]),
            status="preferred",
            source="term_resolver",
            reason=_clean_text(action.get("reason")),
            evidence_refs=_action_evidence_refs(action),
            now=now,
        )
    return sense_id


def _apply_context_update(entry: dict[str, Any], action: dict[str, Any], now: float) -> str:
    action_type = _clean_text(action.get("type"))
    for key, source_key in (
        ("meaning", "meaning"),
        ("why_it_matters", "why_it_matters"),
        ("target_pattern", "target_pattern"),
        ("evidence", "evidence"),
        ("target_language_risk", "target_language_risk"),
        ("resolver_priority", "resolver_priority"),
    ):
        value = _clean_text(action.get(source_key))
        if value:
            entry[key] = value
    refs = entry.setdefault("evidence_refs", [])
    for ref in _action_evidence_refs(action):
        if ref not in refs:
            refs.append(ref)
    if action_type == "add_child_term":
        child = _clean_text(action.get("child_source_term") or action.get("child") or action.get("source_child"))
        if child:
            children = entry.setdefault("children", [])
            if child not in children:
                children.append(child)
            source_terms = entry.setdefault("source_terms", [])
            if child not in source_terms:
                source_terms.append(child)
            entry["normalized_sources"] = [
                normalize_document_source(item)
                for item in source_terms
                if normalize_document_source(item)
            ]
            return child
    return action_type


def apply_term_memory_action(
    memory: dict[str, Any],
    action: dict[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply one validated resolver action to Document Term Memory."""

    now = time.time() if now is None else now
    if not isinstance(action, dict):
        return {"applied": False, "reason": "action_not_object"}
    action_type = _clean_text(action.get("type"))
    if action_type not in ALLOWED_TERM_MEMORY_ACTIONS:
        return {"applied": False, "reason": "unsupported_action", "type": action_type}
    if action_type == "no_update":
        memory.setdefault("resolver_events", []).append(
            {
                "type": "no_update",
                "reason": _clean_text(action.get("reason")),
                "created_at": now,
            }
        )
        memory["updated_at"] = now
        return {"applied": True, "type": action_type}

    try:
        _term_id, entry = _ensure_entry(memory, action, now)
    except ValueError as exc:
        return {"applied": False, "reason": str(exc), "type": action_type}
    if not target_action_allowed_for_entry(entry, action_type):
        return {
            "applied": False,
            "reason": "action_not_allowed_for_memory_kind",
            "type": action_type,
            "source_term": entry.get("source_term"),
            "memory_kind": entry.get("memory_kind"),
        }
    target_for_validation = _clean_target(action.get("target") or action.get("preferred_target"))
    validation_error = _validate_target_action(entry, action, action_type, target_for_validation)
    if validation_error:
        return {
            "applied": False,
            "reason": validation_error,
            "type": action_type,
            "source_term": entry.get("source_term"),
            "target": target_for_validation,
        }
    detail = ""
    evidence_refs = _action_evidence_refs(action)
    reason = _clean_text(action.get("reason"))

    if action_type in {"add_family_evidence", "update_family_pattern", "update_note", "add_child_term"}:
        detail = _apply_context_update(entry, action, now)
    elif action_type == "add_sense":
        detail = _apply_add_sense(entry, action, now)
    elif action_type == "update_sense":
        detail = _apply_update_sense(entry, action, now)
    elif action_type == "set_active_sense":
        sense_id = _clean_text(action.get("sense_id"))
        if not sense_id or _find_sense(entry, sense_id) is None:
            return {"applied": False, "reason": "sense_not_found", "type": action_type}
        entry["active_sense_id"] = sense_id
        detail = sense_id
    elif action_type == "add_target_candidate":
        target = _clean_target(action.get("target") or action.get("preferred_target"))
        if not target:
            return {"applied": False, "reason": "target_required", "type": action_type}
        _upsert_target_candidate(
            entry,
            target,
            status=_clean_text(action.get("status")) or "candidate",
            source="term_resolver",
            reason=reason,
            evidence_refs=evidence_refs,
            now=now,
        )
        detail = target
    elif action_type == "mark_preferred":
        target = _clean_target(action.get("target") or action.get("preferred_target"))
        if not target:
            return {"applied": False, "reason": "target_required", "type": action_type}
        _deprecate_previous_preferred(
            entry,
            target,
            reason=reason,
            evidence_refs=evidence_refs,
            now=now,
        )
        entry["preferred_target"] = target
        entry["status"] = _clean_text(action.get("status")) or entry.get("status") or "analysis_candidate"
        active_sense = _find_sense(entry, str(entry.get("active_sense_id") or ""))
        if active_sense is not None:
            active_sense["preferred_target"] = target
            active_sense["updated_at"] = now
        _upsert_target_candidate(
            entry,
            target,
            status="preferred",
            source="term_resolver",
            reason=reason,
            evidence_refs=evidence_refs,
            now=now,
        )
        detail = target
    elif action_type == "mark_avoid":
        target = _clean_target(action.get("target"))
        if not target:
            return {"applied": False, "reason": "target_required", "type": action_type}
        avoid = entry.setdefault("do_not_translate_as", [])
        if target not in avoid:
            avoid.append(target)
        _upsert_target_candidate(
            entry,
            target,
            status="avoid",
            source="term_resolver",
            reason=reason,
            evidence_refs=evidence_refs,
            now=now,
        )
        entry.setdefault("avoid_targets", []).append(
            {
                "target": target,
                "status": "avoid",
                "reason": reason,
                "evidence_refs": evidence_refs,
                "created_at": now,
                "updated_at": now,
            }
        )
        detail = target
    elif action_type == "deprecate_target":
        target = _clean_target(action.get("target"))
        if not target:
            return {"applied": False, "reason": "target_required", "type": action_type}
        superseded_by = _clean_target(action.get("superseded_by"))
        _upsert_target_candidate(
            entry,
            target,
            status="deprecated",
            source="term_resolver",
            reason=reason,
            evidence_refs=evidence_refs,
            superseded_by=superseded_by,
            now=now,
        )
        avoid = entry.setdefault("do_not_translate_as", [])
        if target not in avoid:
            avoid.append(target)
        detail = target
    elif action_type == "request_repair":
        entry.setdefault("repair_requests", []).append(
            {
                "reason": reason,
                "target": _clean_text(action.get("target")),
                "replacement": _clean_text(action.get("replacement") or action.get("superseded_by")),
                "scope_refs": _list_texts(action.get("scope_refs")),
                "evidence_refs": evidence_refs,
                "status": "pending",
                "created_at": now,
            }
        )
        detail = reason

    entry["updated_at"] = now
    entry["updated_by"] = "term_resolver"
    memory["updated_at"] = now
    _history(entry, action, status="applied", detail=detail, now=now)
    sanitize_document_term_memory(memory)
    return {"applied": True, "type": action_type, "source_term": entry.get("source_term"), "detail": detail}


def apply_term_memory_actions(
    memory: dict[str, Any] | None,
    actions: list[dict[str, Any]] | dict[str, Any] | None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply resolver actions and return an audit summary."""

    if not isinstance(memory, dict):
        return {"applied": 0, "skipped": 0, "results": [], "reason": "memory_not_available"}
    if isinstance(actions, dict):
        action_list = actions.get("actions")
    else:
        action_list = actions
    if not isinstance(action_list, list):
        return {"applied": 0, "skipped": 0, "results": [], "reason": "actions_not_list"}
    ts = time.time() if now is None else now
    results = [apply_term_memory_action(memory, action, now=ts) for action in action_list]
    sanitize_document_term_memory(memory)
    return {
        "applied": sum(1 for result in results if result.get("applied")),
        "skipped": sum(1 for result in results if not result.get("applied")),
        "results": results,
    }


__all__ = [
    "ALLOWED_TERM_MEMORY_ACTIONS",
    "apply_term_memory_action",
    "apply_term_memory_actions",
]
