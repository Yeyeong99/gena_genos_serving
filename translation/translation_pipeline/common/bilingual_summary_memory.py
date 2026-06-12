"""Bilingual continuity memory for long Office translation jobs."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.prompts import render_prompt


_SCHEMA_VERSION = "bilingual_summary_memory.v3"
_DEFAULT_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "bilingual_summary_memory"
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[가-힣]+")
_PENDING_WORD_THRESHOLD_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MIN_PENDING_WORDS"
_COMPRESSION_ENABLED_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_COMPRESSION_ENABLED"


def bilingual_summary_memory_enabled(style_options: dict[str, Any] | None = None) -> bool:
    if isinstance(style_options, dict) and "bilingual_summary_memory" in style_options:
        return bool(style_options.get("bilingual_summary_memory"))
    return os.getenv("AI_TRANSLATION_BILINGUAL_SUMMARY_MEMORY_ENABLED", "0").strip() != "0"


def source_word_count(texts: Iterable[str]) -> int:
    return sum(len(_WORD_RE.findall(str(text or ""))) for text in texts)


def _threshold(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def bilingual_summary_memory_compression_enabled(style_options: dict[str, Any] | None = None) -> bool:
    if isinstance(style_options, dict) and "bilingual_summary_memory_compression" in style_options:
        return bool(style_options.get("bilingual_summary_memory_compression"))
    return os.getenv(_COMPRESSION_ENABLED_ENV, "0").strip() != "0"


def should_enable_bilingual_summary_memory(
    translation_units: list[Any],
    *,
    scope_count: int,
    style_options: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, int]]:
    """Return whether long-document summary memory should be enabled."""

    total_words = source_word_count(getattr(unit, "text", "") for unit in translation_units)
    total_chars = sum(len(str(getattr(unit, "text", "") or "")) for unit in translation_units)
    unit_count = len([unit for unit in translation_units if str(getattr(unit, "text", "") or "").strip()])
    metrics = {
        "source_word_count": total_words,
        "total_chars": total_chars,
        "scope_count": scope_count,
        "translation_unit_count": unit_count,
    }
    if not bilingual_summary_memory_enabled(style_options):
        return False, metrics

    enabled = bilingual_summary_memory_enabled(style_options)
    return enabled, metrics


def create_bilingual_summary_memory(
    *,
    job_id: str,
    target_lang: str,
    doc_format: str,
    translation_units: list[Any],
    style_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    scopes = {
        str(getattr(unit, "context_scope", "") or f"unit:{getattr(unit, 'translation_unit_id', '')}")
        for unit in translation_units
    }
    enabled, metrics = should_enable_bilingual_summary_memory(
        translation_units,
        scope_count=len(scopes),
        style_options=style_options,
    )
    memory = {
        "schema_version": _SCHEMA_VERSION,
        "job_id": job_id,
        "target_lang": target_lang,
        "doc_format": doc_format,
        "enabled": enabled,
        "compression_enabled": bilingual_summary_memory_compression_enabled(style_options),
        **metrics,
        "raw_scopes": [],
        "raw_scope_count": 0,
        "raw_memory_word_count": 0,
        "raw_memory_char_count": 0,
        "raw_memory_last_scope": "",
        "summary": {
            "source_summary": "",
            "target_summary": "",
            "style_continuity": "",
            "discourse_state": "",
            "open_references": [],
        },
        "summaries": [],
        "scope_summaries": [],
        "pending_summary_scopes": [],
        "pending_summary_word_count": 0,
        "summary_update_min_words": _threshold(_PENDING_WORD_THRESHOLD_ENV, 1500),
        "summary_update_call_count": 0,
        "summary_update_skip_count": 0,
        "summary_update_llm_call_count": 0,
        "summary_update_total_elapsed_ms": 0,
        "summary_update_llm_elapsed_ms": 0,
        "summary_update_last_elapsed_ms": 0,
        "summary_update_last_llm_elapsed_ms": 0,
        "summary_update_last_scope": "",
        "summary_update_last_status": "initialized",
        "updated_at": time.time(),
    }
    if enabled:
        log_info(
            "[Bilingual Summary Memory] enabled "
            f"words={metrics['source_word_count']} chars={metrics['total_chars']} "
            f"units={metrics['translation_unit_count']} scopes={metrics['scope_count']} "
            f"compression_enabled={memory['compression_enabled']}"
        )
    else:
        log_info(
            "[Bilingual Summary Memory] skipped "
            f"words={metrics['source_word_count']} chars={metrics['total_chars']} "
            f"units={metrics['translation_unit_count']} scopes={metrics['scope_count']}"
        )
    return memory


def bilingual_summary_memory_is_enabled(memory: dict[str, Any] | None) -> bool:
    return isinstance(memory, dict) and bool(memory.get("enabled"))


def get_prompt_bilingual_summary(memory: dict[str, Any] | None) -> dict[str, Any]:
    if not bilingual_summary_memory_is_enabled(memory):
        return {}
    raw_scopes = [
        item
        for item in (memory.get("raw_scopes") or [])
        if isinstance(item, dict) and item.get("items")
    ]
    summaries = [
        item
        for item in (memory.get("summaries") or [])
        if _summary_has_content(item)
    ]
    latest_summary = memory.get("summary")
    if not summaries and _summary_has_content(latest_summary):
        summaries = [latest_summary]
    compression_enabled = bool(memory.get("compression_enabled"))
    if not compression_enabled and not raw_scopes:
        return {}
    if compression_enabled and not summaries:
        return {}
    prompt_memory = {
        "compression_enabled": compression_enabled,
        "scope_count": len(memory.get("scope_summaries") or []),
        "raw_scope_count": int(memory.get("raw_scope_count") or len(raw_scopes)),
        "raw_memory_word_count": int(memory.get("raw_memory_word_count") or 0),
        "raw_memory_char_count": int(memory.get("raw_memory_char_count") or 0),
    }
    if compression_enabled:
        prompt_memory.update({"summary": summaries[-1], "summaries": summaries})
    else:
        prompt_memory.update({"raw_scopes": raw_scopes})
    return prompt_memory


def _json_object_from_text(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if not stripped:
        return {}
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _scope_text_payload(units: list[Any], translated_by_unit_id: dict[int, str]) -> list[dict[str, str]]:
    payload = []
    for unit in units:
        source = str(getattr(unit, "text", "") or "").strip()
        if not source:
            continue
        unit_id = int(getattr(unit, "translation_unit_id", -1))
        target = str(translated_by_unit_id.get(unit_id, "") or "").strip()
        payload.append({"source": source, "target": target})
    return payload


def _scope_payload_word_count(scope_payload: list[dict[str, str]]) -> int:
    return source_word_count(item.get("source", "") for item in scope_payload)


def _scope_payload_char_count(scope_payload: list[dict[str, str]]) -> int:
    return sum(
        len(str(item.get("source") or "")) + len(str(item.get("target") or ""))
        for item in scope_payload
    )


def _append_raw_scope(memory: dict[str, Any], scope: str, scope_payload: list[dict[str, str]]) -> dict[str, Any]:
    word_count = _scope_payload_word_count(scope_payload)
    char_count = _scope_payload_char_count(scope_payload)
    raw_scope = {
        "scope": scope,
        "word_count": word_count,
        "char_count": char_count,
        "item_count": len(scope_payload),
        "items": scope_payload,
        "completed_at": time.time(),
    }
    raw_scopes = memory.setdefault("raw_scopes", [])
    if not isinstance(raw_scopes, list):
        raw_scopes = []
        memory["raw_scopes"] = raw_scopes
    raw_scopes.append(raw_scope)
    memory["raw_scope_count"] = len(raw_scopes)
    memory["raw_memory_word_count"] = int(memory.get("raw_memory_word_count") or 0) + word_count
    memory["raw_memory_char_count"] = int(memory.get("raw_memory_char_count") or 0) + char_count
    memory["raw_memory_last_scope"] = scope
    return raw_scope


def _summary_has_content(summary: Any) -> bool:
    if not isinstance(summary, dict):
        return False
    return any(
        str(summary.get(key) or "").strip()
        for key in ("source_summary", "target_summary", "style_continuity", "discourse_state")
    ) or bool(summary.get("open_references"))


def _pending_scope_entries(memory: dict[str, Any]) -> list[dict[str, Any]]:
    pending = memory.setdefault("pending_summary_scopes", [])
    if not isinstance(pending, list):
        pending = []
        memory["pending_summary_scopes"] = pending
    if any(not isinstance(item, dict) for item in pending):
        pending = [item for item in pending if isinstance(item, dict)]
        memory["pending_summary_scopes"] = pending
    return pending


def _pending_word_threshold(memory: dict[str, Any]) -> int:
    threshold = _threshold(_PENDING_WORD_THRESHOLD_ENV, 1500)
    memory["summary_update_min_words"] = threshold
    return threshold


def _pending_word_count(pending: list[dict[str, Any]]) -> int:
    return sum(int(item.get("word_count") or 0) for item in pending)


def _pending_scope_label(pending: list[dict[str, Any]], fallback: str) -> str:
    scopes = [str(item.get("scope") or "").strip() for item in pending if str(item.get("scope") or "").strip()]
    if not scopes:
        return fallback
    return "pending:" + ",".join(scopes)


def _pending_scope_items(pending: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in pending:
        for item in entry.get("items") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            if source:
                items.append({"source": source, "target": target})
    return items


def _normalize_summary_update(parsed: dict[str, Any], scope: str) -> dict[str, Any]:
    summary = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else parsed
    open_references = summary.get("open_references") if isinstance(summary, dict) else []
    if not isinstance(open_references, list):
        open_references = []
    scope_summary = parsed.get("scope_summary") if isinstance(parsed.get("scope_summary"), dict) else {}
    return {
        "summary": {
            "source_summary": str(summary.get("source_summary") or "").strip(),
            "target_summary": str(summary.get("target_summary") or "").strip(),
            "style_continuity": str(summary.get("style_continuity") or "").strip(),
            "discourse_state": str(summary.get("discourse_state") or "").strip(),
            "open_references": [str(item).strip() for item in open_references if str(item).strip()][:8],
        },
        "scope_summary": {
            "scope": scope,
            "source_summary": str(scope_summary.get("source_summary") or "").strip(),
            "target_summary": str(scope_summary.get("target_summary") or "").strip(),
            "source_topics": [
                str(item).strip()
                for item in (scope_summary.get("source_topics") or scope_summary.get("important_terms") or [])
                if str(item).strip()
            ][:12],
            "style_notes": [
                str(item).strip()
                for item in (scope_summary.get("style_notes") or [])
                if str(item).strip()
            ][:8],
            "created_at": time.time(),
        },
    }


async def update_bilingual_summary_memory(
    sem: Any,
    session: Any,
    memory: dict[str, Any] | None,
    *,
    scope: str,
    units: list[Any],
    translated_by_unit_id: dict[int, str],
) -> dict[str, Any] | None:
    """Update cumulative bilingual summary from one completed translation scope."""

    if not bilingual_summary_memory_is_enabled(memory):
        return memory
    total_started_at = time.perf_counter()
    scope_payload = _scope_text_payload(units, translated_by_unit_id)
    if not scope_payload:
        return memory
    memory["summary_update_call_count"] = int(memory.get("summary_update_call_count") or 0) + 1
    memory["summary_update_last_scope"] = scope
    raw_scope = _append_raw_scope(memory, scope, scope_payload)
    memory["updated_at"] = time.time()
    if not memory.get("compression_enabled"):
        elapsed_ms = int((time.perf_counter() - total_started_at) * 1000)
        memory["summary_update_last_elapsed_ms"] = elapsed_ms
        memory["summary_update_last_llm_elapsed_ms"] = 0
        memory["summary_update_total_elapsed_ms"] = int(memory.get("summary_update_total_elapsed_ms") or 0) + elapsed_ms
        memory["summary_update_last_status"] = "raw_appended_no_compression"
        memory["pending_summary_scopes"] = []
        memory["pending_summary_word_count"] = 0
        log_info(
            "[Bilingual Summary Memory] raw appended "
            f"scope={scope} scope_words={raw_scope.get('word_count')} "
            f"raw_scopes={memory.get('raw_scope_count')} "
            f"raw_words={memory.get('raw_memory_word_count')} "
            f"raw_chars={memory.get('raw_memory_char_count')} "
            f"compression_enabled=0 elapsed_ms={elapsed_ms}"
        )
        return memory
    pending = _pending_scope_entries(memory)
    pending.append(
        {
            "scope": scope,
            "word_count": _scope_payload_word_count(scope_payload),
            "items": scope_payload,
            "completed_at": time.time(),
        }
    )
    pending_words = _pending_word_count(pending)
    memory["pending_summary_word_count"] = pending_words
    threshold = _pending_word_threshold(memory)
    if pending_words < threshold:
        elapsed_ms = int((time.perf_counter() - total_started_at) * 1000)
        memory["summary_update_skip_count"] = int(memory.get("summary_update_skip_count") or 0) + 1
        memory["summary_update_last_elapsed_ms"] = elapsed_ms
        memory["summary_update_last_llm_elapsed_ms"] = 0
        memory["summary_update_total_elapsed_ms"] = int(memory.get("summary_update_total_elapsed_ms") or 0) + elapsed_ms
        memory["summary_update_last_status"] = "skipped_pending_words_below_threshold"
        memory["updated_at"] = time.time()
        log_info(
            "[Bilingual Summary Memory] update skipped "
            f"scope={scope} reason=pending_words_below_threshold "
            f"pending_words={pending_words}/{threshold} pending_scopes={len(pending)} "
            f"elapsed_ms={elapsed_ms} "
            f"total_overhead_ms={memory.get('summary_update_total_elapsed_ms')}"
        )
        return memory
    pending_items = _pending_scope_items(pending)
    pending_scope = _pending_scope_label(pending, scope)
    existing_summaries = [
        item
        for item in (memory.get("summaries") or [])
        if isinstance(item, dict)
    ]
    prompt = render_prompt(
        "bilingual_summary_memory_update.jinja",
        target_lang=memory.get("target_lang") or "",
        existing_summaries=existing_summaries,
        scope=pending_scope,
        scope_items=pending_items,
    )
    memory["summary_update_llm_call_count"] = int(memory.get("summary_update_llm_call_count") or 0) + 1
    llm_started_at = time.perf_counter()
    raw = await llm_call_async(sem, session, "", prompt)
    llm_elapsed_ms = int((time.perf_counter() - llm_started_at) * 1000)
    parsed = _json_object_from_text(raw)
    normalized = _normalize_summary_update(parsed, pending_scope) if parsed else {}
    if not normalized:
        elapsed_ms = int((time.perf_counter() - total_started_at) * 1000)
        memory["summary_update_skip_count"] = int(memory.get("summary_update_skip_count") or 0) + 1
        memory["summary_update_last_elapsed_ms"] = elapsed_ms
        memory["summary_update_last_llm_elapsed_ms"] = llm_elapsed_ms
        memory["summary_update_total_elapsed_ms"] = int(memory.get("summary_update_total_elapsed_ms") or 0) + elapsed_ms
        memory["summary_update_llm_elapsed_ms"] = int(memory.get("summary_update_llm_elapsed_ms") or 0) + llm_elapsed_ms
        memory["summary_update_last_status"] = "skipped_parse_failed"
        memory["updated_at"] = time.time()
        log_info(
            "[Bilingual Summary Memory] update skipped "
            f"scope={pending_scope} reason=parse_failed "
            f"elapsed_ms={elapsed_ms} llm_elapsed_ms={llm_elapsed_ms} "
            f"total_overhead_ms={memory.get('summary_update_total_elapsed_ms')} "
            f"llm_overhead_ms={memory.get('summary_update_llm_elapsed_ms')}"
        )
        return memory
    summary_entry = {
        **normalized["summary"],
        "scope": pending_scope,
        "created_at": time.time(),
    }
    memory.setdefault("summaries", []).append(summary_entry)
    memory["summary"] = summary_entry
    scope_summary = normalized["scope_summary"]
    if scope_summary.get("source_summary") or scope_summary.get("target_summary"):
        memory.setdefault("scope_summaries", []).append(scope_summary)
    memory["pending_summary_scopes"] = []
    memory["pending_summary_word_count"] = 0
    elapsed_ms = int((time.perf_counter() - total_started_at) * 1000)
    memory["summary_update_last_elapsed_ms"] = elapsed_ms
    memory["summary_update_last_llm_elapsed_ms"] = llm_elapsed_ms
    memory["summary_update_total_elapsed_ms"] = int(memory.get("summary_update_total_elapsed_ms") or 0) + elapsed_ms
    memory["summary_update_llm_elapsed_ms"] = int(memory.get("summary_update_llm_elapsed_ms") or 0) + llm_elapsed_ms
    memory["summary_update_last_status"] = "updated"
    memory["updated_at"] = time.time()
    log_info(
        "[Bilingual Summary Memory] updated "
        f"scope={pending_scope} scopes={len(memory.get('scope_summaries') or [])} "
        f"pending_words={pending_words}/{threshold} "
        f"elapsed_ms={elapsed_ms} llm_elapsed_ms={llm_elapsed_ms} "
        f"total_overhead_ms={memory.get('summary_update_total_elapsed_ms')} "
        f"llm_overhead_ms={memory.get('summary_update_llm_elapsed_ms')} "
        f"prompt_chars={len(prompt)}"
    )
    return memory


def bilingual_summary_memory_dump_dir() -> Path:
    return Path(os.getenv("AI_TRANSLATION_BILINGUAL_SUMMARY_MEMORY_DUMP_DIR", str(_DEFAULT_DUMP_DIR)))


def _safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9가-힣._-]+", "_", str(value or "").strip())
    return cleaned.strip("._")[:80]


def save_bilingual_summary_memory_to_local_file(
    job_id: str,
    memory: dict[str, Any] | None,
    *,
    artifact_label: str = "",
) -> str | None:
    if not isinstance(memory, dict):
        return None
    dump_dir = bilingual_summary_memory_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    safe_job = _safe_filename_part(job_id) or "unknown-job"
    safe_artifact = _safe_filename_part(artifact_label)
    prefix = f"{safe_artifact}__" if safe_artifact else ""
    path = dump_dir / f"{prefix}{safe_job}__bilingual-summary-memory.json"
    path.write_text(json.dumps(memory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)
