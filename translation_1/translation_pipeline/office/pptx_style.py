"""PPTX 텍스트 스타일 보존 helper."""

from __future__ import annotations

import re
from typing import Any, List

from lxml import etree
from pptx.oxml.ns import qn
from pptx.util import Pt

# 번역된 PPTX run 의 East Asian / Complex Script / Sym typeface 를 강제로
# 한글-호환 폰트(Noto Sans CJK KR) 로 덮어쓴다. 원본 PPTX 가 <a:ea typeface="Arial"/>
# 같은 라틴 폰트를 그대로 갖고 있으면 LibreOffice 가 한글 텍스트에 Liberation Sans
# 로 substitute → CJK 글리프 부재 → PDF/SVG 미리보기에서 default glyph("시" 류)
# 가 반복 출력되는 회귀가 발생한다 (#TODO post-mortem 참조). latin 은 라틴 문자에만
# 적용되므로 원본 디자인 유지를 위해 건드리지 않는다.
_KOREAN_PPTX_TYPEFACE = "Noto Sans CJK KR"
_KOREAN_TYPEFACE_TAGS = ("a:ea", "a:cs", "a:sym")


def _force_korean_typeface_on_run(run: Any, typeface: str = _KOREAN_PPTX_TYPEFACE) -> None:
    """번역된 PPTX run 의 ea/cs/sym typeface 를 한글-호환 폰트로 강제한다."""

    r = getattr(run, "_r", None)
    if r is None:
        return
    rPr = r.find(qn("a:rPr"))
    if rPr is None:
        rPr = etree.SubElement(r, qn("a:rPr"))
        r.insert(0, rPr)
    for tag in _KOREAN_TYPEFACE_TAGS:
        el = rPr.find(qn(tag))
        if el is None:
            el = etree.SubElement(rPr, qn(tag))
        el.set("typeface", typeface)



def _run_style_key(run: Any) -> tuple[Any, Any, Any, Any]:
    font = getattr(run, "font", None)
    return (
        getattr(font, "size", None),
        getattr(font, "bold", None),
        getattr(font, "italic", None),
        getattr(font, "name", None),
    )


def _group_runs_by_style(runs: Any) -> List[List[int]]:
    groups: List[List[int]] = []
    previous_key: tuple[Any, Any, Any, Any] | None = None
    for index, run in enumerate(runs):
        key = _run_style_key(run)
        if not groups or key != previous_key:
            groups.append([index])
        else:
            groups[-1].append(index)
        previous_key = key
    return groups


def _split_title_and_body_translation(translated: str) -> tuple[str, str] | None:
    # A PPT paragraph can contain a bold title and smaller bullet body in
    # separate runs. The LLM often fuses them as "... documents: - Load ...".
    # Split at the first bullet marker so the original run styles survive.
    match = re.search(
        r"(?P<prefix>[:：]?\s*)(?P<body>(?:[-–—―－‒]\s*|•\s+))",
        translated,
    )
    if match:
        head = translated[: match.start()].rstrip()
        body = translated[match.start("body") :].strip()
    else:
        colon_match = re.search(r"[:：]\s+(?=[A-Z0-9])", translated)
        if not colon_match:
            return None
        head = translated[: colon_match.start()].rstrip()
        body = translated[colon_match.end() :].strip()
    if head.endswith((':', '：')):
        head = head[:-1].rstrip()
    if not head or not body:
        return None
    return head, body


def _apply_text_to_run_group(runs: Any, group: List[int], text: str) -> None:
    if not group:
        return
    runs[group[0]].text = text
    if text:
        _force_korean_typeface_on_run(runs[group[0]])
    for index in group[1:]:
        runs[index].text = ""


def _apply_translated_paragraph_text(paragraph: Any, translated: str, node: dict) -> None:
    runs = paragraph.runs
    if not runs:
        return

    groups = _group_runs_by_style(runs)
    split = _split_title_and_body_translation(translated) if len(groups) >= 2 else None
    if split:
        head, body = split
        original_body_text = "".join(runs[index].text for index in groups[1]).strip()
        if original_body_text.startswith(("-", "–", "•")) and not body.startswith(("-", "–", "•")):
            body = f"- {body}"
        _apply_text_to_run_group(runs, groups[0], head)
        _apply_text_to_run_group(runs, groups[1], body)
        for group in groups[2:]:
            _apply_text_to_run_group(runs, group, "")
    else:
        _apply_text_to_run_group(runs, list(range(len(runs))), translated)

    if node.get("edited_font_size") is not None:
        try:
            runs[0].font.size = Pt(float(node["edited_font_size"]))
        except (TypeError, ValueError):
            pass

