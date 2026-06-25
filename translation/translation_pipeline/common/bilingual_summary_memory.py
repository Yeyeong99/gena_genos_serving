"""Bilingual continuity memory for long Office translation jobs."""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from translation_pipeline.common.job_artifacts import job_artifact_dir, job_artifact_path
from translation_pipeline.common.llm import llm_call_async
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.prompts import render_prompt
from translation_pipeline.common.retrieval import bm25_rank_documents


_SCHEMA_VERSION = "bilingual_summary_memory.v3"
_DEFAULT_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "bilingual_summary_memory"
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[가-힣]+")
_COMPRESSION_ENABLED_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_COMPRESSION_ENABLED"
_PROMPT_BUDGET_TOKENS_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_PROMPT_BUDGET_TOKENS"
_SUMMARY_TARGET_TOKENS_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_TARGET_TOKENS"
_MODEL_CONTEXT_TOKENS_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MODEL_CONTEXT_TOKENS"
_MEMORY_CONTEXT_RATIO_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MEMORY_CONTEXT_RATIO"
_DOCUMENT_SCALE_RATIO_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_DOCUMENT_SCALE_RATIO"
_SUMMARY_TARGET_RATIO_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_TARGET_RATIO"
_MEMORY_MODE_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MEMORY_MODE"
_MARKDOWN_MEMORY_DIR_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MARKDOWN_MEMORY_DIR"
_MARKDOWN_MEMORY_MAX_CHARS_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MARKDOWN_MAX_CHARS"
_MARKDOWN_MEMORY_SECTION_MAX_CHARS_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MARKDOWN_SECTION_MAX_CHARS"
_MARKDOWN_PROMPT_MAX_CHARS_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MARKDOWN_PROMPT_MAX_CHARS"
_MARKDOWN_BM25_TOP_K_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_MARKDOWN_BM25_TOP_K"
_RECENT_RAW_SCOPE_LIMIT_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_RECENT_RAW_SCOPES"
_MARKDOWN_SELECTIVE_STORE_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_SELECTIVE_STORE_ENABLED"
_PROMPT_INJECTION_POLICY_ENV = "AI_TRANSLATION_BILINGUAL_SUMMARY_PROMPT_POLICY"
_INLINE_SUMMARY_MODE = "inline_summary"
_EXTERNAL_MARKDOWN_MODE = "external_markdown"
_PROMPT_POLICY_AUTO = "auto"
_PROMPT_POLICY_SELECTIVE = "selective"
_PROMPT_POLICY_ALWAYS = "always"
_PROMPT_POLICY_OFF = "off"
_MODEL_CONTEXT_TOKENS_DEFAULT = 32768
_MEMORY_CONTEXT_RATIO_DEFAULT = 0.125
_DOCUMENT_SCALE_RATIO_DEFAULT = 0.125
_SUMMARY_TARGET_RATIO_DEFAULT = 0.25
_PROMPT_BUDGET_MIN_TOKENS = 1024
_SUMMARY_TARGET_MIN_TOKENS = 256
_MARKDOWN_MEMORY_MAX_CHARS_DEFAULT = 48000
_MARKDOWN_MEMORY_SECTION_MAX_CHARS_DEFAULT = 12000
_MARKDOWN_PROMPT_MAX_CHARS_DEFAULT = 16000
_MARKDOWN_BM25_TOP_K_DEFAULT = 5
_DEFAULT_MARKDOWN_MEMORY_DIR = Path(__file__).resolve().parents[2] / "tmp" / "bilingual_markdown_memory"
_SECTION_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)
_LOOKUP_STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "also",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "could",
    "during",
    "each",
    "from",
    "have",
    "into",
    "just",
    "more",
    "most",
    "only",
    "other",
    "over",
    "same",
    "should",
    "some",
    "such",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "until",
    "very",
    "were",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
}
_TERM_MAPPING_KEYWORDS_RE = re.compile(
    r"\b("
    r"glossary|terminology|term\s+mapping|source[- ]target|source\s+term|target\s+term|"
    r"preferred\s+target|preferred\s+translation|do[- ]not[- ]use|blocked\s+target|"
    r"translate(?:d)?\s+(?:as|to)|render(?:ed)?\s+(?:as|to)"
    r")\b",
    re.IGNORECASE,
)
_KO_TERM_MAPPING_RE = re.compile(
    r"(?:로|으로|라고)\s*(?:번역|표기|렌더링)|번역(?:어|명|표현)\s*(?:결정|매핑|고정|유지)"
)
_ARROW_OR_EQUALS_RE = re.compile(r"\s(?:->|=>|=)\s")
_MAPPING_COLON_EXCLUDED_PREFIXES = (
    "section",
    "clause",
    "chapter",
    "part",
    "annex",
    "table",
    "figure",
    "fig.",
    "wave",
    "scope",
    "page",
    "pp.",
)
_CANONICAL_MEMORY_LABELS = {
    "source flow": "Source flow",
    "source summary": "Source flow",
    "source-side summary": "Source flow",
    "section scope": "Source flow",
    "narrative arc & flow": "Source flow",
    "narrative arc and flow": "Source flow",
    "narrative flow": "Source flow",
    "target flow": "Target register notes",
    "target summary": "Target register notes",
    "target register notes": "Target register notes",
    "target-side register notes": "Target register notes",
    "style continuity": "Style continuity",
    "style & tone continuity": "Style continuity",
    "style and tone continuity": "Style continuity",
    "style notes": "Style continuity",
    "voice": "Style continuity",
    "tone": "Style continuity",
    "discourse state": "Discourse state",
    "current moment": "Discourse state",
    "active dynamics": "Discourse state",
    "unresolved tensions": "Discourse state",
    "open references": "Open references",
    "open references for continuity": "Open references",
    "memory reason": "Memory reason",
    "store reason": "Memory reason",
}
_MARKDOWN_MEMORY_LABEL_RE = re.compile(r"^-\s+(?:\*\*)?([^:*]+?)(?:\*\*)?\s*:\s*(.*)$")


def bilingual_summary_memory_enabled(style_options: dict[str, Any] | None = None) -> bool:
    if isinstance(style_options, dict) and "bilingual_summary_memory" in style_options:
        return bool(style_options.get("bilingual_summary_memory"))
    return os.getenv("AI_TRANSLATION_BILINGUAL_SUMMARY_MEMORY_ENABLED", "0").strip() != "0"


def source_word_count(texts: Iterable[str]) -> int:
    return sum(len(_WORD_RE.findall(str(text or ""))) for text in texts)


