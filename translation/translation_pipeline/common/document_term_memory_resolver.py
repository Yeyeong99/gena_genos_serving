"""LLM-backed resolver for Document Term Memory update actions."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from translation_pipeline.common.document_term_memory import find_relevant_document_terms, normalize_document_source
from translation_pipeline.common.document_term_memory_actions import apply_term_memory_actions
from translation_pipeline.common.job_artifacts import job_artifact_path, safe_artifact_part
from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.prompts import render_prompt
from translation_pipeline.common.term_observer import extract_target_candidate
from translation_pipeline.common.term_memory_core import _chunk_id, _clean_evidence_text

_DEFAULT_PROMPT_SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory_resolver_prompts"
_TARGET_RELATIONS = {
    "same_meaning_variant",
    "acceptable_variant",
    "different_sense",
    "sibling_term",
    "invalid_candidate",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _clean_target(value: Any) -> str:
    return re.sub(r"\s+\(", "(", _clean_text(value))


def _parse_resolver_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _unit_payload(unit: Any, translated: str) -> dict[str, Any]:
    return {
        "translation_unit_id": getattr(unit, "translation_unit_id", None),
        "chunk_id": _chunk_id(unit),
        "source_text": str(getattr(unit, "text", "") or ""),
        "translated_text": translated,
        "context_scope": str(getattr(unit, "context_scope", "") or ""),
        "context_text": str(getattr(unit, "context_text", "") or "")[:1200],
        "element_type": str(getattr(unit, "element_type", "") or ""),
    }


def _observed_translation_payload(
    units: Any,
    translated_by_unit_id: dict[int, str],
    *,
    max_items: int = 40,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for unit in list(units or []):
        unit_id = getattr(unit, "translation_unit_id", None)
        translated = str(translated_by_unit_id.get(int(unit_id), "") or "") if unit_id is not None else ""
        if not translated:
            continue
        payload.append(_unit_payload(unit, translated))
        if len(payload) >= max_items:
            break
    return payload


def _source_terms_from_entry(entry: dict[str, Any]) -> list[str]:
    values = [entry.get("source"), entry.get("source_term"), entry.get("term"), entry.get("family_name")]
    values.extend(entry.get("source_terms") or [])
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = normalize_document_source(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _term_appears_in_source(term: str, source_text: str) -> bool:
    term_norm = normalize_document_source(term)
    source_norm = normalize_document_source(source_text)
    if not term_norm or not source_norm:
        return False
    return term_norm in source_norm


def _entry_appears_in_source(entry: dict[str, Any], source_text: str) -> bool:
    return any(_term_appears_in_source(term, source_text) for term in _source_terms_from_entry(entry))


def _source_terms_in_observed(memory: dict[str, Any] | None, observed: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(memory, dict):
        return [], []
    entries = memory.get("entries") if isinstance(memory.get("entries"), dict) else {}
    source_text = "\n".join(str(item.get("source_text") or "") for item in observed)
    matched: list[dict[str, Any]] = []
    matched_terms: list[str] = []
    seen_entries: set[int] = set()
    seen_terms: set[str] = set()
    for entry in entries.values():
        if not isinstance(entry, dict) or not _entry_appears_in_source(entry, source_text):
            continue
        identity = id(entry)
        if identity not in seen_entries:
            matched.append(entry)
            seen_entries.add(identity)
        for term in _source_terms_from_entry(entry):
            if _term_appears_in_source(term, source_text):
                key = normalize_document_source(term)
                if key not in seen_terms:
                    matched_terms.append(term)
                    seen_terms.add(key)
    return matched, matched_terms


def _target_appears_in_translation(target: str, translated_text: str) -> bool:
    target_key = normalize_document_source(target)
    translated_key = normalize_document_source(translated_text)
    return bool(target_key and translated_key and target_key in translated_key)


def _candidate_target_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        target = item.get("target") if isinstance(item, dict) else item
        target = _clean_target(target)
        key = normalize_document_source(target)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(target)
    return result


def _matched_candidate_pool_target(candidate_targets: Any, translated_text: str) -> str:
    for target in _candidate_target_texts(candidate_targets):
        if _target_appears_in_translation(target, translated_text):
            return target
    return ""


def _prompt_injections_for_unit(memory: dict[str, Any] | None, unit_id: Any) -> list[dict[str, Any]]:
    if not isinstance(memory, dict):
        return []
    by_unit = memory.get("_prompt_injections_by_unit_id")
    if not isinstance(by_unit, dict):
        return []
    injections = by_unit.get(str(unit_id)) or by_unit.get(unit_id) or []
    return [item for item in injections if isinstance(item, dict)] if isinstance(injections, list) else []


def _classify_observation_against_injections(memory: dict[str, Any] | None, observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify whether an observed target echoed or diverged from injected DTM."""

    observations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in observed:
        unit_id = item.get("translation_unit_id")
        source_text = str(item.get("source_text") or "")
        translated_text = str(item.get("translated_text") or "")
        for injection in _prompt_injections_for_unit(memory, unit_id):
            source_term = str(injection.get("source_term") or "").strip()
            if not source_term or not _term_appears_in_source(source_term, source_text):
                continue
            injected_target = _clean_target(injection.get("injected_target"))
            candidate_pool_target = _matched_candidate_pool_target(injection.get("candidate_targets"), translated_text)
            observed_target = extract_target_candidate(source_text, translated_text, source_term) or ""
            if injected_target and _target_appears_in_translation(injected_target, translated_text):
                observation_type = "echoed_injected_target"
            elif candidate_pool_target:
                observation_type = "echoed_candidate_pool_target"
                observed_target = candidate_pool_target
            elif injected_target and observed_target:
                observation_type = "diverged_from_injected_target"
            elif injected_target:
                observation_type = "injected_target_not_observed"
            elif observed_target:
                observation_type = "independent_observed_target"
            else:
                observation_type = "no_target_candidate"
            marker = (
                normalize_document_source(source_term),
                normalize_document_source(injected_target),
                normalize_document_source(observed_target),
                str(unit_id),
            )
            if marker in seen:
                continue
            seen.add(marker)
            observations.append(
                {
                    "source_term": source_term,
                    "injected_target": injected_target or None,
                    "candidate_targets": injection.get("candidate_targets") or [],
                    "observed_target": observed_target or None,
                    "observation_type": observation_type,
                    "injection_strength": injection.get("injection_strength"),
                    "status": injection.get("status"),
                    "needs_review": bool(injection.get("needs_review")),
                    "target_decision_needed": bool(injection.get("target_decision_needed")),
                    "translation_unit_id": unit_id,
                    "context_scope": item.get("context_scope"),
                    "source_text": source_text[:300],
                    "translated_text": translated_text[:300],
                }
            )
    return observations


