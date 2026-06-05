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
from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.prompts import render_prompt
from translation_pipeline.common.term_memory_core import _clean_evidence_text


_ENABLED_ENV_VAR = "AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_ENABLED"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_DEFAULT_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "document_term_memory_pre_judge"
_MAX_ENTRIES = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MAX_ENTRIES", "24"))
_MAX_OCCURRENCES_PER_TERM = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MAX_OCCURRENCES", "1000"))
_MIN_RELATED_BASE_COUNT = int(os.getenv("AI_TRANSLATION_DOCUMENT_TERM_PRE_JUDGE_MIN_RELATED_BASE_COUNT", "3"))


def document_term_pre_judge_enabled() -> bool:
    value = os.getenv(_ENABLED_ENV_VAR, "1").strip().lower()
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
    first = parts[0]
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


def _evidence_for_entry(entry: dict[str, Any], evidence_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # seed_analysis가 initial target을 결정할 때 본 문장을 먼저 포함한다.
    # pre_judge는 이 문장들을 보고 해당 번역어가 적합한지 검증해야 한다.
    source_keys = {normalize_document_source(source) for source in _entry_sources(entry)}
    evidence: list[dict[str, Any]] = []
    seen_snippets: set[str] = set()
    for evidence_entry in evidence_entries:
        evidence_source = normalize_document_source(evidence_entry.get("source_term"))
        aliases = {normalize_document_source(alias) for alias in (evidence_entry.get("aliases") or [])}
        if not source_keys.intersection({evidence_source, *aliases}):
            continue
        for occurrence in evidence_entry.get("occurrences") or []:
            if not isinstance(occurrence, dict):
                continue
            snippet = _clean_evidence_text(
                occurrence.get("source_snippet")
                or occurrence.get("surrounding_source")
                or occurrence.get("source")
            )
            if not snippet or snippet in seen_snippets:
                continue
            seen_snippets.add(snippet)
            item = {
                "source": snippet,
                "section": occurrence.get("section"),
                "table_title": occurrence.get("table_title"),
                "element_type": occurrence.get("element_type"),
            }
            evidence.append({key: value for key, value in item.items() if value})
            if len(evidence) >= _MAX_OCCURRENCES_PER_TERM:
                return evidence
    return evidence


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
        return False
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
    selected: list[dict[str, Any]] = []
    for term_id, entry in entries.items():
        if not isinstance(entry, dict) or not _entry_needs_pre_judge(entry, base_counts):
            continue
        source_base = _source_base(entry.get("source_term"))
        item = {
            "term_id": term_id,
            "source_term": entry.get("source_term"),
            "source_terms": _entry_sources(entry),
            "memory_kind": clean_memory_kind(entry.get("memory_kind"), "term"),
            "status": entry.get("status"),
            "needs_review": bool(entry.get("needs_review")),
            "preferred_target": entry.get("preferred_target"),
            "target_candidates": _target_candidates(entry),
            "meaning": entry.get("meaning"),
            "why_it_matters": entry.get("why_it_matters"),
            "target_language_risk": entry.get("target_language_risk"),
            "confidence": entry.get("confidence"),
            "related_source_base_count": base_counts.get(source_base, 0) if source_base else 0,
            "evidence": _evidence_for_entry(entry, evidence_entries),
        }
        selected.append({key: value for key, value in item.items() if value not in ("", [], None)})
        if len(selected) >= _MAX_ENTRIES:
            break
    return {
        "judge_type": "document_term_memory_pre_judge",
        "source_only": True,
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


def _sanitize_pre_judge_actions(parsed: dict[str, Any], pre_judge_input: dict[str, Any]) -> dict[str, Any]:
    allowed_types = {"no_update", "mark_preferred", "update_sense"}
    allowed_relations = {"same_meaning_variant", "acceptable_variant", "different_sense"}
    valid_sources = _valid_source_terms(pre_judge_input)
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
        if action_type != "no_update" and source_key not in valid_sources:
            rejected.append({"action": action, "reason": "source_term_not_in_pre_judge_input"})
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
    return {
        "judge_type": parsed.get("judge_type") or "document_term_memory_pre_judge",
        "actions": actions,
        "rejected_actions": rejected,
        "caveats": parsed.get("caveats") or [],
    }


def _find_entry_by_source(memory: dict[str, Any], source_term: Any) -> dict[str, Any] | None:
    source_key = normalize_document_source(source_term)
    if not source_key:
        return None
    entries = memory.get("entries")
    if not isinstance(entries, dict):
        return None
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        for source in _entry_sources(entry):
            if normalize_document_source(source) == source_key:
                return entry
    return None


def _mark_pre_judge_reviewed_entries(memory: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    now = time.time()
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = _clean_text(action.get("type"))
        if action_type not in {"mark_preferred", "no_update"}:
            continue
        entry = _find_entry_by_source(memory, action.get("source_term") or action.get("source"))
        if not isinstance(entry, dict):
            continue
        target = _clean_text(action.get("target") or action.get("preferred_target") or entry.get("preferred_target"))
        if action_type == "no_update" and not entry.get("preferred_target"):
            continue
        entry["needs_review"] = False
        entry["target_decision_needed"] = False
        entry["status"] = _clean_text(action.get("status")) or ("preferred" if action_type == "mark_preferred" else "initial_seed")
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
    dump_dir = document_term_pre_judge_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    job_id = _safe_filename_part((memory or {}).get("job_id")) or f"pre-judge-{uuid.uuid4().hex[:12]}"
    artifact = _safe_filename_part((memory or {}).get("_artifact_label"))
    stamp = int(time.time() * 1000)
    prefix = "__".join(item for item in (artifact, job_id, str(stamp)) if item)
    path = dump_dir / f"{prefix}-pre-judge.json"
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
    prompt = render_prompt(
        "document_term_pre_judge.jinja",
        target_lang=target_lang,
        pre_judge_input_json=json.dumps(pre_judge_input, ensure_ascii=False, indent=2),
    )
    started_at = time.perf_counter()
    try:
        raw = await llm_call_async(sem, session, "", prompt)
    except Exception as exc:
        log_info(f"[Document Term Pre-Judge] LLM call failed: {exc}")
        return None
    parsed = _parse_json_object(raw)
    if not parsed:
        snapshot_path = _save_pre_judge_snapshot(memory, pre_judge_input, prompt, None, None)
        log_info(
            "[Document Term Pre-Judge] returned non-JSON "
            f"elapsed={time.perf_counter() - started_at:.2f}s snapshot={snapshot_path}"
        )
        return None
    sanitized = _sanitize_pre_judge_actions(parsed, pre_judge_input)
    result: dict[str, Any] = {"proposal": sanitized}
    if apply:
        result["apply_result"] = apply_term_memory_actions(memory, sanitized.get("actions"))
        _mark_pre_judge_reviewed_entries(memory, sanitized.get("actions") or [])
        memory["_last_pre_judge_result"] = result
        sanitize_document_term_memory(memory)
    snapshot_path = _save_pre_judge_snapshot(memory, pre_judge_input, prompt, sanitized, result)
    if isinstance(memory, dict):
        memory["_pre_judge_dump_path"] = snapshot_path
    if sanitized.get("rejected_actions"):
        log_info(
            "[Document Term Pre-Judge] rejected actions "
            f"count={len(sanitized.get('rejected_actions') or [])}"
        )
    log_info(
        "[Document Term Pre-Judge] complete "
        f"entries={len(pre_judge_input.get('entries') or [])} "
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
