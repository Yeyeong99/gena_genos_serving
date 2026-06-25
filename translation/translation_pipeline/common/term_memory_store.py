"""Storage, lookup, and persistence for document-local term memory.

The JSON shape is intentionally unchanged:
``pending``, ``review``, ``soft_locked``, ``locked``, ``applied_terms``, and
``excluded`` remain compatible with existing Redis/local dumps.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterable

from translation_pipeline.common.job_artifacts import job_artifact_path
from translation_pipeline.common.term_memory_core import (
    _MAX_OCCURRENCES_PER_TERM,
    _MAX_RELEVANT_TERMS,
    _REDIS_TTL_SECONDS,
    _SCHEMA_VERSION,
    _contains_term,
    _has_ambiguous_acronym_aliases,
    _has_unrelated_acronym,
    _is_bad_target_candidate,
    _is_target_too_short_for_source,
    _sample_occurrences_evenly,
    _token_count,
    normalize_source,
)


_RELEVANT_TERM_BUCKET_PRIORITY = {
    "locked": 0,
    "soft_locked": 1,
}


def glossary_enabled(style_options: dict[str, Any] | None = None) -> bool:
    """Return whether temporary glossary logic should run."""

    if isinstance(style_options, dict) and style_options.get("temporary_glossary") is False:
        return False
    value = os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def create_memory(*, job_id: str = "", target_lang: str = "") -> dict[str, Any]:
    now = time.time()
    return {
        "schema_version": _SCHEMA_VERSION,
        "job_id": job_id,
        "target_lang": target_lang,
        "created_at": now,
        "updated_at": now,
        "pending": {},
        "review": {},
        "soft_locked": {},
        "locked": {},
        "applied_terms": [],
        "excluded": [],
    }


def _next_term_id(memory: dict[str, Any]) -> str:
    max_id = 0
    for bucket in ("pending", "review", "soft_locked", "locked"):
        for term_id in (memory.get(bucket) or {}).keys():
            match = re.search(r"(\d+)$", str(term_id))
            if match:
                max_id = max(max_id, int(match.group(1)))
    return f"term_{max_id + 1:03d}"


def _find_term(memory: dict[str, Any], normalized_source: str) -> tuple[str, str, dict[str, Any]] | None:
    for bucket in ("pending", "review", "soft_locked", "locked"):
        entries = memory.get(bucket)
        if not isinstance(entries, dict):
            continue
        for term_id, entry in entries.items():
            normalized_terms = {normalize_source(entry.get("source_term"))}
            normalized_terms.update(normalize_source(item) for item in entry.get("aliases") or [])
            if normalized_source in normalized_terms:
                return bucket, str(term_id), entry
    return None


def update_memory_from_scan(memory: dict[str, Any], scan_result: dict[str, Any]) -> dict[str, Any]:
    if scan_result.get("source_term_language"):
        memory["source_term_language"] = scan_result.get("source_term_language")
    pending = memory.setdefault("pending", {})
    for candidate in (scan_result.get("candidates") or {}).values():
        if not isinstance(candidate, dict):
            continue
        normalized = normalize_source(candidate.get("normalized_source") or candidate.get("source_term"))
        existing = _find_term(memory, normalized)
        if existing:
            bucket, _term_id, entry = existing
            entry["frequency"] = max(int(entry.get("frequency") or 0), int(candidate.get("frequency") or 0))
            entry["chunk_count"] = max(int(entry.get("chunk_count") or 0), int(candidate.get("chunk_count") or 0))
            entry["section_count"] = max(int(entry.get("section_count") or 0), int(candidate.get("section_count") or 0))
            entry["candidate_score"] = max(float(entry.get("candidate_score") or 0.0), float(candidate.get("candidate_score") or 0.0))
            entry["match_priority"] = max(int(entry.get("match_priority") or 0), int(candidate.get("match_priority") or 0))
            merged_reasons = set(entry.get("reason") or []) | set(candidate.get("reason") or [])
            merged_types = set(entry.get("candidate_types") or []) | set(candidate.get("candidate_types") or [])
            merged_aliases = set(entry.get("aliases") or []) | set(candidate.get("aliases") or [])
            entry["reason"] = sorted(str(item) for item in merged_reasons)
            entry["candidate_types"] = sorted(str(item) for item in merged_types)
            entry["aliases"] = sorted(str(item) for item in merged_aliases if str(item).strip())
            occurrences = entry.setdefault("occurrences", [])
            seen = {
                (
                    item.get("chunk_id"),
                    item.get("unit_id"),
                    item.get("source_snippet"),
                )
                for item in occurrences
                if isinstance(item, dict)
            }
            for occurrence in candidate.get("occurrences") or []:
                key = (
                    occurrence.get("chunk_id"),
                    occurrence.get("unit_id"),
                    occurrence.get("source_snippet"),
                )
                if key in seen:
                    continue
                occurrences.append(occurrence)
                seen.add(key)
            entry["occurrences"] = _sample_occurrences_evenly(occurrences, _MAX_OCCURRENCES_PER_TERM)
            entry["updated_at"] = time.time()
            if bucket != "pending":
                memory[bucket][_term_id] = entry
            continue

        term_id = str(candidate.get("term_id") or _next_term_id(memory))
        while term_id in pending:
            term_id = _next_term_id(memory)
        candidate = {**candidate, "term_id": term_id, "status": "pending"}
        pending[term_id] = candidate

    memory.setdefault("excluded", []).extend(scan_result.get("excluded") or [])
    memory["updated_at"] = time.time()
    return memory


def _iter_locked_entries(memory: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    for bucket in ("locked", "soft_locked"):
        entries = memory.get(bucket) or {}
        if not isinstance(entries, dict):
            continue
        for term_id, entry in entries.items():
            target = entry.get("target_term") or entry.get("target")
            if not isinstance(entry, dict) or not entry.get("source_term") or not target:
                continue
            if bucket == "soft_locked" and entry.get("review_reason"):
                continue
            if _is_target_too_short_for_source(str(entry.get("source_term") or ""), str(target)):
                continue
            if _is_bad_target_candidate(str(entry.get("source_term") or ""), str(target)):
                continue
            if bucket == "soft_locked" and _has_unrelated_acronym(entry, str(target)):
                continue
            if bucket == "soft_locked" and _has_ambiguous_acronym_aliases(entry):
                continue
            yield bucket, str(term_id), entry


def _matching_entry_source(entry: dict[str, Any], source_text: str) -> str:
    candidates = [str(entry.get("source_term") or "").strip()]
    candidates.extend(str(item).strip() for item in entry.get("aliases") or [] if str(item).strip())
    candidates.sort(key=lambda item: (-_token_count(item), -len(item)))
    for candidate in candidates:
        if _contains_term(source_text, candidate):
            return candidate
    return ""


def find_relevant_terms(
    memory: dict[str, Any] | None,
    texts: Iterable[str],
    *,
    limit: int = _MAX_RELEVANT_TERMS,
) -> list[dict[str, Any]]:
    if not isinstance(memory, dict):
        return []
    source_text = "\n".join(str(text or "") for text in texts)
    matched: list[dict[str, Any]] = []
    for bucket, term_id, entry in _iter_locked_entries(memory):
        matched_source = _matching_entry_source(entry, source_text)
        if not matched_source:
            continue
        matched.append(
            {
                "term_id": term_id,
                "source": str(entry.get("source_term") or matched_source).strip(),
                "matched_source": matched_source,
                "target": str(entry.get("target_term") or entry.get("target") or "").strip(),
                "status": bucket,
                "confidence": entry.get("confidence"),
                "term_version": entry.get("version", 1),
                "match_priority": entry.get("match_priority", 0),
                "token_count": max(int(entry.get("token_count") or 0), _token_count(matched_source)),
            }
        )
    matched.sort(
        key=lambda item: (
            _RELEVANT_TERM_BUCKET_PRIORITY.get(str(item.get("status") or ""), 99),
            -int(item.get("token_count") or 0),
            -int(item.get("match_priority") or 0),
            str(item.get("source") or ""),
        )
    )
    return matched[:limit]


def record_applied_terms(
    memory: dict[str, Any] | None,
    *,
    chunk_id: str,
    terms: list[dict[str, Any]],
) -> None:
    if not isinstance(memory, dict) or not terms:
        return
    now = time.time()
    memory.setdefault("applied_terms", []).append(
        {
            "chunk_id": chunk_id,
            "glossary_version": memory.get("schema_version", _SCHEMA_VERSION),
            "applied_terms": [
                {
                    "source": item.get("source"),
                    "target": item.get("target"),
                    "lock_type": item.get("status"),
                    "term_id": item.get("term_id"),
                    "term_version": item.get("term_version", 1),
                }
                for item in terms
            ],
        }
    )
    for item in terms:
        term_id = str(item.get("term_id") or "")
        if not term_id:
            continue
        for bucket in ("locked", "soft_locked"):
            entry = (memory.get(bucket) or {}).get(term_id)
            if not isinstance(entry, dict):
                continue
            entry["used_in_prompt"] = True
            entry["last_applied_chunk_id"] = chunk_id
            entry["last_applied_at"] = now
            entry["prompt_use_count"] = int(entry.get("prompt_use_count") or 0) + 1
            break
    memory["updated_at"] = now


def memory_summary(memory: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(memory, dict):
        return {"pending": 0, "soft_locked": 0, "locked": 0, "applied_terms": 0}
    return {
        "pending": len(memory.get("pending") or {}),
        "review": len(memory.get("review") or {}),
        "soft_locked": len(memory.get("soft_locked") or {}),
        "locked": len(memory.get("locked") or {}),
        "applied_terms": len(memory.get("applied_terms") or []),
    }


def dumps_memory(memory: dict[str, Any]) -> str:
    return json.dumps(memory, ensure_ascii=False, separators=(",", ":"))


def loads_memory(raw: str | bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None


def redis_enabled() -> bool:
    value = os.getenv("AI_TRANSLATION_TEMP_GLOSSARY_REDIS_ENABLED", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def redis_key(job_id: str) -> str:
    return f"translation:temporary_glossary:{job_id}"


def save_memory_to_local_file(job_id: str, memory: dict[str, Any], *, artifact_label: str = "") -> str:
    """Write the temporary glossary to the job-scoped local artifact folder."""

    path = job_artifact_path(job_id, artifact_label, "temporary_glossary.json")
    with open(path, "w", encoding="utf-8") as output:
        json.dump(memory, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return str(path)


async def _redis_client() -> Any | None:
    if not redis_enabled():
        return None
    try:
        from redis.asyncio import Redis  # type: ignore
    except Exception:
        return None
    host = os.getenv("REDIS_HOST", "localhost")
    try:
        port = int(os.getenv("REDIS_PORT", "6379"))
    except ValueError:
        port = 6379
    password = os.getenv("REDIS_PASSWORD") or None
    return Redis(host=host, port=port, password=password, decode_responses=False)


async def save_memory_to_redis(job_id: str, memory: dict[str, Any]) -> bool:
    """Best-effort Redis save. Returns False when Redis is disabled/unavailable."""

    client = await _redis_client()
    if client is None:
        return False
    try:
        await client.setex(redis_key(job_id), _REDIS_TTL_SECONDS, dumps_memory(memory))
        return True
    except Exception:
        return False
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def load_memory_from_redis(job_id: str) -> dict[str, Any] | None:
    """Best-effort Redis load. Returns None when Redis is disabled/unavailable."""

    client = await _redis_client()
    if client is None:
        return None
    try:
        return loads_memory(await client.get(redis_key(job_id)))
    except Exception:
        return None
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


__all__ = [
    "create_memory",
    "dumps_memory",
    "find_relevant_terms",
    "glossary_enabled",
    "load_memory_from_redis",
    "loads_memory",
    "memory_summary",
    "record_applied_terms",
    "redis_enabled",
    "redis_key",
    "save_memory_to_local_file",
    "save_memory_to_redis",
    "update_memory_from_scan",
]