def _prompt_influence_risks(injected_observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_term": item.get("source_term"),
            "target": item.get("injected_target") or item.get("observed_target"),
            "translation_unit_id": item.get("translation_unit_id"),
            "context_scope": item.get("context_scope"),
            "reason": "translated target matches a target injected from Document Term Memory before translation",
        }
        for item in injected_observations
        if item.get("observation_type") in {"echoed_injected_target", "echoed_candidate_pool_target"}
        and (item.get("injected_target") or item.get("observed_target"))
    ]


def _temporary_glossary_evidence(
    evidence_memory: dict[str, Any] | None,
    *,
    source_terms: list[str] | None = None,
    max_terms: int = 40,
    max_occurrences: int = 3,
) -> list[dict[str, Any]]:
    if not isinstance(evidence_memory, dict):
        return []
    source_keys = {normalize_document_source(item) for item in (source_terms or []) if normalize_document_source(item)}
    entries: list[dict[str, Any]] = []
    for bucket in ("pending", "review", "soft_locked", "locked"):
        bucket_entries = evidence_memory.get(bucket) or {}
        if not isinstance(bucket_entries, dict):
            continue
        for term_id, entry in bucket_entries.items():
            if not isinstance(entry, dict):
                continue
            aliases = [entry.get("source_term"), *(entry.get("aliases") or [])]
            if source_keys and not any(normalize_document_source(item) in source_keys for item in aliases):
                continue
            occurrences = []
            for occurrence in entry.get("occurrences") or []:
                if not isinstance(occurrence, dict):
                    continue
                occurrences.append(
                    {
                        "chunk_id": occurrence.get("chunk_id"),
                        "source_snippet": _clean_evidence_text(occurrence.get("source_snippet")),
                        "surrounding_source": _clean_evidence_text(
                            occurrence.get("source_snippet")
                            or occurrence.get("surrounding_source")
                        ),
                        "translated_snippet": occurrence.get("translated_snippet"),
                        "target_candidate": occurrence.get("target_candidate"),
                    }
                )
                if len(occurrences) >= max_occurrences:
                    break
            entries.append(
                {
                    "term_id": term_id,
                    "bucket": bucket,
                    "source_term": entry.get("source_term"),
                    "aliases": entry.get("aliases") or [],
                    "frequency": entry.get("frequency"),
                    "target_candidates": entry.get("target_candidates") or [],
                    "occurrences": occurrences,
                }
            )
            if len(entries) >= max_terms:
                return entries
    return entries


