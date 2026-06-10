"""Translation prompt builder.

Prompt wording lives in ``genos/translation/prompts``. This module only selects
rule data and renders the matching Jinja templates.
"""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from translation_pipeline.common.prompts import render_prompt


_RULES_DIR = Path(__file__).resolve().parent / "translation_rules"
_STYLE_GUIDE_DIR = _RULES_DIR / "style_guides"

_TARGET_TO_RULE_KEY = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "영어": "en",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    "한국어": "ko",
}

_OFFICE_PROMPT_PROFILES: dict[str, dict[str, str]] = {
    "pptx": {
        "role_name": "presentation translator",
        "source_label": "TARGET_TEXT",
        "context_label": "CONTEXT_TEXT",
        "items_label": "TARGET_TEXT_ITEMS",
    },
    "docx": {
        "role_name": "document translator",
        "source_label": "SOURCE_TEXT",
        "context_label": "ITEM_CONTEXT",
        "items_label": "ITEMS",
    },
    "xlsx": {
        "role_name": "spreadsheet translator",
        "source_label": "CELL_TEXT",
        "context_label": "CELL_CONTEXT",
        "items_label": "CELLS",
    },
}


def _prompt_log_mode() -> str:
    mode = os.getenv("AI_TRANSLATION_PROMPT_LOG_MODE", "").strip().lower()
    if mode in {"all", "glossary", "off"}:
        return mode
    explicit = os.getenv("AI_TRANSLATION_PROMPT_VERBOSE_LOG", "").strip().lower()
    if explicit:
        return "glossary" if explicit in {"1", "true", "yes", "on"} else "off"
    return "off"


def _has_prompt_memory(context: dict[str, Any]) -> bool:
    style_context = context.get("style_context")
    if not isinstance(style_context, dict):
        return False
    document_term_memory = style_context.get("document_term_memory")
    if isinstance(document_term_memory, dict):
        terms = document_term_memory.get("terms")
        return isinstance(terms, list) and bool(terms)
    return False


def _prompt_log_enabled(context: dict[str, Any]) -> bool:
    mode = _prompt_log_mode()
    if mode == "off":
        return False
    if mode == "all":
        return True
    return _has_prompt_memory(context)


def _prompt_log_limit() -> int:
    try:
        return max(0, int(os.getenv("AI_TRANSLATION_PROMPT_LOG_MAX_CHARS", "0")))
    except ValueError:
        return 0


def _render_prompt(template_name: str, log_name: str, **context: Any) -> str:
    rendered = render_prompt(template_name, **context)
    if _prompt_log_enabled(context):
        limit = _prompt_log_limit()
        output = rendered if limit == 0 else rendered[:limit]
        truncated = "" if limit == 0 or len(rendered) <= limit else f"\n[truncated at {limit} chars]"
        log_info(
            f"\n[translation prompt] {log_name} "
            f"(template={template_name}, chars={len(rendered)})\n"
            f"{output}{truncated}\n"
            f"[translation prompt] end {log_name}\n",
            flush=True,
        )
    return rendered


