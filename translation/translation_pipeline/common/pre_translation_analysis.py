"""Pre-translation analysis built from document-local term memory.

This is the cold-start context layer: it runs before translation has produced a
DelTA-style bilingual summary. It uses source-only occurrence evidence from term
memory and stores the resulting analysis separately from the term memory schema.
"""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.prompts import render_prompt
from translation_pipeline.common.term_memory_core import _clean_evidence_text


_DEFAULT_MAX_TERMS = int(os.getenv("AI_TRANSLATION_PRE_ANALYSIS_MAX_TERMS", "64"))
_DEFAULT_MAX_OCCURRENCES_PER_TERM = int(os.getenv("AI_TRANSLATION_PRE_ANALYSIS_MAX_OCCURRENCES_PER_TERM", "2"))
_DEFAULT_MAX_CHARS = int(os.getenv("AI_TRANSLATION_PRE_ANALYSIS_MAX_CHARS", "24000"))
_ENABLED_ENV_VAR = "AI_TRANSLATION_PRE_ANALYSIS_ENABLED"
_INITIAL_TERM_DECISION_ENV_VAR = "AI_TRANSLATION_INITIAL_TERM_DECISION_ENABLED"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_DEFAULT_DUMP_DIR = (
    Path(__file__).resolve().parents[2] / "tmp" / "pre_analysis"
)
_DEFAULT_INITIAL_GLOSSARY_DUMP_DIR = (
    Path(__file__).resolve().parents[2] / "tmp" / "initial_glossary"
)


def _env_enabled(env_var: str, *, default: str = "1") -> bool:
    value = os.getenv(env_var, default).strip().lower()
    return value not in _DISABLED_VALUES


def _env_enabled_log_detail(env_var: str, *, default: str = "1") -> tuple[bool, str]:
    raw_value = os.getenv(env_var)
    normalized = (raw_value if raw_value is not None else default).strip().lower()
    enabled = normalized not in _DISABLED_VALUES
    source = "env" if raw_value is not None else "default"
    return enabled, f"{env_var}={normalized} ({source})"


def pre_translation_analysis_enabled(style_options: dict[str, Any] | None = None) -> bool:
    return _env_enabled(_ENABLED_ENV_VAR)


def initial_term_decision_enabled(style_options: dict[str, Any] | None = None) -> bool:
    return _env_enabled(_INITIAL_TERM_DECISION_ENV_VAR)


def _iter_candidate_entries(memory: dict[str, Any]) -> list[dict[str, Any]]:
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