def build_document_term_resolver_input(
    memory: dict[str, Any] | None,
    *,
    evidence_memory: dict[str, Any] | None = None,
    units: Any = None,
    translated_by_unit_id: dict[int, str] | None = None,
    pre_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build compact resolver input from DTM, evidence, and observed translations."""

    observed = _observed_translation_payload(units or [], translated_by_unit_id or {})
    matched_entries, matched_source_terms = _source_terms_in_observed(memory, observed)
    lookup_texts = []
    for item in observed:
        lookup_texts.append(str(item.get("source_text") or ""))
        lookup_texts.append(str(item.get("context_text") or ""))
    primary_texts = [str(item.get("source_text") or "") for item in observed]
    relevant_terms = find_relevant_document_terms(memory, lookup_texts, primary_texts=primary_texts) if lookup_texts else []
    if matched_source_terms:
        matched_keys = {normalize_document_source(item) for item in matched_source_terms}
        relevant_terms = [
            term
            for term in relevant_terms
            if any(normalize_document_source(source) in matched_keys for source in (term.get("source_terms") or [term.get("source")]))
        ]
    injected_observations = _classify_observation_against_injections(memory, observed)
    prompt_influence_risks = _prompt_influence_risks(injected_observations)
    return {
        "document_profile": (pre_analysis or {}).get("document_profile") or (memory or {}).get("document_profile") or {},
        "domain_context": (pre_analysis or {}).get("domain_context") or (memory or {}).get("domain_context") or [],
        "document_term_memory_summary": {
            "schema_version": (memory or {}).get("schema_version"),
            "job_id": (memory or {}).get("job_id"),
            "target_lang": (memory or {}).get("target_lang"),
            "updated_at": (memory or {}).get("updated_at"),
        },
        "relevant_document_terms": relevant_terms,
        "source_terms_in_scope": matched_source_terms,
        "high_risk_terms_in_scope": _risk_marked_terms_in_scope(matched_entries, matched_source_terms),
        "injected_term_observations": injected_observations,
        "prompt_influence_risks": prompt_influence_risks,
        "temporary_glossary_evidence": _temporary_glossary_evidence(evidence_memory, source_terms=matched_source_terms, max_terms=16),
        "observed_translations": observed,
    }


def _entry_is_risk_marked(entry: dict[str, Any]) -> bool:
    """Return whether pre-analysis/DTM data marks this entry as risky.

    Do not infer risk from document-specific source term names here. Source-term
    policy belongs in pre-analysis / DTM, not in Python constants.
    """

    priority = str(entry.get("resolver_priority") or "").strip().lower()
    if priority in {"high", "critical"}:
        return True
    if _clean_text(entry.get("target_language_risk")):
        return True
    for sense in entry.get("senses") or []:
        if not isinstance(sense, dict):
            continue
        sense_priority = str(sense.get("resolver_priority") or "").strip().lower()
        if sense_priority in {"high", "critical"}:
            return True
        if _clean_text(sense.get("target_language_risk")):
            return True
    return False


def _risk_marked_terms_in_scope(entries: list[dict[str, Any]], matched_source_terms: list[str]) -> list[str]:
    matched_keys = {normalize_document_source(term) for term in matched_source_terms}
    result: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not _entry_is_risk_marked(entry):
            continue
        for term in _source_terms_from_entry(entry):
            key = normalize_document_source(term)
            if key and key in matched_keys and key not in seen:
                result.append(term)
                seen.add(key)
    return result


def _candidate_targets_by_source(relevant_terms: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for entry in relevant_terms:
        if not isinstance(entry, dict):
            continue
        source_keys = {
            normalize_document_source(source)
            for source in (entry.get("source_terms") or [entry.get("source")])
            if normalize_document_source(source)
        }
        target_keys: set[str] = set()
        for target in _candidate_target_texts(entry.get("candidate_targets")):
            key = normalize_document_source(target)
            if key:
                target_keys.add(key)
        preferred = normalize_document_source(entry.get("preferred_target"))
        if preferred:
            target_keys.add(preferred)
        active_sense = entry.get("active_sense")
        if isinstance(active_sense, dict):
            active_preferred = normalize_document_source(active_sense.get("preferred_target"))
            if active_preferred:
                target_keys.add(active_preferred)
        for source_key in source_keys:
            result.setdefault(source_key, set()).update(target_keys)
    return result


def _source_key_for_observation(observation: dict[str, Any]) -> str:
    return normalize_document_source(observation.get("source_term"))


def _observed_target_key(observation: dict[str, Any]) -> str:
    return normalize_document_source(observation.get("observed_target"))


def _has_actionable_resolver_signal(resolver_input: dict[str, Any]) -> bool:
    observations = [
        item
        for item in (resolver_input.get("injected_term_observations") or [])
        if isinstance(item, dict)
    ]
    if not observations:
        return bool(resolver_input.get("high_risk_terms_in_scope"))

    known_targets = _candidate_targets_by_source(resolver_input.get("relevant_document_terms") or [])
    for observation in observations:
        observation_type = str(observation.get("observation_type") or "")
        if observation_type == "diverged_from_injected_target":
            return True
        if observation_type == "independent_observed_target":
            source_key = _source_key_for_observation(observation)
            target_key = _observed_target_key(observation)
            if target_key and target_key not in known_targets.get(source_key, set()):
                return True
        if observation_type == "injected_target_not_observed":
            strength = str(observation.get("injection_strength") or "")
            if strength in {"preferred", "suggested"}:
                return True
    return False


def _resolver_gate(memory: dict[str, Any] | None, resolver_input: dict[str, Any]) -> tuple[bool, str]:
    observed = resolver_input.get("observed_translations") or []
    if not observed:
        return False, "no_observed_translations"
    source_terms = resolver_input.get("source_terms_in_scope") or []
    if not source_terms:
        return False, "no_source_term_in_scope"
    entries = memory.get("entries") if isinstance(memory, dict) and isinstance(memory.get("entries"), dict) else {}
    source_keys = {normalize_document_source(item) for item in source_terms}
    matched = [
        entry
        for entry in entries.values()
        if isinstance(entry, dict)
        and any(normalize_document_source(term) in source_keys for term in _source_terms_from_entry(entry))
    ]
    if not matched:
        return False, "no_matching_memory_entry"
    if not _has_actionable_resolver_signal(resolver_input):
        return False, "covered_by_pre_judge_or_candidate_pool"
    return True, "llm_should_decide"


def _action_source_terms(action: dict[str, Any]) -> list[str]:
    terms = [_clean_text(action.get("source_term") or action.get("source"))]
    terms.extend(_clean_text(item) for item in (action.get("source_terms") or []) if _clean_text(item))
    return [term for term in terms if term]


def _target_for_action(action: dict[str, Any]) -> str:
    return _clean_target(action.get("target") or action.get("preferred_target"))


def _entry_target_policy_error(entry: dict[str, Any], action: dict[str, Any]) -> str:
    target = _target_for_action(action)
    if not target:
        return ""
    target_key = normalize_document_source(target)
    if not target_key:
        return ""
    for value in entry.get("do_not_translate_as") or []:
        if normalize_document_source(value) == target_key:
            return "target_conflicts_with_document_term_memory_avoid"
    for item in entry.get("avoid_targets") or []:
        if isinstance(item, dict) and normalize_document_source(item.get("target")) == target_key:
            return "target_conflicts_with_document_term_memory_avoid"
    for item in entry.get("target_candidates") or []:
        if not isinstance(item, dict):
            continue
        if normalize_document_source(item.get("target")) != target_key:
            continue
        if str(item.get("status") or "").strip().lower() in {"avoid", "deprecated"}:
            return "target_conflicts_with_document_term_memory_avoid"
    return ""


def _action_is_confirming_target(action: dict[str, Any]) -> bool:
    action_type = _clean_text(action.get("type"))
    if action_type == "mark_preferred":
        return True
    if action_type in {"add_sense", "update_sense"}:
        status = str(action.get("status") or "").strip().lower()
        if status in {"confirmed", "preferred", "active", "soft_locked", "locked"}:
            return True
        if bool(action.get("set_active")):
            return True
    if action_type == "set_active_sense":
        return True
    return False


def _prompt_influence_policy_error(action: dict[str, Any], resolver_input: dict[str, Any]) -> str:
    if not _action_is_confirming_target(action):
        return ""
    action_target = normalize_document_source(_target_for_action(action))
    if not action_target:
        return ""
    action_sources = {normalize_document_source(term) for term in _action_source_terms(action)}
    for risk in resolver_input.get("prompt_influence_risks") or []:
        if not isinstance(risk, dict):
            continue
        if normalize_document_source(risk.get("target")) != action_target:
            continue
        risk_source = normalize_document_source(risk.get("source_term"))
        if risk_source and risk_source in action_sources:
            return "prompt_influenced_review_target_cannot_be_confirmed"
    return ""


def _sanitize_resolver_actions(proposed: dict[str, Any], resolver_input: dict[str, Any]) -> dict[str, Any]:
    actions = proposed.get("actions")
    if not isinstance(actions, list):
        return proposed
    allowed_source_keys = {
        normalize_document_source(item)
        for item in (resolver_input.get("source_terms_in_scope") or [])
        if normalize_document_source(item)
    }
    relevant_entries = resolver_input.get("relevant_document_terms") or []
    sanitized: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            rejected.append({"reason": "action_not_object", "action": action})
            continue
        action_type = _clean_text(action.get("type"))
        if action_type == "no_update":
            sanitized.append(action)
            continue
        if action_type == "deprecate_target":
            rejected.append({"reason": "deprecate_target_disabled_for_candidate_pool", "action": action})
            continue
        if action_type == "mark_preferred":
            rejected.append({"reason": "mark_preferred_disabled_preferred_finalized_by_pre_judge", "action": action})
            continue
        source_terms = _action_source_terms(action)
        action_source_keys = {normalize_document_source(term) for term in source_terms if normalize_document_source(term)}
        if not action_source_keys or not (action_source_keys & allowed_source_keys):
            rejected.append({"reason": "source_term_not_in_current_scope", "action": action})
            continue
        normalized_action = dict(action)
        target_relation = _clean_text(normalized_action.get("target_relation") or normalized_action.get("relation"))
        if target_relation:
            if target_relation not in _TARGET_RELATIONS:
                rejected.append({"reason": "unsupported_target_relation", "action": normalized_action})
                continue
            normalized_action["target_relation"] = target_relation
            normalized_action.pop("relation", None)
        if normalized_action.get("target") is not None:
            normalized_action["target"] = _clean_target(normalized_action.get("target"))
        if normalized_action.get("preferred_target") is not None:
            normalized_action["preferred_target"] = _clean_target(normalized_action.get("preferred_target"))
        prompt_influence_error = _prompt_influence_policy_error(normalized_action, resolver_input)
        if prompt_influence_error:
            rejected.append({"reason": prompt_influence_error, "action": normalized_action})
            continue
        action_policy_errors = [
            _entry_target_policy_error(entry, normalized_action)
            for entry in relevant_entries
            if isinstance(entry, dict)
            and any(
                normalize_document_source(term) in action_source_keys
                for term in _source_terms_from_entry(entry)
            )
        ]
        action_policy_errors = [error for error in action_policy_errors if error]
        if action_policy_errors:
            rejected.append({"reason": action_policy_errors[0], "action": normalized_action})
            continue
        sanitized.append(normalized_action)
    result = {**proposed, "actions": sanitized}
    if rejected:
        result["rejected_actions"] = rejected
    return result


def resolver_prompt_snapshot_dir() -> Path:
    value = os.getenv("AI_TRANSLATION_DOCUMENT_TERM_RESOLVER_PROMPT_DIR", "").strip()
    return Path(value) if value else _DEFAULT_PROMPT_SNAPSHOT_DIR


def _safe_filename_part(value: Any) -> str:
    safe = re.sub(r"[^0-9A-Za-z가-힣_.() -]+", "_", str(value or "").strip())
    safe = re.sub(r"\s+", "_", safe).strip("._- ")
    return safe[:120]


def _save_resolver_prompt_snapshot(memory: dict[str, Any] | None, resolver_input: dict[str, Any], prompt: str) -> str:
    if os.getenv("AI_TRANSLATION_RESOLVER_PROMPT_SNAPSHOT_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return ""
    job_id = _safe_filename_part((memory or {}).get("job_id")) or f"resolver-{uuid.uuid4().hex[:12]}"
    artifact = _safe_filename_part((memory or {}).get("_artifact_label"))
    scope = _safe_filename_part(((resolver_input.get("observed_translations") or [{}])[0] or {}).get("context_scope"))
    stamp = int(time.time() * 1000)
    prefix = "__".join(item for item in (scope, str(stamp)) if item) or f"resolver_{stamp}"
    path = job_artifact_path(
        job_id,
        artifact,
        f"{safe_artifact_part(prefix, limit=180)}.json",
        subdir="document_term_resolver_prompts",
    )
    payload = {
        "job_id": (memory or {}).get("job_id"),
        "artifact_label": (memory or {}).get("_artifact_label"),
        "scope": ((resolver_input.get("observed_translations") or [{}])[0] or {}).get("context_scope"),
        "source_terms_in_scope": resolver_input.get("source_terms_in_scope") or [],
        "high_risk_terms_in_scope": resolver_input.get("high_risk_terms_in_scope") or [],
        "prompt": prompt,
        "resolver_input": resolver_input,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


async def propose_document_term_memory_actions(
    sem: Any,
    session: aiohttp.ClientSession | None,
    memory: dict[str, Any] | None,
    *,
    target_lang: str,
    evidence_memory: dict[str, Any] | None = None,
    units: Any = None,
    translated_by_unit_id: dict[int, str] | None = None,
    pre_analysis: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ask the LLM resolver to return DTM update actions."""

    if not isinstance(memory, dict) or session is None or sem is None:
        return None
    resolver_input = build_document_term_resolver_input(
        memory,
        evidence_memory=evidence_memory,
        units=units,
        translated_by_unit_id=translated_by_unit_id,
        pre_analysis=pre_analysis,
    )
    should_call, gate_reason = _resolver_gate(memory, resolver_input)
    if not should_call:
        scope = ((resolver_input.get("observed_translations") or [{}])[0] or {}).get("context_scope")
        log_info(f"[Document Term Resolver] skipped scope={scope} reason={gate_reason}")
        return None
    prompt = render_prompt(
        "document_term_resolver.jinja",
        target_lang=target_lang,
        resolver_input_json=json.dumps(resolver_input, ensure_ascii=False, indent=2),
    )
    prompt_snapshot_path = _save_resolver_prompt_snapshot(memory, resolver_input, prompt)
    started_at = time.perf_counter()
    try:
        raw = await llm_call_async(sem, session, "", prompt)
    except Exception as exc:
        log_info(f"[Document Term Resolver] LLM call failed: {exc}")
        return None
    log_info(
        "[Document Term Resolver] LLM call done "
        f"{time.perf_counter() - started_at:.2f}s prompt_chars={len(prompt)}"
        f"{f' prompt_snapshot={prompt_snapshot_path}' if prompt_snapshot_path else ''}"
    )
    parsed = _parse_resolver_json(raw)
    if not parsed:
        log_info("[Document Term Resolver] returned non-JSON actions")
        return None
    actions = parsed.get("actions")
    if not isinstance(actions, list):
        log_info("[Document Term Resolver] skipped: actions is not a list")
        return None
    sanitized = _sanitize_resolver_actions(parsed, resolver_input)
    if sanitized.get("rejected_actions"):
        log_info(
            "[Document Term Resolver] rejected actions "
            f"count={len(sanitized.get('rejected_actions') or [])}"
        )
    return sanitized


async def resolve_document_term_memory_actions(
    sem: Any,
    session: aiohttp.ClientSession | None,
    memory: dict[str, Any] | None,
    *,
    target_lang: str,
    evidence_memory: dict[str, Any] | None = None,
    units: Any = None,
    translated_by_unit_id: dict[int, str] | None = None,
    pre_analysis: dict[str, Any] | None = None,
    apply: bool = True,
) -> dict[str, Any] | None:
    """Propose resolver actions and optionally apply them to memory."""

    proposed = await propose_document_term_memory_actions(
        sem,
        session,
        memory,
        target_lang=target_lang,
        evidence_memory=evidence_memory,
        units=units,
        translated_by_unit_id=translated_by_unit_id,
        pre_analysis=pre_analysis,
    )
    if not proposed:
        return None
    result = {"proposal": proposed}
    if apply:
        result["apply_result"] = apply_term_memory_actions(memory, proposed.get("actions"))
    return result


__all__ = [
    "build_document_term_resolver_input",
    "propose_document_term_memory_actions",
    "resolve_document_term_memory_actions",
]
