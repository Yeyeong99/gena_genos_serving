"""파이프라인 공통 노드/레이아웃 유틸."""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional, Tuple


PREVIEW_WIDTH = 960
PREVIEW_HEIGHT = 620


def _normalize_text(value: Any) -> str:
    """입력값을 문자열로 정규화한다.

    Args:
        value: 정규화할 값.

    Returns:
        문자열 값.
    """

    if value is None:
        return ""
    return str(value)


def _safe_int(value: Any) -> Optional[int]:
    """입력값을 안전하게 정수로 변환한다.

    Args:
        value: 변환할 값.

    Returns:
        변환된 정수 또는 None.
    """

    try:
        if value is None:
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    """입력값을 안전하게 실수로 변환한다.

    Args:
        value: 변환할 값.

    Returns:
        변환된 실수 또는 None.
    """

    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """빈 값을 제외한 딕셔너리를 만든다.

    Args:
        data: 원본 딕셔너리.

    Returns:
        빈 값이 제거된 딕셔너리.
    """

    return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def assign_node_ids(nodes: List[dict]) -> List[dict]:
    """노드 목록에 순차 ID를 부여한다.

    Args:
        nodes: ID를 부여할 노드 목록.

    Returns:
        ID가 반영된 노드 목록.
    """

    for index, node in enumerate(nodes):
        node["node_id"] = index
    return nodes


def build_node_layout(node: dict) -> Dict[str, Any]:
    """프런트 표시용 문서 블록 레이아웃을 생성한다.

    Args:
        node: 레이아웃으로 변환할 노드.

    Returns:
        프런트용 문서 블록 딕셔너리.
    """

    base = {
        "id": node.get("node_id"),
        "type": node.get("type", ""),
        "source": node.get("source", ""),
        "group": node.get("group", ""),
        "order": node.get("node_id"),
        "original": _normalize_text(node.get("text", "")),
        "translated": _normalize_text(node.get("translated_text", node.get("text", ""))),
    }

    style = _compact_dict(
        {
            "font_size": _safe_float(node.get("font_size")),
            "bold": node.get("bold"),
            "italic": node.get("italic"),
            "underline": node.get("underline"),
            "font_name": _normalize_text(node.get("font_name", "")),
            "align": _normalize_text(node.get("align", "")),
            "fill": _normalize_text(node.get("fill", "")),
            "color": _normalize_text(node.get("color", "")),
        }
    )

    location = _compact_dict(
        {
            "page": _safe_int(node.get("page_num")),
            "translated_page": _safe_int(node.get("translated_page_num")),
            "slide": _safe_int(node.get("slide_index")),
            "sheet": _normalize_text(node.get("sheet_name", "")),
            "row": _safe_int(node.get("row")),
            "col": _safe_int(node.get("col")),
            "cell": _normalize_text(node.get("cell_ref", "")),
            "shape": _normalize_text(node.get("shape_name", "")),
            "bbox": node.get("bbox"),
            "original_bbox": node.get("original_bbox"),
            "translated_bbox": node.get("translated_bbox"),
        }
    )

    container = _compact_dict(
        {
            "section": _normalize_text(node.get("section", "")),
            "paragraph_style": _normalize_text(node.get("paragraph_style", "")),
            "chart_kind": _normalize_text(node.get("chart_kind", "")),
        }
    )

    if style:
        base["style"] = style
    if location:
        base["location"] = location
    if container:
        base["container"] = container
    return _compact_dict(base)


def build_document_layout(nodes: List[dict]) -> List[dict]:
    """노드 목록을 프런트용 문서 블록 목록으로 변환한다.

    Args:
        nodes: 변환할 노드 목록.

    Returns:
        문서 블록 목록.
    """

    return [build_node_layout(node) for node in nodes]


def build_translation_pairs(nodes: List[dict], trans_map: Dict[str, str]) -> List[dict]:
    """노드 목록과 번역 맵으로 원문/번역 쌍 목록을 만든다.

    Args:
        nodes: 번역 대상 노드 목록.
        trans_map: 원문/번역 매핑.

    Returns:
        원문/번역 쌍 목록.
    """

    pairs: List[dict] = []
    for node in nodes:
        original = _normalize_text(node.get("text", ""))
        translated = _normalize_text(trans_map.get(original, original))
        pairs.append(
            {
                "id": node["node_id"],
                "original": original,
                "translated": translated,
                "type": node.get("type", ""),
                "source": node.get("source", ""),
                "group": node.get("group", ""),
            }
        )
    return pairs


def build_edited_text_by_id(edited_pairs: List[dict]) -> Dict[int, str]:
    """사용자 수정 결과를 노드 ID 기준 맵으로 변환한다.

    Args:
        edited_pairs: 사용자 수정 결과 목록.

    Returns:
        노드 ID별 수정 텍스트 매핑.
    """

    edited_text_by_id: Dict[int, str] = {}
    for item in edited_pairs:
        if not isinstance(item, dict) or "id" not in item:
            continue
        try:
            node_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        if "translated" not in item:
            continue
        edited_text_by_id[node_id] = _normalize_text(item.get("translated", ""))
    return edited_text_by_id