@lru_cache(maxsize=8)
def _load_style_guide(rule_key: str) -> dict[str, Any]:
    path = _STYLE_GUIDE_DIR / f"{rule_key}.json"
    if not rule_key or not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _select_style_options(
    style_options: dict[str, Any] | None,
    guide: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Use orchestration-provided style values and fill only missing defaults."""

    options = style_options if isinstance(style_options, dict) else {}
    defaults = (guide or {}).get("defaults") if isinstance(guide, dict) else {}
    if not isinstance(defaults, dict):
        defaults = {}

    return {
        "purpose": str(options.get("purpose") or defaults.get("purpose") or "default"),
        "formality": str(options.get("formality") or defaults.get("formality") or "formal"),
        "terminology": str(
            options.get("terminology")
            or defaults.get("terminology")
            or "preserve_key_terms"
        ),
    }


def _rule_applies(rule: dict[str, Any], selected: dict[str, str]) -> bool:
    selected_element_types = {
        item.strip()
        for item in str(selected.get("element_type") or "").split(",")
        if item.strip()
    }
    rule_element_type = str(rule.get("element_type") or "").strip()
    rule_element_types = rule.get("element_types")
    if rule_element_type and rule_element_type not in selected_element_types:
        return False
    if isinstance(rule_element_types, list):
        allowed = {str(item).strip() for item in rule_element_types if str(item).strip()}
        if allowed and not (allowed & selected_element_types):
            return False

    category = str(rule.get("category") or "").strip()
    if category == "base":
        return True
    value = rule.get("value")
    return category in selected and value is not None and str(value) == selected[category]


def _override_applies(rule: dict[str, Any], selected: dict[str, str]) -> bool:
    when = rule.get("when")
    if not isinstance(when, dict):
        return False
    return all(str(selected.get(str(key), "")) == str(value) for key, value in when.items())


def _sorted_rule_texts(rules: list[dict[str, Any]]) -> list[str]:
    sorted_rules = sorted(rules, key=lambda item: int(item.get("priority") or 0), reverse=True)
    return [str(rule.get("rule") or "").strip() for rule in sorted_rules if rule.get("rule")]


def _entry_pairs(entries: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or entry.get("term") or "").strip()
        target = str(entry.get("target") or entry.get("translation") or "").strip()
        if source and target:
            pair = {"source": source, "target": target}
            status = str(entry.get("status") or entry.get("lock_type") or "").strip()
            if status:
                pair["status"] = status
            pairs.append(pair)
    return pairs


def _do_not_use_pairs(entries: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or entry.get("term") or "").strip()
        targets = entry.get("targets")
        if not isinstance(targets, list):
            targets = entry.get("do_not_use") or entry.get("wrong_targets") or []
        if not isinstance(targets, list):
            targets = [entry.get("target") or entry.get("translation")]
        cleaned_targets = [str(item).strip() for item in targets if str(item).strip()]
        if source and cleaned_targets:
            pairs.append({"source": source, "targets": " / ".join(cleaned_targets)})
    return pairs


def _document_profile_context(style_options: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(style_options, dict):
        return {}
    profile = style_options.get("_source_document_profile")
    return profile if isinstance(profile, dict) else {}


def _pre_translation_analysis_context(style_options: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(style_options, dict):
        return {}
    analysis = style_options.get("_pre_translation_analysis")
    return analysis if isinstance(analysis, dict) else {}


def _document_term_memory_context(style_options: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(style_options, dict):
        return {}
    memory = style_options.get("_document_term_memory")
    return memory if isinstance(memory, dict) else {}


def _bilingual_summary_memory_context(style_options: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(style_options, dict):
        return {}
    memory = style_options.get("_bilingual_summary_memory")
    return memory if isinstance(memory, dict) else {}


def get_translation_style_context(
    target_lang: str,
    style_options: dict[str, Any] | None = None,
    *,
    doc_format: str = "",
    element_type: str = "",
    terminology_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    guide = _load_style_guide(_TARGET_TO_RULE_KEY.get((target_lang or "").strip().lower(), ""))
    selected = _select_style_options(style_options, guide if guide else None)
    if element_type:
        selected = {**selected, "element_type": element_type}
    rules = guide.get("rules") if isinstance(guide.get("rules"), list) else []
    overrides = guide.get("overrides") if isinstance(guide.get("overrides"), list) else []

    revision_instruction = ""
    do_not_use_terms: list[dict[str, Any]] | None = None
    if isinstance(style_options, dict):
        revision_instruction = str(style_options.get("_revision_instruction") or "").strip()
        for key in (
            "_do_not_use_terms",
            "_temporary_glossary_do_not_use",
            "_document_do_not_use_terms",
        ):
            maybe_terms = style_options.get(key)
            if isinstance(maybe_terms, list):
                do_not_use_terms = maybe_terms
                break

    return {
        "selected": selected,
        "doc_format": doc_format,
        "element_type": element_type,
        "rules": _sorted_rule_texts(
            [rule for rule in rules if isinstance(rule, dict) and _rule_applies(rule, selected)]
        ),
        "overrides": _sorted_rule_texts(
            [
                rule
                for rule in overrides
                if isinstance(rule, dict) and _override_applies(rule, selected)
            ]
        ),
        "terminology_entries": _entry_pairs(terminology_entries),
        "do_not_use_terms": _do_not_use_pairs(do_not_use_terms),
        "document_profile": _document_profile_context(style_options),
        "pre_translation_analysis": _pre_translation_analysis_context(style_options),
        "document_term_memory": _document_term_memory_context(style_options),
        "bilingual_summary_memory": _bilingual_summary_memory_context(style_options),
        "revision_instruction": revision_instruction,
    }


def get_translation_system_prompt(
    target_lang: str,
    style_options: dict[str, Any] | None = None,
) -> str:
    """Build a batch translation system prompt."""

    return _render_prompt(
        "office_translate_system.jinja",
        "batch.system",
        role_name="text translator",
        source_label="TEXT",
        context_label="CONTEXT",
        target_lang=target_lang,
        prompt_profile="plain",
        style_context=get_translation_style_context(target_lang, style_options),
    )


def build_batch_user_prompt(texts_with_ids: list[tuple[int, str]]) -> str:
    """Build a batch translation user prompt."""

    return _render_prompt(
        "office_translate_user.jinja",
        "batch.user",
        context_label="",
        context_text="",
        items_label="TEXT_ITEMS",
        items_json=json.dumps(
            [{"id": tid, "s": text} for tid, text in texts_with_ids],
            ensure_ascii=False,
        ),
    )


def get_single_translation_system_prompt(target_lang: str) -> str:
    """Build a single-text translation system prompt."""

    return _render_prompt(
        "office_translate_single_system.jinja",
        "single.system",
        target_lang=target_lang,
    )


def build_single_user_prompt(
    text: str,
    target_lang: str,
    style_options: dict[str, Any] | None = None,
    *,
    source_label: str = "SOURCE_TEXT",
    context_instruction: str = "",
    extra_instruction: str | None = None,
    context_label: str = "CONTEXT",
    context_text: str = "",
    previous_translation: str = "",
    doc_format: str = "",
    element_type: str = "",
) -> str:
    """Build a single-text translation user prompt."""

    return _render_prompt(
        "office_translate_single_user.jinja",
        f"single.user.{doc_format or 'plain'}",
        source_label=source_label,
        target_lang=target_lang,
        context_instruction=context_instruction,
        extra_instruction=extra_instruction or "",
        style_context=get_translation_style_context(
            target_lang,
            style_options,
            doc_format=doc_format,
            element_type=element_type,
        ),
        context_label=context_label,
        context_text=context_text,
        source_text=text,
        previous_translation=previous_translation,
    )


def build_office_context_system_prompt(
    doc_format: str,
    target_lang: str,
    style_options: dict[str, Any] | None = None,
    *,
    element_type: str = "",
) -> str:
    profile = _OFFICE_PROMPT_PROFILES[doc_format]
    return _render_prompt(
        "office_translate_system.jinja",
        f"office.{doc_format}.system",
        role_name=profile["role_name"],
        source_label=profile["source_label"],
        context_label=profile["context_label"],
        target_lang=target_lang,
        prompt_profile=doc_format,
        style_context=get_translation_style_context(
            target_lang,
            style_options,
            doc_format=doc_format,
            element_type=element_type,
        ),
    )


def build_office_context_user_prompt(
    doc_format: str,
    batch: list[Any] | None = None,
    *,
    context_text: str = "",
    target_items: list[tuple[int, str]] | None = None,
    previous_items: dict[int, str] | None = None,
) -> str:
    previous_items = previous_items or {}
    profile = _OFFICE_PROMPT_PROFILES[doc_format]

    if doc_format == "pptx":
        payload = []
        for item_id, text in target_items or []:
            item: dict[str, Any] = {"id": item_id, "s": text}
            previous = previous_items.get(item_id)
            if previous:
                item["previous_t"] = previous
            payload.append(item)
        return _render_prompt(
            "office_translate_user.jinja",
            "office.pptx.user",
            context_label=profile["context_label"],
            context_text=context_text,
            items_label=profile["items_label"],
            items_json=json.dumps(payload, ensure_ascii=False),
        )

    if doc_format == "docx":
        payload = [
            {
                "id": unit.translation_unit_id,
                "context": unit.context_text,
                "source": unit.text,
            }
            for unit in batch or []
        ]
        return _render_prompt(
            "office_translate_user.jinja",
            "office.docx.user",
            context_label="",
            context_text="",
            items_label=profile["items_label"],
            items_json=json.dumps(payload, ensure_ascii=False),
        )

    if doc_format == "xlsx":
        payload = [
            {
                "id": unit.translation_unit_id,
                "cell_context": unit.context_text,
                "cell_text": unit.text,
                **(
                    {"previous_t": previous_items[unit.translation_unit_id]}
                    if previous_items.get(unit.translation_unit_id)
                    else {}
                ),
            }
            for unit in batch or []
        ]
        return _render_prompt(
            "office_translate_user.jinja",
            "office.xlsx.user",
            context_label="",
            context_text="",
            items_label=profile["items_label"],
            items_json=json.dumps(payload, ensure_ascii=False),
        )

    raise ValueError(f"Unsupported office prompt format: {doc_format}")


def build_validation_retry_system_prompt(base_system_prompt: str) -> str:
    return _render_prompt(
        "office_translate_retry_system.jinja",
        "validation_retry.system",
        base_system_prompt=base_system_prompt,
    )
