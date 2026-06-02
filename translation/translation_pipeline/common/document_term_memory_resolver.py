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
from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.prompts import render_prompt
from translation_pipeline.common.term_memory_core import _chunk_id

_DEFAULT_PROMPT_SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory_resolver_prompts"


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
                        "source_snippet": occurrence.get("source_snippet"),
                        "surrounding_source": occurrence.get("surrounding_source"),
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


def _entry_needs_resolver(entry: dict[str, Any]) -> bool:
    status = str(entry.get("status") or "")
    if bool(entry.get("target_decision_needed")):
        return True
    if not entry.get("preferred_target") and status not in {"confirmed", "locked"}:
        return True
    if status in {"analysis_candidate", "review"}:
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
    if any(_entry_needs_resolver(entry) for entry in matched):
        return True, "needs_resolution"
    return False, "stable_relevant_terms"


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
        source_terms = _action_source_terms(action)
        action_source_keys = {normalize_document_source(term) for term in source_terms if normalize_document_source(term)}
        if not action_source_keys or not (action_source_keys & allowed_source_keys):
            rejected.append({"reason": "source_term_not_in_current_scope", "action": action})
            continue
        normalized_action = dict(action)
        if normalized_action.get("target") is not None:
            normalized_action["target"] = _clean_target(normalized_action.get("target"))
        if normalized_action.get("preferred_target") is not None:
            normalized_action["preferred_target"] = _clean_target(normalized_action.get("preferred_target"))
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
    dump_dir = resolver_prompt_snapshot_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    job_id = _safe_filename_part((memory or {}).get("job_id")) or f"resolver-{uuid.uuid4().hex[:12]}"
    artifact = _safe_filename_part((memory or {}).get("_artifact_label"))
    scope = _safe_filename_part(((resolver_input.get("observed_translations") or [{}])[0] or {}).get("context_scope"))
    stamp = int(time.time() * 1000)
    prefix = "__".join(item for item in (artifact, job_id, scope, str(stamp)) if item)
    path = dump_dir / f"{prefix}-resolver-prompt.json"
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