def apply_node_translations(
    nodes: List[dict],
    trans_map: Optional[Dict[str, str]] = None,
    edited_text_by_id: Optional[Dict[int, str]] = None,
) -> None:
    """노드에 번역 결과 또는 사용자 수정 결과를 반영한다.

    Args:
        nodes: 갱신할 노드 목록.
        trans_map: 원문/번역 매핑.
        edited_text_by_id: 노드 ID별 수정 텍스트 매핑.

    Returns:
        없음.
    """

    trans_map = trans_map or {}
    edited_text_by_id = edited_text_by_id or {}

    for node in nodes:
        original = _normalize_text(node.get("text", ""))
        translated = edited_text_by_id.get(node["node_id"], trans_map.get(original, original))
        node["translated_text"] = _normalize_text(translated)


def build_download_payload(output_path: str) -> Dict[str, str]:
    """다운로드에 필요한 파일 메타데이터를 생성한다.

    Args:
        output_path: 저장된 파일 경로.

    Returns:
        파일 경로, 파일명, base64, mime type을 포함한 payload.
    """

    mime_type = mimetypes.guess_type(output_path)[0] or "application/octet-stream"
    with open(output_path, "rb") as file_handle:
        file_base64 = base64.b64encode(file_handle.read()).decode("utf-8")
    return {
        "file_path": output_path,
        "output_filename": os.path.basename(output_path),
        "file_base64": file_base64,
        "mime_type": mime_type,
    }


def _clamp(value: float, min_value: float, max_value: float) -> float:
    """값을 지정한 범위로 제한한다.

    Args:
        value: 원본 값.
        min_value: 최소값.
        max_value: 최대값.

    Returns:
        범위 내로 보정된 값.
    """

    return max(min_value, min(max_value, value))


def _preview_bbox(x: float, y: float, w: float, h: float) -> List[int]:
    """preview 좌표계 기준 bbox를 생성한다.

    Args:
        x: 시작 x 좌표.
        y: 시작 y 좌표.
        w: 너비.
        h: 높이.

    Returns:
        preview 기준 bbox.
    """

    x0 = _clamp(x, 0, PREVIEW_WIDTH - 8)
    y0 = _clamp(y, 0, PREVIEW_HEIGHT - 8)
    x1 = _clamp(x0 + max(24, w), x0 + 8, PREVIEW_WIDTH)
    y1 = _clamp(y0 + max(18, h), y0 + 8, PREVIEW_HEIGHT)
    return [int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))]


def _xlsx_cell_bbox(row: int, col: int) -> List[int]:
    """XLSX synthetic preview용 셀 bbox를 계산한다.

    Args:
        row: 행 번호.
        col: 열 번호.

    Returns:
        preview 기준 셀 bbox.
    """

    cell_w = 150
    cell_h = 34
    x = 62 + (max(1, col) - 1) * cell_w
    y = 58 + (max(1, row) - 1) * cell_h
    return _preview_bbox(x, y, cell_w, cell_h)


def _docx_text_bbox(index: int) -> Tuple[int, List[int]]:
    """DOCX synthetic preview용 문단 bbox를 계산한다.

    Args:
        index: 문단 인덱스.

    Returns:
        페이지 번호와 preview 기준 bbox.
    """

    per_page = 12
    page = index // per_page + 1
    row = index % per_page
    return page, _preview_bbox(150, 72 + row * 42, 660, 32)


def assign_preview_bboxes(nodes: List[dict], ext: str) -> None:
    """synthetic preview를 위한 기본 bbox를 노드에 할당한다.

    Args:
        nodes: bbox를 반영할 노드 목록.
        ext: 파일 확장자.

    Returns:
        없음.
    """

    for index, node in enumerate(nodes):
        if node.get("bbox"):
            continue

        if ext == ".xlsx":
            node["bbox"] = _xlsx_cell_bbox(_safe_int(node.get("row")) or 1, _safe_int(node.get("col")) or 1)
            continue

        if ext == ".docx":
            page, bbox = _docx_text_bbox(index)
            node["page_num"] = node.get("page_num") or page
            node["bbox"] = bbox
            continue

        if ext == ".pptx":
            slide_nodes = [item for item in nodes if item.get("slide_index") == node.get("slide_index")]
            slide_index = slide_nodes.index(node) if node in slide_nodes else index
            col = slide_index % 2
            row = slide_index // 2
            node["bbox"] = _preview_bbox(76 + col * 430, 82 + row * 76, 360, 54)


def is_translatable(text: str) -> bool:
    """문자열이 번역 대상인지 판별한다.

    Args:
        text: 판별할 문자열.

    Returns:
        번역 대상 여부.
    """

    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("="):
        return False
    if re.match(r"^[\d\s.,%+\-$€£¥₩:/()]+$", stripped):
        return False
    if re.match(r"^https?://", stripped) or re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", stripped):
        return False
    return True