def _optional_threshold(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _ratio(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(0.0, value)


def _estimate_tokens(text: str) -> int:
    # Cheap tokenizer-free estimate. This is used only for relative prompt-budget control.
    return max(0, int(math.ceil(len(str(text or "")) / 4)))


def _estimate_tokens_from_chars(char_count: int) -> int:
    return max(0, int(math.ceil(max(0, int(char_count or 0)) / 4)))


def _clamp_int(value: float, lower: int, upper: int) -> int:
    if upper <= 0:
        return 0
    lower = min(max(0, lower), upper)
    return min(max(int(math.ceil(value)), lower), upper)


def _dynamic_memory_budget_settings(metrics: dict[str, int]) -> dict[str, Any]:
    document_estimated_tokens = _estimate_tokens_from_chars(int(metrics.get("total_chars") or 0))
    model_context_tokens = _optional_threshold(_MODEL_CONTEXT_TOKENS_ENV) or _MODEL_CONTEXT_TOKENS_DEFAULT
    memory_context_ratio = _ratio(_MEMORY_CONTEXT_RATIO_ENV, _MEMORY_CONTEXT_RATIO_DEFAULT)
    document_scale_ratio = _ratio(_DOCUMENT_SCALE_RATIO_ENV, _DOCUMENT_SCALE_RATIO_DEFAULT)
    summary_ratio = _ratio(_SUMMARY_TARGET_RATIO_ENV, _SUMMARY_TARGET_RATIO_DEFAULT)
    prompt_budget_max = int(math.ceil(model_context_tokens * memory_context_ratio))
    summary_target_max = int(math.ceil(prompt_budget_max * summary_ratio))
    prompt_budget = _optional_threshold(_PROMPT_BUDGET_TOKENS_ENV)
    prompt_budget_source = "env" if prompt_budget is not None else "dynamic"
    if prompt_budget is None:
        prompt_budget = _clamp_int(
            document_estimated_tokens * document_scale_ratio,
            _PROMPT_BUDGET_MIN_TOKENS,
            prompt_budget_max,
        )
    summary_target = _optional_threshold(_SUMMARY_TARGET_TOKENS_ENV)
    summary_target_source = "env" if summary_target is not None else "dynamic"
    if summary_target is None:
        summary_target = _clamp_int(
            prompt_budget * summary_ratio,
            _SUMMARY_TARGET_MIN_TOKENS,
            summary_target_max,
        )
    return {
        "document_estimated_tokens": document_estimated_tokens,
        "prompt_memory_budget_tokens": prompt_budget,
        "summary_target_tokens": summary_target,
        "model_context_tokens": model_context_tokens,
        "memory_context_ratio": memory_context_ratio,
        "document_scale_ratio": document_scale_ratio,
        "summary_target_ratio": summary_ratio,
        "prompt_memory_budget_source": prompt_budget_source,
        "summary_target_source": summary_target_source,
        "prompt_memory_budget_min_tokens": _PROMPT_BUDGET_MIN_TOKENS,
        "prompt_memory_budget_max_tokens": prompt_budget_max,
        "summary_target_min_tokens": _SUMMARY_TARGET_MIN_TOKENS,
        "summary_target_max_tokens": summary_target_max,
    }


def bilingual_summary_memory_compression_enabled(style_options: dict[str, Any] | None = None) -> bool:
    if isinstance(style_options, dict) and "bilingual_summary_memory_compression" in style_options:
        return bool(style_options.get("bilingual_summary_memory_compression"))
    return os.getenv(_COMPRESSION_ENABLED_ENV, "0").strip() != "0"


def bilingual_summary_memory_mode(style_options: dict[str, Any] | None = None) -> str:
    value = ""
    if isinstance(style_options, dict):
        value = str(style_options.get("bilingual_summary_memory_mode") or "").strip().lower()
    if not value:
        value = os.getenv(_MEMORY_MODE_ENV, _INLINE_SUMMARY_MODE).strip().lower()
    if value in {"external", "markdown", _EXTERNAL_MARKDOWN_MODE}:
        return _EXTERNAL_MARKDOWN_MODE
    return _INLINE_SUMMARY_MODE


def bilingual_summary_prompt_policy(style_options: dict[str, Any] | None = None) -> str:
    value = ""
    if isinstance(style_options, dict):
        value = str(
            style_options.get("bilingual_summary_memory_prompt_policy")
            or style_options.get("bilingual_summary_prompt_policy")
            or ""
        ).strip().lower()
    if not value:
        value = os.getenv(_PROMPT_INJECTION_POLICY_ENV, _PROMPT_POLICY_AUTO).strip().lower()
    if value in {"0", "false", "no", "none", "disabled", "disable", "off"}:
        return _PROMPT_POLICY_OFF
    if value in {"selective", "relevant", "retrieval"}:
        return _PROMPT_POLICY_SELECTIVE
    if value in {"1", "true", "yes", "all", "always", "force", "on"}:
        return _PROMPT_POLICY_ALWAYS
    return _PROMPT_POLICY_AUTO


def _document_profile_from_style_options(style_options: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(style_options, dict):
        return {}
    analysis = style_options.get("_pre_translation_analysis")
    if isinstance(analysis, dict):
        profile = analysis.get("document_profile")
        if isinstance(profile, dict) and profile:
            return dict(profile)
        if any(key in analysis for key in ("document_type", "domain", "content_structure", "purpose")):
            return {
                key: analysis.get(key)
                for key in ("document_type", "domain", "workflow", "product_system", "purpose", "audience", "content_structure")
                if analysis.get(key)
            }
    source_profile = style_options.get("_source_document_profile")
    return dict(source_profile) if isinstance(source_profile, dict) else {}


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
    budget_settings = _dynamic_memory_budget_settings(metrics)
    document_profile = _document_profile_from_style_options(style_options)
    memory = {
        "schema_version": _SCHEMA_VERSION,
        "job_id": job_id,
        "_artifact_label": str((style_options or {}).get("_filename") or (style_options or {}).get("_file_name") or ""),
        "markdown_date_prefix": _memory_date_prefix(),
        "markdown_folder_name": "",
        "target_lang": target_lang,
        "doc_format": doc_format,
        "document_profile": document_profile,
        "continuity_memory_decision": {},
        "enabled": enabled,
        "memory_mode": bilingual_summary_memory_mode(style_options),
        "compression_enabled": bilingual_summary_memory_compression_enabled(style_options),
        "prompt_injection_policy": bilingual_summary_prompt_policy(style_options),
        "prompt_injection_last_decision": "",
        "prompt_injection_last_reason": "",
        "prompt_injection_last_relevance_score": 0,
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
        **budget_settings,
        "prompt_memory_estimated_tokens": 0,
        "prompt_memory_last_budget_tokens": 0,
        "summary_update_call_count": 0,
        "summary_update_skip_count": 0,
        "summary_update_llm_call_count": 0,
        "summary_update_total_elapsed_ms": 0,
        "summary_update_llm_elapsed_ms": 0,
        "summary_update_last_elapsed_ms": 0,
        "summary_update_last_llm_elapsed_ms": 0,
        "summary_update_last_scope": "",
        "summary_update_last_status": "initialized",
        "markdown_memory_path": "",
        "markdown_manifest_path": "",
        "markdown_total_char_count": 0,
        "markdown_section_count": 0,
        "markdown_last_action": "",
        "markdown_last_action_reason": "",
        "updated_at": time.time(),
    }
    if enabled and memory["memory_mode"] == _EXTERNAL_MARKDOWN_MODE:
        _ensure_external_markdown_memory_files(memory)
    if enabled:
        log_info(
            "[Bilingual Summary Memory] enabled "
            f"words={metrics['source_word_count']} chars={metrics['total_chars']} "
            f"units={metrics['translation_unit_count']} scopes={metrics['scope_count']} "
            f"mode={memory['memory_mode']} "
            f"compression_enabled={memory['compression_enabled']} "
            f"document_tokens={memory['document_estimated_tokens']} "
            f"model_context={memory['model_context_tokens']} "
            f"memory_context_ratio={memory['memory_context_ratio']} "
            f"document_scale_ratio={memory['document_scale_ratio']} "
            f"prompt_budget={memory['prompt_memory_budget_tokens']}/{memory['prompt_memory_budget_max_tokens']}"
            f"({memory['prompt_memory_budget_source']}) "
            f"summary_target={memory['summary_target_tokens']}({memory['summary_target_source']})"
        )
    else:
        log_info(
            "[Bilingual Summary Memory] skipped "
            f"words={metrics['source_word_count']} chars={metrics['total_chars']} "
            f"units={metrics['translation_unit_count']} scopes={metrics['scope_count']}"
        )
    return memory


def _memory_decision_unit_sample(translation_units: list[Any], *, max_items: int = 28) -> list[dict[str, Any]]:
    units = [
        unit
        for unit in translation_units
        if str(getattr(unit, "text", "") or "").strip()
    ]
    if not units:
        return []
    if len(units) <= max_items:
        selected = list(enumerate(units))
    else:
        raw_positions = {
            0,
            len(units) - 1,
            len(units) // 4,
            len(units) // 2,
            (len(units) * 3) // 4,
        }
        step = max(1, len(units) // max(1, max_items - len(raw_positions)))
        raw_positions.update(range(0, len(units), step))
        selected = [(index, units[index]) for index in sorted(raw_positions)[:max_items]]
    sample: list[dict[str, Any]] = []
    for index, unit in selected:
        text = str(getattr(unit, "text", "") or "").strip()
        sample.append(
            {
                "index": index,
                "scope": str(getattr(unit, "context_scope", "") or ""),
                "element_type": str(getattr(unit, "element_type", "") or ""),
                "text": text[:900],
            }
        )
    return sample


def _explicit_prompt_policy(style_options: dict[str, Any] | None) -> str:
    if not isinstance(style_options, dict):
        return ""
    if (
        "bilingual_summary_memory_prompt_policy" not in style_options
        and "bilingual_summary_prompt_policy" not in style_options
    ):
        return ""
    return bilingual_summary_prompt_policy(style_options)


def _memory_policy_metrics(memory: dict[str, Any]) -> dict[str, Any]:
    source_word_count_value = int(memory.get("source_word_count") or 0)
    scope_count_value = int(memory.get("scope_count") or 0)
    total_chars_value = int(memory.get("total_chars") or 0)
    document_estimated_tokens_value = int(
        memory.get("document_estimated_tokens") or _estimate_tokens_from_chars(total_chars_value)
    )
    if source_word_count_value >= 80000 or scope_count_value >= 160:
        document_scale = "very_long"
    elif source_word_count_value >= 30000 or scope_count_value >= 60:
        document_scale = "long"
    elif source_word_count_value >= 8000 or scope_count_value >= 20:
        document_scale = "medium"
    else:
        document_scale = "short"
    return {
        "source_word_count": source_word_count_value,
        "total_chars": total_chars_value,
        "document_estimated_tokens": document_estimated_tokens_value,
        "scope_count": scope_count_value,
        "translation_unit_count": int(memory.get("translation_unit_count") or 0),
        "document_scale": document_scale,
        "length_signal": (
            "Use document length only as a supporting signal. A long document still needs memory only "
            "when earlier scopes are likely to affect later scopes."
        ),
    }


async def decide_bilingual_summary_memory_policy(
    sem: Any,
    session: Any,
    memory: dict[str, Any] | None,
    *,
    translation_units: list[Any],
    target_lang: str,
    style_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Let the LLM decide whether long-document memory should be used."""

    if not bilingual_summary_memory_is_enabled(memory):
        return memory
    explicit_policy = _explicit_prompt_policy(style_options)
    if explicit_policy and explicit_policy != _PROMPT_POLICY_AUTO:
        memory["prompt_injection_policy"] = explicit_policy
        memory["continuity_memory_decision"] = {
            "memory_policy": explicit_policy,
            "decision_source": "user_or_request_option",
            "reason": "Explicit prompt policy option was provided.",
        }
        if explicit_policy == _PROMPT_POLICY_OFF:
            memory["enabled"] = False
        return memory

    sample = _memory_decision_unit_sample(translation_units)
    prompt = render_prompt(
        "bilingual_summary_memory_policy.jinja",
        target_lang=target_lang,
        doc_format=memory.get("doc_format") or "",
        metrics=_memory_policy_metrics(memory),
        document_profile=memory.get("document_profile") or {},
        pre_translation_analysis=(style_options or {}).get("_pre_translation_analysis") or {},
        style_options={
            key: (style_options or {}).get(key)
            for key in ("purpose", "formality", "terminology", "tone")
            if isinstance(style_options, dict) and (style_options or {}).get(key) is not None
        },
        source_sample=sample,
    )
    started_at = time.perf_counter()
    parsed: dict[str, Any] = {}
    try:
        raw = await llm_call_async(sem, session, "", prompt)
        parsed = _json_object_from_text(raw)
    except Exception as exc:
        log_info(f"[Bilingual Summary Memory] LLM policy decision failed: {exc}")
    policy = str(parsed.get("memory_policy") or "").strip().lower()
    if policy not in {_PROMPT_POLICY_OFF, _PROMPT_POLICY_SELECTIVE, _PROMPT_POLICY_ALWAYS}:
        policy = _PROMPT_POLICY_SELECTIVE
        parsed = {
            **parsed,
            "memory_policy": policy,
            "decision_source": "fallback_after_invalid_or_missing_llm_decision",
            "reason": parsed.get("reason") or "LLM did not return a valid policy; using selective retrieval as bounded fallback.",
        }
    decision = {
        "memory_policy": policy,
        "reason": str(parsed.get("reason") or ""),
        "expected_benefit": str(parsed.get("expected_benefit") or ""),
        "risk": str(parsed.get("risk") or ""),
        "selection_guidance": str(parsed.get("selection_guidance") or ""),
        "decision_source": str(parsed.get("decision_source") or "llm_context_decision"),
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
    }
    memory["continuity_memory_decision"] = decision
    memory["prompt_injection_policy"] = policy
    if policy == _PROMPT_POLICY_OFF:
        memory["enabled"] = False
    log_info(
        "[Bilingual Summary Memory] LLM policy decision "
        f"policy={policy} enabled={memory.get('enabled')} "
        f"reason={decision.get('reason')} elapsed_ms={decision.get('elapsed_ms')}"
    )
    return memory


def bilingual_summary_memory_is_enabled(memory: dict[str, Any] | None) -> bool:
    return isinstance(memory, dict) and bool(memory.get("enabled"))


def get_prompt_bilingual_summary(
    memory: dict[str, Any] | None,
    *,
    lookup_texts: Iterable[str] | None = None,
    current_scope: str = "",
) -> dict[str, Any]:
    if not bilingual_summary_memory_is_enabled(memory):
        return {}
    lookup_items = list(lookup_texts or [])
    if memory.get("memory_mode") == _EXTERNAL_MARKDOWN_MODE:
        return _external_markdown_prompt_memory(
            memory,
            lookup_texts=lookup_items,
            current_scope=current_scope,
        )
    raw_scopes = [
        item
        for item in (memory.get("raw_scopes") or [])
        if isinstance(item, dict) and item.get("items")
    ]
    latest_summary = memory.get("summary")
    compression_enabled = bool(memory.get("compression_enabled"))
    pending_raw_scopes = [
        item
        for item in _pending_scope_entries(memory)
        if isinstance(item, dict) and item.get("items")
    ]
    prompt_raw_scopes = pending_raw_scopes if compression_enabled else raw_scopes
    has_summary = _summary_has_content(latest_summary)
    if not compression_enabled and not prompt_raw_scopes:
        return {}
    if compression_enabled and not has_summary and not prompt_raw_scopes:
        return {}
    prompt_memory = {
        "compression_enabled": compression_enabled,
        "scope_count": len(memory.get("scope_summaries") or []),
        "raw_scope_count": int(memory.get("raw_scope_count") or len(raw_scopes)),
        "raw_memory_word_count": int(memory.get("raw_memory_word_count") or 0),
        "raw_memory_char_count": int(memory.get("raw_memory_char_count") or 0),
        "prompt_memory_budget_tokens": int(memory.get("prompt_memory_budget_tokens") or 0),
        "prompt_memory_estimated_tokens": int(memory.get("prompt_memory_estimated_tokens") or 0),
    }
    if compression_enabled:
        prompt_memory.update({"summary": latest_summary if has_summary else {}, "raw_scopes": prompt_raw_scopes})
    else:
        prompt_memory.update({"raw_scopes": prompt_raw_scopes})
    selected_text = json.dumps(
        {
            "summary": prompt_memory.get("summary") or {},
            "raw_scopes": prompt_memory.get("raw_scopes") or [],
        },
        ensure_ascii=False,
    )
    return _with_prompt_injection_decision(
        memory,
        prompt_memory,
        selected_text=selected_text,
        lookup_texts=lookup_items,
        current_scope=current_scope,
    )


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _recent_raw_scope_limit() -> int:
    return _int_env(_RECENT_RAW_SCOPE_LIMIT_ENV, 0)


def _markdown_selective_store_enabled() -> bool:
    return os.getenv(_MARKDOWN_SELECTIVE_STORE_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _safe_markdown_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _external_markdown_base_dir() -> Path:
    value = os.getenv(_MARKDOWN_MEMORY_DIR_ENV, "").strip()
    return Path(value) if value else _DEFAULT_MARKDOWN_MEMORY_DIR


def _memory_date_prefix() -> str:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%y%m%d")
    except Exception:
        return datetime.now().strftime("%y%m%d")


def _external_markdown_folder_name(memory: dict[str, Any]) -> str:
    existing = str(memory.get("markdown_folder_name") or "").strip()
    if existing:
        return existing
    date_prefix = _safe_filename_part(str(memory.get("markdown_date_prefix") or "")) or _memory_date_prefix()
    artifact = _safe_filename_part(str(memory.get("_artifact_label") or memory.get("artifact_label") or ""))
    job = _safe_filename_part(str(memory.get("job_id") or "unknown-job")) or "unknown-job"
    parts = [date_prefix]
    if artifact:
        parts.append(artifact)
    parts.append(job)
    folder_name = "__".join(parts)
    memory["markdown_date_prefix"] = date_prefix
    memory["markdown_folder_name"] = folder_name
    return folder_name


def _external_markdown_paths(memory: dict[str, Any]) -> tuple[Path, Path]:
    job_id = str(memory.get("job_id") or "unknown-job")
    artifact = str(memory.get("_artifact_label") or memory.get("artifact_label") or "")
    base = job_artifact_dir(job_id, artifact)
    memory["markdown_folder_name"] = base.name
    return base / "continuity_memory.md", base / "continuity_memory.json"


def _initial_external_markdown(memory: dict[str, Any]) -> str:
    target_lang = str(memory.get("target_lang") or "").strip()
    doc_format = str(memory.get("doc_format") or "").strip()
    lines = [
        "# Document Continuity Memory",
        "",
        f"- Target language: {target_lang}" if target_lang else "",
        f"- Document format: {doc_format}" if doc_format else "",
        "- Purpose: section flow, discourse continuity, register, and style continuity only.",
        "- Not a glossary: terminology decisions belong to Document Term Memory.",
        "",
    ]
    return "\n".join(line for line in lines if line != "").strip() + "\n"


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _markdown_sections(markdown: str) -> list[dict[str, Any]]:
    matches = list(_SECTION_HEADING_RE.finditer(markdown))
    sections: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        level = len(match.group(1))
        title = match.group(2).strip()
        body = markdown[body_start:body_end].strip()
        sections.append(
            {
                "level": level,
                "title": title,
                "start": match.start(),
                "end": body_end,
                "body": body,
                "text": markdown[match.start():body_end].strip(),
            }
        )
    return sections


def _build_external_manifest(memory: dict[str, Any], markdown: str) -> dict[str, Any]:
    sections = _markdown_sections(markdown)
    return {
        "schema_version": _SCHEMA_VERSION,
        "memory_mode": _EXTERNAL_MARKDOWN_MODE,
        "job_id": memory.get("job_id") or "",
        "artifact_label": memory.get("_artifact_label") or memory.get("artifact_label") or "",
        "markdown_date_prefix": memory.get("markdown_date_prefix") or "",
        "markdown_folder_name": memory.get("markdown_folder_name") or "",
        "target_lang": memory.get("target_lang") or "",
        "doc_format": memory.get("doc_format") or "",
        "markdown_memory_path": memory.get("markdown_memory_path") or "",
        "section_count": len(sections),
        "total_char_count": len(markdown),
        "sections": [
            {
                "title": section["title"],
                "char_count": len(section["text"]),
            }
            for section in sections
        ],
        "updated_at": time.time(),
    }


def _write_external_markdown_state(memory: dict[str, Any], markdown: str) -> dict[str, Any]:
    md_path, manifest_path = _external_markdown_paths(memory)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    memory["markdown_memory_path"] = str(md_path)
    memory["markdown_manifest_path"] = str(manifest_path)
    md_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    manifest = _build_external_manifest(memory, markdown)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    memory["markdown_total_char_count"] = manifest["total_char_count"]
    memory["markdown_section_count"] = manifest["section_count"]
    return manifest


def _ensure_external_markdown_memory_files(memory: dict[str, Any]) -> tuple[Path, Path, str, dict[str, Any]]:
    md_path, manifest_path = _external_markdown_paths(memory)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    memory["markdown_memory_path"] = str(md_path)
    memory["markdown_manifest_path"] = str(manifest_path)
    if md_path.exists():
        markdown = md_path.read_text(encoding="utf-8", errors="ignore")
    else:
        markdown = _initial_external_markdown(memory)
        md_path.write_text(markdown, encoding="utf-8")
    manifest = _load_json_file(manifest_path)
    if not manifest:
        manifest = _write_external_markdown_state(memory, markdown)
    else:
        memory["markdown_total_char_count"] = int(manifest.get("total_char_count") or len(markdown))
        memory["markdown_section_count"] = int(manifest.get("section_count") or 0)
    return md_path, manifest_path, markdown, manifest


def _normalized_lookup(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _significant_lookup_terms(lookup_texts: Iterable[str] | None, *, limit: int = 80) -> list[str]:
    counts: dict[str, int] = {}
    for text in lookup_texts or []:
        for raw_token in _WORD_RE.findall(str(text or "")):
            token = raw_token.lower().strip("-'")
            if len(token) < 4:
                continue
            if token in _LOOKUP_STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
    return [
        token
        for token, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _prompt_memory_relevance_score(
    selected_text: str,
    *,
    lookup_texts: Iterable[str] | None,
    current_scope: str = "",
) -> tuple[int, list[str]]:
    memory_text = str(selected_text or "").lower()
    if not memory_text:
        return 0, []
    matched_terms: list[str] = []
    for term in _significant_lookup_terms(lookup_texts):
        if term in memory_text:
            matched_terms.append(term)
    score = len(matched_terms)
    scope = _normalized_lookup(current_scope)
    if scope and scope in _normalized_lookup(selected_text):
        score += 3
        matched_terms.append(f"scope:{scope}")
    return score, matched_terms[:20]


def _with_prompt_injection_decision(
    memory: dict[str, Any],
    prompt_memory: dict[str, Any],
    *,
    selected_text: str,
    lookup_texts: Iterable[str] | None,
    current_scope: str = "",
) -> dict[str, Any]:
    policy = str(memory.get("prompt_injection_policy") or _PROMPT_POLICY_AUTO).strip().lower()
    if policy not in {_PROMPT_POLICY_AUTO, _PROMPT_POLICY_SELECTIVE, _PROMPT_POLICY_ALWAYS, _PROMPT_POLICY_OFF}:
        policy = _PROMPT_POLICY_SELECTIVE

    score, matched_terms = _prompt_memory_relevance_score(
        selected_text,
        lookup_texts=lookup_texts,
        current_scope=current_scope,
    )

    decision = "included"
    reason = f"policy={policy}; score={score}"
    if policy == _PROMPT_POLICY_OFF:
        decision = "skipped"
        reason = f"{reason}; prompt injection disabled"
    elif policy == _PROMPT_POLICY_AUTO:
        decision = "included"
        reason = f"{reason}; auto unresolved, using selected memory candidates"

    memory["prompt_injection_last_decision"] = decision
    memory["prompt_injection_last_reason"] = reason
    memory["prompt_injection_last_relevance_score"] = score
    memory["prompt_injection_last_matched_terms"] = matched_terms

    if decision == "skipped":
        log_info(f"[Bilingual Summary Memory] prompt skipped {reason}")
        return {}

    output = dict(prompt_memory)
    output["prompt_injection_policy"] = policy
    output["prompt_injection_decision"] = decision
    output["prompt_injection_reason"] = reason
    output["prompt_injection_relevance_score"] = score
    output["prompt_injection_matched_terms"] = matched_terms
    output["continuity_memory_decision"] = memory.get("continuity_memory_decision") or {}
    return output


def _select_markdown_sections(
    markdown: str,
    *,
    lookup_texts: Iterable[str] | None = None,
    current_scope: str = "",
    max_chars: int | None = None,
) -> str:
    max_chars = max_chars if max_chars is not None else _int_env(
        _MARKDOWN_PROMPT_MAX_CHARS_ENV,
        _MARKDOWN_PROMPT_MAX_CHARS_DEFAULT,
    )
    if max_chars <= 0:
        return _sanitize_markdown_memory_text(markdown).strip()
    sections = _markdown_sections(markdown)
    if not sections:
        return markdown[-max_chars:].strip()
    granular_sections = [section for section in sections if int(section.get("level") or 0) >= 3]
    # Wave-level (###) entries are the intended retrieval units when they exist.
    # The broader ## section can be large and repetitive, so use it only as a
    # fallback for older markdown that has not accumulated wave entries yet.
    retrieval_sections = granular_sections or sections
    lookup = _normalized_lookup("\n".join(str(item or "") for item in (lookup_texts or [])))
    scope = _normalized_lookup(current_scope)
    query = "\n".join(
        item
        for item in (
            current_scope,
            "\n".join(str(text or "") for text in (lookup_texts or [])),
        )
        if item
    )
    top_k = _int_env(_MARKDOWN_BM25_TOP_K_ENV, _MARKDOWN_BM25_TOP_K_DEFAULT)
    selected: list[str] = []
    selected_indices: set[int] = set()
    for index, section in enumerate(retrieval_sections):
        title = _normalized_lookup(section["title"])
        body = _normalized_lookup(section["body"])
        title_match = title and title in lookup
        scope_match = scope and scope in body
        if title_match or scope_match:
            selected_indices.add(index)
            selected.append(section["text"])

    if not selected:
        bm25_documents = [
            f"{section.get('title', '')}\n{section.get('body', '')}"
            for section in retrieval_sections
        ]
        for _, index in bm25_rank_documents(query, bm25_documents)[:top_k]:
            section = retrieval_sections[index]
            if index in selected_indices:
                continue
            selected_indices.add(index)
            selected.append(section["text"])

    if not selected:
        selected = [section["text"] for section in retrieval_sections[-3:]]
    output = "\n\n".join(selected).strip()
    if len(output) > max_chars:
        output = output[-max_chars:].strip()
    return _sanitize_markdown_memory_text(output).strip()


def _external_markdown_prompt_memory(
    memory: dict[str, Any],
    *,
    lookup_texts: Iterable[str] | None = None,
    current_scope: str = "",
) -> dict[str, Any]:
    _, _, markdown, manifest = _ensure_external_markdown_memory_files(memory)
    lookup_items = list(lookup_texts or [])
    selected_markdown = _select_markdown_sections(
        markdown,
        lookup_texts=lookup_items,
        current_scope=current_scope,
    )
    raw_scopes = [
        item
        for item in (memory.get("raw_scopes") or [])
        if isinstance(item, dict) and item.get("items")
    ]
    recent_limit = _recent_raw_scope_limit()
    recent_raw_scopes = raw_scopes[-recent_limit:] if recent_limit else []
    if not selected_markdown and not recent_raw_scopes:
        return {}
    prompt_memory = {
        "memory_mode": _EXTERNAL_MARKDOWN_MODE,
        "compression_enabled": True,
        "scope_count": int(manifest.get("section_count") or memory.get("markdown_section_count") or 0),
        "raw_scope_count": int(memory.get("raw_scope_count") or len(raw_scopes)),
        "raw_memory_word_count": int(memory.get("raw_memory_word_count") or 0),
        "raw_memory_char_count": int(memory.get("raw_memory_char_count") or 0),
        "prompt_memory_budget_tokens": int(memory.get("prompt_memory_budget_tokens") or 0),
        "prompt_memory_estimated_tokens": _estimate_tokens(selected_markdown),
        "markdown_memory": {
            "path": memory.get("markdown_memory_path") or "",
            "total_char_count": int(manifest.get("total_char_count") or len(markdown)),
            "section_count": int(manifest.get("section_count") or 0),
            "selected_markdown": selected_markdown,
        },
        "raw_scopes": recent_raw_scopes,
    }
    selected_text = "\n\n".join(
        text
        for text in (
            selected_markdown,
            json.dumps(recent_raw_scopes, ensure_ascii=False) if recent_raw_scopes else "",
        )
        if text
    )
    return _with_prompt_injection_decision(
        memory,
        prompt_memory,
        selected_text=selected_text,
        lookup_texts=lookup_items,
        current_scope=current_scope,
    )


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


def _discard_raw_scope(memory: dict[str, Any], raw_scope: dict[str, Any]) -> None:
    raw_scopes = memory.get("raw_scopes")
    if not isinstance(raw_scopes, list):
        memory["raw_scopes"] = []
        memory["raw_scope_count"] = 0
        memory["raw_memory_last_scope"] = ""
        return

    for index in range(len(raw_scopes) - 1, -1, -1):
        if raw_scopes[index] is raw_scope or raw_scopes[index] == raw_scope:
            raw_scopes.pop(index)
            break

    memory["raw_scope_count"] = len(raw_scopes)
    memory["raw_memory_word_count"] = max(
        0,
        int(memory.get("raw_memory_word_count") or 0) - int(raw_scope.get("word_count") or 0),
    )
    memory["raw_memory_char_count"] = max(
        0,
        int(memory.get("raw_memory_char_count") or 0) - int(raw_scope.get("char_count") or 0),
    )
    memory["raw_memory_last_scope"] = str(raw_scopes[-1].get("scope") or "") if raw_scopes else ""


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


def _summary_for_prompt(memory: dict[str, Any]) -> dict[str, Any]:
    summary = memory.get("summary")
    return summary if _summary_has_content(summary) else {}


def _prompt_memory_budget_tokens(memory: dict[str, Any]) -> int:
    override = _optional_threshold(_PROMPT_BUDGET_TOKENS_ENV)
    if override is not None:
        budget = override
        memory["prompt_memory_budget_source"] = "env"
    else:
        budget = int(memory.get("prompt_memory_budget_tokens") or 0)
        if budget <= 0:
            settings = _dynamic_memory_budget_settings(
                {"total_chars": int(memory.get("total_chars") or 0)}
            )
            memory.update(settings)
            budget = int(settings["prompt_memory_budget_tokens"])
    memory["prompt_memory_budget_tokens"] = budget
    memory["prompt_memory_last_budget_tokens"] = budget
    return budget


def _summary_target_tokens(memory: dict[str, Any]) -> int:
    override = _optional_threshold(_SUMMARY_TARGET_TOKENS_ENV)
    if override is not None:
        target = override
        memory["summary_target_source"] = "env"
    else:
        target = int(memory.get("summary_target_tokens") or 0)
        if target <= 0:
            settings = _dynamic_memory_budget_settings(
                {"total_chars": int(memory.get("total_chars") or 0)}
            )
            memory.update(settings)
            target = int(settings["summary_target_tokens"])
    memory["summary_target_tokens"] = target
    return target


def _prompt_memory_estimated_tokens(memory: dict[str, Any]) -> int:
    prompt_memory = {
        "summary": _summary_for_prompt(memory),
        "raw_scopes": _pending_scope_entries(memory),
    }
    estimated = _estimate_tokens(json.dumps(prompt_memory, ensure_ascii=False))
    memory["prompt_memory_estimated_tokens"] = estimated
    return estimated


def _normalize_summary_update(parsed: dict[str, Any], scope: str) -> dict[str, Any]:
    summary = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else parsed
    open_references = summary.get("open_references") if isinstance(summary, dict) else []
    if not isinstance(open_references, list):
        open_references = []
    scope_summary = parsed.get("scope_summary") if isinstance(parsed.get("scope_summary"), dict) else {}
    return {
        "summary": {
            "source_summary": _sanitize_memory_text(summary.get("source_summary")),
            "target_summary": _sanitize_memory_text(summary.get("target_summary"), max_lines=4),
            "style_continuity": _sanitize_memory_text(summary.get("style_continuity"), max_lines=4),
            "discourse_state": _sanitize_memory_text(summary.get("discourse_state"), max_lines=4),
            "open_references": _sanitize_memory_references(open_references),
        },
        "scope_summary": {
            "scope": scope,
            "source_summary": _sanitize_memory_text(scope_summary.get("source_summary")),
            "target_summary": _sanitize_memory_text(scope_summary.get("target_summary"), max_lines=4),
            "source_topics": [
                _sanitize_memory_text(item, max_lines=1)
                for item in (scope_summary.get("source_topics") or scope_summary.get("important_terms") or [])
                if _sanitize_memory_text(item, max_lines=1)
            ][:12],
            "style_notes": [
                _sanitize_memory_text(item, max_lines=1)
                for item in (scope_summary.get("style_notes") or [])
                if _sanitize_memory_text(item, max_lines=1)
            ][:8],
            "created_at": time.time(),
        },
    }


def _scope_action_payload(scope_payload: list[dict[str, str]], *, limit: int = 120) -> list[dict[str, str]]:
    if len(scope_payload) <= limit:
        return scope_payload
    head = scope_payload[: max(1, limit // 2)]
    tail = scope_payload[-max(1, limit // 2) :]
    return head + tail


def _raw_scope_metadata(raw_scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": raw_scope.get("scope"),
        "word_count": raw_scope.get("word_count"),
        "char_count": raw_scope.get("char_count"),
        "item_count": raw_scope.get("item_count"),
        "completed_at": raw_scope.get("completed_at"),
    }


def _is_mapping_colon_line(line: str) -> bool:
    stripped = str(line or "").strip().lstrip("-*").strip()
    if ":" not in stripped:
        return False
    left, right = stripped.split(":", 1)
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    lowered_left = left.lower()
    if lowered_left.startswith(_MAPPING_COLON_EXCLUDED_PREFIXES):
        return False
    if len(left) > 70 or len(right) > 120:
        return False
    left_words = _WORD_RE.findall(left)
    right_words = _WORD_RE.findall(right)
    if not left_words or len(left_words) > 8 or len(right_words) > 16:
        return False
    has_target_script = bool(re.search(r"[가-힣一-龥ぁ-ゟ゠-ヿ]", right))
    return has_target_script and not stripped.endswith(".")


def _is_glossary_like_memory_line(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if _TERM_MAPPING_KEYWORDS_RE.search(text) or _KO_TERM_MAPPING_RE.search(text):
        return True
    if _ARROW_OR_EQUALS_RE.search(text):
        return True
    if _is_mapping_colon_line(text):
        return True
    return any(
        phrase in lowered
        for phrase in (
            "must be translated",
            "should be translated",
            "keep the translation",
            "use the translation",
            "translate consistently as",
        )
    )


def _sanitize_memory_text(value: Any, *, max_lines: int = 8) -> str:
    text = _safe_markdown_text(value)
    if not text:
        return ""
    pieces = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    kept: list[str] = []
    for piece in pieces:
        piece = piece.strip(" \t\r\n-•")
        if not piece:
            continue
        if _is_glossary_like_memory_line(piece):
            continue
        kept.append(piece)
        if len(kept) >= max_lines:
            break
    return " ".join(kept).strip()


def _sanitize_memory_references(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        text = _sanitize_memory_text(item, max_lines=1)
        if text:
            refs.append(text)
        if len(refs) >= 8:
            break
    return refs


def _canonical_memory_label(label: str) -> str:
    normalized = re.sub(r"\s+", " ", str(label or "").strip().strip("*")).lower()
    return _CANONICAL_MEMORY_LABELS.get(normalized, "")


def _sanitize_markdown_memory_text(markdown: str) -> str:
    output: list[str] = []
    for raw_line in str(markdown or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            output.append(raw_line)
            continue
        label_match = _MARKDOWN_MEMORY_LABEL_RE.match(stripped)
        if label_match:
            canonical_label = _canonical_memory_label(label_match.group(1))
            if canonical_label:
                value = _sanitize_memory_text(label_match.group(2), max_lines=3)
                output.append(f"- {canonical_label}:{f' {value}' if value else ''}")
                continue
            continue
        if stripped.startswith("-") and _is_glossary_like_memory_line(stripped):
            continue
        output.append(raw_line)
    sanitized = "\n".join(output).strip()
    return sanitized + ("\n" if sanitized else "")


def _memory_entry_has_content(normalized: dict[str, Any]) -> bool:
    entry = normalized.get("memory_entry") if isinstance(normalized.get("memory_entry"), dict) else {}
    return any(
        str(entry.get(key) or "").strip()
        for key in ("source_flow", "target_register_notes", "style_continuity", "discourse_state")
    ) or bool(entry.get("open_references"))


def _normalize_markdown_action(
    parsed: dict[str, Any],
    scope: str,
    *,
    selective_store_enabled: bool = True,
) -> dict[str, Any]:
    action = str(parsed.get("action") or "append").strip().lower()
    if not selective_store_enabled:
        action = "append"
    if action not in {"skip", "append"}:
        action = "append"
    memory_entry = parsed.get("memory_entry")
    if isinstance(memory_entry, str):
        memory_entry = {"source_flow": memory_entry}
    if not isinstance(memory_entry, dict):
        memory_entry = {}
    open_references = memory_entry.get("open_references")
    if not isinstance(open_references, list):
        open_references = []
    target_register_notes = (
        memory_entry.get("target_register_notes")
        or memory_entry.get("target_style_notes")
        or memory_entry.get("target_flow")
        or memory_entry.get("target_summary")
        or ""
    )
    return {
        "action": action,
        "section_title": _sanitize_memory_text(parsed.get("section_title") or "Document Flow", max_lines=1)[:160]
        or "Document Flow",
        "reason": _sanitize_memory_text(parsed.get("reason"), max_lines=2)[:500],
        "memory_entry": {
            "source_flow": _sanitize_memory_text(memory_entry.get("source_flow") or memory_entry.get("source_summary")),
            "target_register_notes": _sanitize_memory_text(target_register_notes, max_lines=4),
            "style_continuity": _sanitize_memory_text(memory_entry.get("style_continuity"), max_lines=4),
            "discourse_state": _sanitize_memory_text(memory_entry.get("discourse_state"), max_lines=4),
            "open_references": _sanitize_memory_references(open_references),
        },
        "scope": scope,
    }


def _format_markdown_memory_entry(scope: str, normalized: dict[str, Any]) -> str:
    entry = normalized.get("memory_entry") if isinstance(normalized.get("memory_entry"), dict) else {}
    lines = [f"### {scope}", ""]
    field_labels = (
        ("source_flow", "Source flow"),
        ("target_register_notes", "Target register notes"),
        ("style_continuity", "Style continuity"),
        ("discourse_state", "Discourse state"),
    )
    for key, label in field_labels:
        value = _safe_markdown_text(entry.get(key))
        if value:
            lines.append(f"- {label}: {value}")
    references = entry.get("open_references")
    if isinstance(references, list) and references:
        refs = " / ".join(str(item).strip() for item in references if str(item).strip())
        if refs:
            lines.append(f"- Open references: {refs}")
    reason = _safe_markdown_text(normalized.get("reason"))
    if reason:
        lines.append(f"- Memory reason: {reason}")
    if len(lines) <= 2:
        lines.append("- Scope completed; no durable continuity notes were provided.")
    return "\n".join(lines).strip() + "\n"


def _append_markdown_section(markdown: str, title: str, entry: str) -> str:
    title = title.strip() or "Document Flow"
    sections = _markdown_sections(markdown)
    for section in sections:
        if section["title"].strip().lower() != title.lower():
            continue
        before = markdown[: section["end"]].rstrip()
        after = markdown[section["end"] :].lstrip()
        updated = before + "\n\n" + entry.strip() + "\n\n"
        if after:
            updated += after
        return updated.rstrip() + "\n"
    return markdown.rstrip() + f"\n\n## {title}\n\n{entry.strip()}\n"


def _replace_markdown_section(markdown: str, title: str, replacement_body: str) -> str:
    title = title.strip()
    replacement_body = replacement_body.strip()
    if not title or not replacement_body:
        return markdown
    sections = _markdown_sections(markdown)
    for section in sections:
        if section["title"].strip().lower() != title.lower():
            continue
        before = markdown[: section["start"]].rstrip()
        after = markdown[section["end"] :].lstrip()
        replacement = f"## {section['title']}\n\n{replacement_body}\n\n"
        return (before + "\n\n" + replacement + after).strip() + "\n"
    return markdown


def _section_body_for_title(markdown: str, title: str) -> str:
    for section in _markdown_sections(markdown):
        if section["title"].strip().lower() == title.strip().lower():
            return section["body"]
    return ""


async def _compress_external_markdown_memory(
    sem: Any,
    session: Any,
    memory: dict[str, Any],
    *,
    markdown: str,
    section_title: str = "",
    global_compress: bool = False,
) -> str:
    target = markdown if global_compress else _section_body_for_title(markdown, section_title)
    if not target.strip():
        return markdown
    prompt = render_prompt(
        "bilingual_markdown_memory_compress.jinja",
        target_lang=memory.get("target_lang") or "",
        doc_format=memory.get("doc_format") or "",
        section_title=section_title,
        global_compress=global_compress,
        summary_target_tokens=_summary_target_tokens(memory),
        markdown=target,
    )
    raw = await llm_call_async(sem, session, "", prompt)
    parsed = _json_object_from_text(raw)
    compressed = ""
    if parsed:
        compressed = str(parsed.get("markdown") or parsed.get("compressed_markdown") or "").strip()
    if not compressed:
        compressed = raw.strip()
    if not compressed:
        return markdown
    compressed = _sanitize_markdown_memory_text(compressed)
    if not compressed:
        return markdown
    if global_compress:
        if not compressed.lstrip().startswith("#"):
            compressed = "# Document Continuity Memory\n\n" + compressed
        return compressed.rstrip() + "\n"
    return _replace_markdown_section(markdown, section_title, compressed)


async def _generate_external_markdown_entry(
    sem: Any,
    session: Any,
    memory: dict[str, Any],
    *,
    scope: str,
    section_title: str,
    reason: str,
    existing_memory_excerpt: str,
    scope_payload: list[dict[str, str]],
    selective_store_enabled: bool,
) -> tuple[dict[str, Any], int]:
    prompt = render_prompt(
        "bilingual_markdown_memory_entry.jinja",
        target_lang=memory.get("target_lang") or "",
        doc_format=memory.get("doc_format") or "",
        scope=scope,
        section_title=section_title,
        reason=reason,
        existing_memory_excerpt=existing_memory_excerpt,
        scope_items=_scope_action_payload(scope_payload),
    )
    memory["summary_update_llm_call_count"] = int(memory.get("summary_update_llm_call_count") or 0) + 1
    llm_started_at = time.perf_counter()
    raw = await llm_call_async(sem, session, "", prompt)
    llm_elapsed_ms = int((time.perf_counter() - llm_started_at) * 1000)
    parsed = _json_object_from_text(raw)
    memory_entry = parsed.get("memory_entry") if isinstance(parsed, dict) else {}
    if not isinstance(memory_entry, dict) and isinstance(parsed, dict):
        memory_entry = parsed
    normalized = _normalize_markdown_action(
        {
            "action": "append",
            "section_title": section_title,
            "reason": reason,
            "memory_entry": memory_entry,
        },
        scope,
        selective_store_enabled=selective_store_enabled,
    )
    return normalized, llm_elapsed_ms


async def _update_external_markdown_memory(
    sem: Any,
    session: Any,
    memory: dict[str, Any],
    *,
    scope: str,
    scope_payload: list[dict[str, str]],
    raw_scope: dict[str, Any],
    total_started_at: float,
) -> dict[str, Any]:
    _, _, markdown, manifest = _ensure_external_markdown_memory_files(memory)
    lookup_texts = [item.get("source", "") for item in scope_payload if item.get("source")]
    existing_excerpt = _select_markdown_sections(markdown, lookup_texts=lookup_texts, current_scope=scope)
    selective_store_enabled = _markdown_selective_store_enabled()
    prompt = render_prompt(
        "bilingual_markdown_memory_action.jinja",
        target_lang=memory.get("target_lang") or "",
        doc_format=memory.get("doc_format") or "",
        scope=scope,
        raw_scope=_raw_scope_metadata(raw_scope),
        manifest=manifest,
        existing_memory_excerpt=existing_excerpt,
        scope_items=_scope_action_payload(scope_payload),
        memory_budget_tokens=_prompt_memory_budget_tokens(memory),
        summary_target_tokens=_summary_target_tokens(memory),
        selective_store_enabled=selective_store_enabled,
    )
    memory["summary_update_llm_call_count"] = int(memory.get("summary_update_llm_call_count") or 0) + 1
    llm_started_at = time.perf_counter()
    raw = await llm_call_async(sem, session, "", prompt)
    llm_elapsed_ms = int((time.perf_counter() - llm_started_at) * 1000)
    parsed = _json_object_from_text(raw)
    normalized = _normalize_markdown_action(
        parsed,
        scope,
        selective_store_enabled=selective_store_enabled,
    ) if parsed else {
        "action": "skip" if selective_store_enabled else "append",
        "section_title": "Document Flow",
        "reason": (
            "Action response could not be parsed; skipping to avoid storing low-confidence continuity memory."
            if selective_store_enabled
            else "Action response could not be parsed; storing a minimal scope-completed note because selective storage is disabled."
        ),
        "memory_entry": {},
        "scope": scope,
    }
    action = str(normalized.get("action") or "append")
    section_title = str(normalized.get("section_title") or "Document Flow").strip() or "Document Flow"
    if action == "append":
        normalized, entry_llm_elapsed_ms = await _generate_external_markdown_entry(
            sem,
            session,
            memory,
            scope=scope,
            section_title=section_title,
            reason=str(normalized.get("reason") or ""),
            existing_memory_excerpt=existing_excerpt,
            scope_payload=scope_payload,
            selective_store_enabled=selective_store_enabled,
        )
        llm_elapsed_ms += entry_llm_elapsed_ms
        section_title = str(normalized.get("section_title") or section_title).strip() or section_title
    if action == "append" and selective_store_enabled and not _memory_entry_has_content(normalized):
        normalized["action"] = "skip"
        normalized["reason"] = (
            normalized.get("reason")
            or "No non-terminology continuity content remained after memory validation."
        )
        action = "skip"
    status = action
    if action == "skip":
        _discard_raw_scope(memory, raw_scope)
        elapsed_ms = int((time.perf_counter() - total_started_at) * 1000)
        memory["summary_update_skip_count"] = int(memory.get("summary_update_skip_count") or 0) + 1
        memory["summary_update_last_elapsed_ms"] = elapsed_ms
        memory["summary_update_last_llm_elapsed_ms"] = llm_elapsed_ms
        memory["summary_update_total_elapsed_ms"] = int(memory.get("summary_update_total_elapsed_ms") or 0) + elapsed_ms
        memory["summary_update_llm_elapsed_ms"] = int(memory.get("summary_update_llm_elapsed_ms") or 0) + llm_elapsed_ms
        memory["summary_update_last_status"] = "skipped_by_llm"
        memory["markdown_last_action"] = "skip"
        memory["markdown_last_action_reason"] = normalized.get("reason") or ""
        memory["prompt_memory_estimated_tokens"] = _estimate_tokens(
            _select_markdown_sections(markdown, lookup_texts=lookup_texts, current_scope=scope)
        )
        memory["updated_at"] = time.time()
        log_info(
            "[Bilingual Summary Memory] external markdown skipped "
            f"scope={scope} action=skip status=skipped_by_llm "
            f"reason={memory.get('markdown_last_action_reason')} "
            f"markdown_chars={memory.get('markdown_total_char_count')} "
            f"sections={memory.get('markdown_section_count')} "
            f"elapsed_ms={elapsed_ms} llm_elapsed_ms={llm_elapsed_ms} "
            f"dump_path={memory.get('markdown_memory_path')}"
        )
        return memory
    entry = _format_markdown_memory_entry(scope, normalized)
    markdown = _append_markdown_section(markdown, section_title, entry)
    section_limit = _int_env(
        _MARKDOWN_MEMORY_SECTION_MAX_CHARS_ENV,
        _MARKDOWN_MEMORY_SECTION_MAX_CHARS_DEFAULT,
    )
    total_limit = _int_env(_MARKDOWN_MEMORY_MAX_CHARS_ENV, _MARKDOWN_MEMORY_MAX_CHARS_DEFAULT)
    section_body = _section_body_for_title(markdown, section_title)
    if section_limit and len(section_body) > section_limit:
        markdown = await _compress_external_markdown_memory(
            sem,
            session,
            memory,
            markdown=markdown,
            section_title=section_title,
        )
        status = "compressed_section_by_threshold"
    if total_limit and len(markdown) > total_limit:
        markdown = await _compress_external_markdown_memory(
            sem,
            session,
            memory,
            markdown=markdown,
            global_compress=True,
        )
        status = "compressed_global_by_threshold"
    _write_external_markdown_state(memory, markdown)
    elapsed_ms = int((time.perf_counter() - total_started_at) * 1000)
    memory["summary_update_last_elapsed_ms"] = elapsed_ms
    memory["summary_update_last_llm_elapsed_ms"] = llm_elapsed_ms
    memory["summary_update_total_elapsed_ms"] = int(memory.get("summary_update_total_elapsed_ms") or 0) + elapsed_ms
    memory["summary_update_llm_elapsed_ms"] = int(memory.get("summary_update_llm_elapsed_ms") or 0) + llm_elapsed_ms
    memory["summary_update_last_status"] = status
    memory["markdown_last_action"] = action
    memory["markdown_last_action_reason"] = normalized.get("reason") or ""
    memory["prompt_memory_estimated_tokens"] = _estimate_tokens(
        _select_markdown_sections(markdown, lookup_texts=lookup_texts, current_scope=scope)
    )
    memory["updated_at"] = time.time()
    log_info(
        "[Bilingual Summary Memory] external markdown updated "
        f"scope={scope} action={action} status={status} "
        f"section={section_title} markdown_chars={memory.get('markdown_total_char_count')} "
        f"sections={memory.get('markdown_section_count')} "
        f"elapsed_ms={elapsed_ms} llm_elapsed_ms={llm_elapsed_ms} "
        f"dump_path={memory.get('markdown_memory_path')}"
    )
    return memory


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
    if memory.get("memory_mode") == _EXTERNAL_MARKDOWN_MODE:
        return await _update_external_markdown_memory(
            sem,
            session,
            memory,
            scope=scope,
            scope_payload=scope_payload,
            raw_scope=raw_scope,
            total_started_at=total_started_at,
        )
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
    # Keep the memory prompt bounded by budget. We compress only when the
    # cumulative summary plus the recent raw buffer would exceed that budget.
    budget_tokens = _prompt_memory_budget_tokens(memory)
    estimated_tokens = _prompt_memory_estimated_tokens(memory)
    if estimated_tokens <= budget_tokens:
        elapsed_ms = int((time.perf_counter() - total_started_at) * 1000)
        memory["summary_update_skip_count"] = int(memory.get("summary_update_skip_count") or 0) + 1
        memory["summary_update_last_elapsed_ms"] = elapsed_ms
        memory["summary_update_last_llm_elapsed_ms"] = 0
        memory["summary_update_total_elapsed_ms"] = int(memory.get("summary_update_total_elapsed_ms") or 0) + elapsed_ms
        memory["summary_update_last_status"] = "skipped_prompt_budget_available"
        memory["updated_at"] = time.time()
        log_info(
            "[Bilingual Summary Memory] update skipped "
            f"scope={scope} reason=prompt_budget_available "
            f"prompt_tokens={estimated_tokens}/{budget_tokens} "
            f"pending_words={pending_words} pending_scopes={len(pending)} "
            f"elapsed_ms={elapsed_ms} "
            f"total_overhead_ms={memory.get('summary_update_total_elapsed_ms')}"
        )
        return memory
    pending_items = _pending_scope_items(pending)
    pending_scope = _pending_scope_label(pending, scope)
    prompt = render_prompt(
        "bilingual_summary_memory_update.jinja",
        target_lang=memory.get("target_lang") or "",
        existing_summary=_summary_for_prompt(memory),
        scope=pending_scope,
        scope_items=pending_items,
        memory_budget_tokens=budget_tokens,
        summary_target_tokens=_summary_target_tokens(memory),
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
    memory["prompt_memory_estimated_tokens"] = _prompt_memory_estimated_tokens(memory)
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
        f"prompt_tokens={estimated_tokens}/{budget_tokens} "
        f"pending_words={pending_words} "
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
    artifact = artifact_label or str(memory.get("_artifact_label") or "")
    path = job_artifact_path(job_id, artifact, "bilingual_summary_memory.json")
    path.write_text(json.dumps(memory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)
