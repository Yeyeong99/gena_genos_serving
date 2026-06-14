"""LLM translation response validation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class TranslationValidationResult:
    """Normalized translations plus validation diagnostics."""

    normalized: dict[int, str] = field(default_factory=dict)
    hard_errors: list[str] = field(default_factory=list)
    soft_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard_errors


def _unwrap_items(parsed_items: Any) -> Any:
    if isinstance(parsed_items, dict):
        for key in ("items", "translations", "results", "data"):
            candidate = parsed_items.get(key)
            if isinstance(candidate, list):
                return candidate
    return parsed_items


def validate_translation_batch_response(
    parsed_items: Any,
    expected_sources: Mapping[int, str],
) -> TranslationValidationResult:
    """Validate a JSON-array translation response before injection.

    Hard errors mean the response cannot be trusted for positional injection:
    malformed shape, duplicate/unexpected ids, missing ids, or non-string text.
    Soft warnings are quality signals that may still be safe to inject.
    """

    result = TranslationValidationResult()
    expected_ids = set(expected_sources.keys())
    items = _unwrap_items(parsed_items)
    if not isinstance(items, list):
        result.hard_errors.append("response is not a JSON array")
        return result

    seen_ids: set[int] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            result.hard_errors.append(f"item[{index}] is not an object")
            continue
        if "id" not in item:
            result.hard_errors.append(f"item[{index}] missing id")
            continue
        try:
            item_id = int(item["id"])
        except (TypeError, ValueError):
            result.hard_errors.append(f"item[{index}] id is not an integer")
            continue
        if item_id in seen_ids:
            result.hard_errors.append(f"duplicate id {item_id}")
            continue
        seen_ids.add(item_id)
        if item_id not in expected_ids:
            result.hard_errors.append(f"unexpected id {item_id}")
            continue

        translated = (
            item.get("t")
            if item.get("t") is not None
            else item.get("translated")
            if item.get("translated") is not None
            else item.get("translation")
            if item.get("translation") is not None
            else item.get("text")
        )
        if not isinstance(translated, str):
            result.hard_errors.append(f"id {item_id} translation is not a string")
            continue
        result.normalized[item_id] = translated

        source = str(expected_sources.get(item_id, ""))
        if source.strip() and not translated.strip():
            result.soft_warnings.append(f"id {item_id} translated text is blank")
        elif source.strip() and translated.strip() == source.strip():
            result.soft_warnings.append(f"id {item_id} translated text equals source")

    missing_ids = expected_ids - set(result.normalized.keys())
    if missing_ids:
        preview = sorted(missing_ids)[:8]
        suffix = "..." if len(missing_ids) > len(preview) else ""
        result.hard_errors.append(f"missing ids {preview}{suffix}")

    return result