def build_analysis_sample_from_term_memory(
    memory: dict[str, Any] | None,
    *,
    max_terms: int = _DEFAULT_MAX_TERMS,
    max_occurrences_per_term: int = _DEFAULT_MAX_OCCURRENCES_PER_TERM,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Build a source-only sample from term-memory occurrence evidence."""

    if not isinstance(memory, dict):
        return {"sample_type": "term_memory_occurrences", "terms": []}

    terms: list[dict[str, Any]] = []
    seen_snippets: set[str] = set()
    char_count = 0
    for entry in _iter_candidate_entries(memory):
        if len(terms) >= max_terms or char_count >= max_chars:
            break
        occurrences = []
        for occurrence in entry.get("occurrences") or []:
            if not isinstance(occurrence, dict):
                continue
            snippet = _clean_evidence_text(
                occurrence.get("source_snippet")
                or occurrence.get("surrounding_source")
                or ""
            )
            if not snippet or snippet in seen_snippets:
                continue
            seen_snippets.add(snippet)
            payload = {
                "section": occurrence.get("section"),
                "section_path": occurrence.get("section_path"),
                "container_type": occurrence.get("container_type"),
                "table_title": occurrence.get("table_title"),
                "element_type": occurrence.get("element_type"),
                "source": snippet,
            }
            occurrences.append({key: value for key, value in payload.items() if value})
            char_count += len(snippet)
            if len(occurrences) >= max_occurrences_per_term or char_count >= max_chars:
                break
        if not occurrences:
            continue
        terms.append(
            {
                "source_term": entry.get("source_term"),
                "aliases": entry.get("aliases") or [],
                "candidate_types": entry.get("candidate_types") or [],
                "frequency": entry.get("frequency"),
                "candidate_score": entry.get("candidate_score"),
                "occurrences": occurrences,
            }
        )

    return {
        "sample_type": "term_memory_occurrences",
        "source_only": True,
        "term_count": len(terms),
        "char_count": char_count,
        "terms": terms,
    }


def _parse_analysis_json(raw: str) -> dict[str, Any] | None:
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


def _normalized_llm_analysis(
    document_analysis: dict[str, Any],
    term_decision_analysis: dict[str, Any],
    sample: dict[str, Any],
    *,
    target_lang: str,
    document_context_enabled: bool,
    initial_term_decision_enabled: bool,
) -> dict[str, Any]:
    initial_document_terms = (
        term_decision_analysis.get("entries")
        or term_decision_analysis.get("initial_document_terms")
        or term_decision_analysis.get("selected_terms")
        or []
    )
    if document_context_enabled and initial_term_decision_enabled:
        source = "llm_parallel_document_context_and_initial_term_decision"
    elif initial_term_decision_enabled:
        source = "llm_initial_term_decision"
    else:
        source = "llm_document_context_analysis"
    merged = {
        **document_analysis,
        "analysis_type": "pre_translation_analysis",
        "source": source,
        "source_only": False,
        "sample_source_only": True,
        "target_lang": target_lang,
        "document_context_enabled": document_context_enabled,
        "initial_term_decision_enabled": initial_term_decision_enabled,
        "document_context_analysis": document_analysis,
        "initial_glossary_analysis": term_decision_analysis,
        "initial_document_terms": initial_document_terms,
        "sample_summary": {
            "sample_type": sample.get("sample_type"),
            "term_count": sample.get("term_count", 0),
            "char_count": sample.get("char_count", 0),
        },
    }
    caveats: list[Any] = []
    for value in (document_analysis.get("caveats"), term_decision_analysis.get("caveats")):
        if isinstance(value, list):
            caveats.extend(value)
    if caveats:
        merged["caveats"] = caveats
    return merged


async def _run_analysis_prompt(
    sem: Any,
    session: aiohttp.ClientSession,
    *,
    template_name: str,
    target_lang: str,
    sample: dict[str, Any],
    label: str,
) -> dict[str, Any] | None:
    prompt = render_prompt(
        template_name,
        target_lang=target_lang,
        sample_json=json.dumps(sample, ensure_ascii=False, indent=2),
    )
    started_at = time.perf_counter()
    try:
        raw = await llm_call_async(sem, session, "", prompt)
    except Exception as exc:
        log_info(f"[Pre-Translation Analysis] {label} LLM call failed: {exc}")
        return None
    log_info(
        "[Pre-Translation Analysis] "
        f"{label} LLM call done {time.perf_counter() - started_at:.2f}s "
        f"prompt_chars={len(prompt)}"
    )
    parsed = _parse_analysis_json(raw)
    if not parsed:
        log_info(f"[Pre-Translation Analysis] {label} returned non-JSON analysis")
        return None
    return parsed


async def run_pre_translation_analysis(
    sem: Any,
    session: aiohttp.ClientSession | None,
    memory: dict[str, Any] | None,
    *,
    target_lang: str = "",
    style_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run pre-translation analysis from term-memory occurrences."""

    document_context_enabled, document_context_detail = _env_enabled_log_detail(_ENABLED_ENV_VAR)
    term_decision_enabled, term_decision_detail = _env_enabled_log_detail(_INITIAL_TERM_DECISION_ENV_VAR)
    if not document_context_enabled and not term_decision_enabled:
        log_info(
            "[Pre-Translation Analysis] disabled: "
            f"{document_context_detail}; {term_decision_detail}"
        )
        return None
    if document_context_enabled:
        log_info(f"[Pre-Translation Analysis] document context enabled: {document_context_detail}")
    else:
        log_info(f"[Pre-Translation Analysis] document context disabled: {document_context_detail}")
    if term_decision_enabled:
        log_info(f"[Pre-Translation Analysis] initial term decision enabled: {term_decision_detail}")
    else:
        log_info(f"[Pre-Translation Analysis] initial term decision disabled: {term_decision_detail}")

    sample = build_analysis_sample_from_term_memory(memory)
    if not sample.get("terms"):
        log_info("[Pre-Translation Analysis] skipped: no term occurrence samples")
        return None

    if sem is None or session is None:
        log_info("[Pre-Translation Analysis] skipped: LLM session is unavailable")
        return None
    log_info(
        "[Pre-Translation Analysis] running "
        f"sample_terms={sample.get('term_count', 0)} "
        f"sample_chars={sample.get('char_count', 0)}"
    )

    tasks: list[tuple[str, Any]] = []
    if document_context_enabled:
        tasks.append(
            (
                "document_context",
                _run_analysis_prompt(
                    sem,
                    session,
                    template_name="pre_translation_analysis.jinja",
                    target_lang=target_lang,
                    sample=sample,
                    label="document context",
                ),
            )
        )
    if term_decision_enabled:
        tasks.append(
            (
                "initial_term_decision",
                _run_analysis_prompt(
                    sem,
                    session,
                    template_name="document_term_seed_analysis.jinja",
                    target_lang=target_lang,
                    sample=sample,
                    label="initial term decision",
                ),
            )
        )
    analysis_started_at = time.perf_counter()
    results = await asyncio.gather(*(task for _, task in tasks))
    log_info(
        "[Pre-Translation Analysis] LLM calls elapsed "
        f"{time.perf_counter() - analysis_started_at:.2f}s"
    )
    result_by_label = dict(zip((label for label, _ in tasks), results))
    document_analysis = result_by_label.get("document_context")
    term_decision_analysis = result_by_label.get("initial_term_decision")
    if not document_analysis and not term_decision_analysis:
        log_info("[Pre-Translation Analysis] skipped: both analysis calls failed")
        return None
    result = _normalized_llm_analysis(
        document_analysis or {},
        term_decision_analysis or {},
        sample,
        target_lang=target_lang,
        document_context_enabled=document_context_enabled,
        initial_term_decision_enabled=term_decision_enabled,
    )
    log_info(
        "[Pre-Translation Analysis] complete "
        f"initial_terms={len(result.get('initial_document_terms') or [])} "
        f"excluded_terms={len((result.get('initial_glossary_analysis') or {}).get('excluded') or [])}"
    )
    return result


def pre_analysis_dump_dir() -> Path:
    value = os.getenv("AI_TRANSLATION_PRE_ANALYSIS_DUMP_DIR", "").strip()
    return Path(value) if value else _DEFAULT_DUMP_DIR


def initial_glossary_dump_dir() -> Path:
    value = os.getenv("AI_TRANSLATION_INITIAL_GLOSSARY_DUMP_DIR", "").strip()
    return Path(value) if value else _DEFAULT_INITIAL_GLOSSARY_DUMP_DIR


def _safe_dump_prefix(job_id: str, artifact_label: str = "", *, fallback: str) -> str:
    safe_job_id = re.sub(r"[^0-9A-Za-z가-힣_.() -]+", "_", str(job_id or "").strip())
    safe_job_id = re.sub(r"\s+", "_", safe_job_id).strip("._- ")
    safe_label = re.sub(r"[^0-9A-Za-z가-힣_.() -]+", "_", str(artifact_label or "").strip())
    safe_label = re.sub(r"\s+", "_", safe_label).strip("._- ")
    if safe_label and safe_job_id:
        return f"{safe_label[:120]}__{safe_job_id[:120]}"
    if safe_job_id:
        return safe_job_id[:120]
    if safe_label:
        return safe_label[:120]
    return f"{fallback}-{uuid.uuid4().hex[:12]}"


def save_pre_analysis_to_local_file(
    job_id: str,
    analysis: dict[str, Any],
    *,
    artifact_label: str = "",
) -> str:
    if not isinstance(analysis, dict) or not analysis:
        return ""
    dump_dir = pre_analysis_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    prefix = _safe_dump_prefix(job_id, artifact_label, fallback="pre-analysis")
    path = dump_dir / f"{prefix}-pre-analysis.json"
    payload = {
        **analysis,
        "job_id": job_id or None,
        "artifact_label": artifact_label or None,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def save_initial_glossary_to_local_file(
    job_id: str,
    glossary_analysis: dict[str, Any],
    *,
    artifact_label: str = "",
) -> str:
    if not isinstance(glossary_analysis, dict) or not glossary_analysis:
        return ""
    dump_dir = initial_glossary_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    prefix = _safe_dump_prefix(job_id, artifact_label, fallback="initial-glossary")
    path = dump_dir / f"{prefix}-initial-glossary.json"
    payload = {
        **glossary_analysis,
        "job_id": job_id or None,
        "artifact_label": artifact_label or None,
        "saved_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


__all__ = [
    "build_analysis_sample_from_term_memory",
    "initial_glossary_dump_dir",
    "initial_term_decision_enabled",
    "pre_translation_analysis_enabled",
    "run_pre_translation_analysis",
    "save_initial_glossary_to_local_file",
    "save_pre_analysis_to_local_file",
]
