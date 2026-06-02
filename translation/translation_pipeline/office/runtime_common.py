"""공통 Office 런타임 helper."""

from __future__ import annotations

from typing import Dict, List

from translation_pipeline.common.nodes import PREVIEW_HEIGHT, PREVIEW_WIDTH


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def _preview_bbox(x: float, y: float, w: float, h: float) -> List[int]:
    x0 = _clamp(x, 0, PREVIEW_WIDTH - 8)
    y0 = _clamp(y, 0, PREVIEW_HEIGHT - 8)
    x1 = _clamp(x0 + max(24, w), x0 + 8, PREVIEW_WIDTH)
    y1 = _clamp(y0 + max(18, h), y0 + 8, PREVIEW_HEIGHT)
    return [int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))]


def _element_type_with_placeholder(text: str, default: str) -> str:
    normalized = " ".join(str(text or "").strip().split()).lower()
    if normalized in {"blank", "n/a", "na"}:
        return "placeholder"
    return default


def _node_translation(node: dict, trans_map: Dict[str, str]) -> str:
    """노드에 적용할 번역 문자열을 구한다."""

    if node.get("translated_text") is not None:
        return str(node.get("translated_text", ""))
    original = str(node.get("text", ""))
    return str(trans_map.get(original, original))
