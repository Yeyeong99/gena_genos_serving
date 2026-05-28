"""Promotion, demotion, and resolution logic for document-local term memory."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

from translation_pipeline.common.term_memory_core import (
    _SOFT_LOCK_MAX_TARGET_ENTROPY,
    _SOFT_LOCK_MIN_OBSERVED_SCORE,
    _SOFT_LOCK_MIN_SCORE,
    _SOFT_LOCK_MIN_TARGET_COUNT,
    _SOFT_LOCK_MIN_TARGET_SHARE,
    _entries_are_related,
    _entry_source_acronyms,
    _extract_acronyms,
    _has_ambiguous_acronym_aliases,
    _has_unrelated_acronym,
    _is_bad_target_candidate,
    _is_target_too_short_for_source,
    normalize_source,
)


def _move_to_review(memory: dict[str, Any], term_id: str, entry: dict[str, Any], reason: str) -> None:
    reviewed = {
        **entry,
        "status": "review",
        "review_reason": reason,
        "updated_at": time.time(),
    }
    memory.setdefault("review", {})[term_id] = reviewed
    memory.get("pending", {}).pop(term_id, None)
    memory.get("soft_locked", {}).pop(term_id, None)


def _target_entropy(target_candidates: list[dict[str, Any]]) -> float:
    total = sum(int(item.get("count") or 0) for item in target_candidates if isinstance(item, dict))
    if total <= 0:
        return 0.0
    entropy = 0.0
    for item in target_candidates:
        if not isinstance(item, dict):
            continue
        count = int(item.get("count") or 0)
        if count <= 0:
            continue
        probability = count / total
        entropy -= probability * math.log(probability)
    return entropy


def _promote_if_ready(memory: dict[str, Any], term_id: str, entry: dict[str, Any]) -> None:
    if entry.get("status") != "pending":
        return
    if entry.get("review_reason"):
        _move_to_review(memory, term_id, entry, str(entry.get("review_reason") or "needs_review"))
        return
    if int(entry.get("frequency") or 0) < 2:
        return
    target_candidates = [
        item
        for item in entry.get("target_candidates") or []
        if isinstance(item, dict) and item.get("target")
    ]
    if not target_candidates:
        return
    top = max(target_candidates, key=lambda item: int(item.get("count") or 0))
    target = str(top.get("target") or "").strip()
    count = int(top.get("count") or 0)
    total = sum(int(item.get("count") or 0) for item in target_candidates)
    if count < _SOFT_LOCK_MIN_TARGET_COUNT:
        return
    if total and count / total < _SOFT_LOCK_MIN_TARGET_SHARE:
        _move_to_review(memory, term_id, entry, "target_conflict")
        return
    if len(target_candidates) >= 3 and _target_entropy(target_candidates) > _SOFT_LOCK_MAX_TARGET_ENTROPY:
        _move_to_review(memory, term_id, entry, "target_entropy_high")
        return
    score = float(entry.get("candidate_score") or entry.get("confidence") or 0.0)
    if score < _SOFT_LOCK_MIN_SCORE and score < _SOFT_LOCK_MIN_OBSERVED_SCORE:
        return
    if _is_bad_target_candidate(str(entry.get("source_term") or ""), target):
        _move_to_review(memory, term_id, entry, "bad_target_candidate_shape")
        return
    if _is_target_too_short_for_source(str(entry.get("source_term") or ""), target):
        _move_to_review(memory, term_id, entry, "target_too_short_for_source")
        return
    if _has_unrelated_acronym(entry, target):
        _move_to_review(memory, term_id, entry, "target_contains_unrelated_acronym")
        return
    if _has_ambiguous_acronym_aliases(entry):
        _move_to_review(memory, term_id, entry, "ambiguous_acronym_aliases")
        return
    confidence = max(float(entry.get("confidence") or 0.0), min(0.85, 0.65 + count * 0.08))
    promoted = {
        **entry,
        "status": "soft_locked",
        "target": target,
        "target_term": target,
        "source_type": "observed_translation",
        "confidence": round(confidence, 4),
        "version": int(entry.get("version") or 1),
        "updated_at": time.time(),
    }
    memory.setdefault("soft_locked", {})[term_id] = promoted
    memory.get("pending", {}).pop(term_id, None)


def _demote_invalid_soft_locked(memory: dict[str, Any]) -> None:
    for term_id, entry in list((memory.get("soft_locked") or {}).items()):
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source_term") or "")
        target = str(entry.get("target_term") or entry.get("target") or "")
        if entry.get("review_reason"):
            _move_to_review(memory, str(term_id), entry, str(entry.get("review_reason") or "needs_review"))
        elif _is_bad_target_candidate(source, target):
            _move_to_review(memory, str(term_id), entry, "bad_target_candidate_shape")
        elif _is_target_too_short_for_source(source, target):
            _move_to_review(memory, str(term_id), entry, "target_too_short_for_source")
        elif _has_unrelated_acronym(entry, target):
            _move_to_review(memory, str(term_id), entry, "target_contains_unrelated_acronym")
        elif _has_ambiguous_acronym_aliases(entry):
            _move_to_review(memory, str(term_id), entry, "ambiguous_acronym_aliases")


def _demote_target_collisions(memory: dict[str, Any]) -> None:
    soft_locked = memory.get("soft_locked") or {}
    if not isinstance(soft_locked, dict):
        return
    by_target: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for term_id, entry in soft_locked.items():
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target_term") or entry.get("target") or "").strip()
        if target:
            by_target[normalize_source(target)].append((str(term_id), entry))

    for items in by_target.values():
        if len(items) <= 1:
            continue
        collided: set[str] = set()
        for index, (left_id, left_entry) in enumerate(items):
            for right_id, right_entry in items[index + 1 :]:
                if _entries_are_related(left_entry, right_entry):
                    continue
                collided.add(left_id)
                collided.add(right_id)
        if not collided:
            continue

        target = str(items[0][1].get("target_term") or items[0][1].get("target") or "")
        target_acronyms = _extract_acronyms(target)
        for term_id, entry in list(items):
            if term_id not in collided:
                continue
            if target_acronyms and not (target_acronyms - _entry_source_acronyms(entry)):
                continue
            _move_to_review(memory, term_id, entry, "target_collision")


def resolve_observed_terms(
    memory: dict[str, Any] | None,
    units: Any,
    translated_by_unit_id: dict[int, str],
) -> None:
    """Observe translations and apply current promotion/demotion rules."""

    from translation_pipeline.common.term_observer import record_observed_translations

    record_observed_translations(memory, units, translated_by_unit_id)


__all__ = ["resolve_observed_terms"]
