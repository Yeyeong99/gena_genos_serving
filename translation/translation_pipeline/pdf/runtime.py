"""PDF 문서 추출/번역/주입 런타임."""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import asyncio
import base64
import html
import os
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import fitz
import pdfplumber

from translation_pipeline.common.llm import (
    batch_translate_async,
    llm_call_async,
)
from translation_pipeline.common.prompt_builder import (
    get_translation_style_context,
)
from translation_pipeline.common.prompts import render_prompt
from translation_pipeline.common.nodes import is_translatable


def _rect_area(rect: Any) -> float:
    """사각형 넓이를 계산한다.

    Args:
        rect: PyMuPDF Rect 객체.

    Returns:
        넓이 값.
    """

    if rect is None:
        return 0.0
    return max(0.0, float(rect.width)) * max(0.0, float(rect.height))


def _bbox_to_rect(bbox: Any) -> Any:
    """bbox 배열을 PyMuPDF Rect로 변환한다.

    Args:
        bbox: 좌표 배열.

    Returns:
        Rect 객체 또는 None.
    """

    try:
        return fitz.Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except Exception:
        return None


def _overlap_ratio(rect_a: Any, rect_b: Any) -> float:
    """두 사각형의 겹침 비율을 계산한다.

    Args:
        rect_a: 기준 Rect.
        rect_b: 비교 Rect.

    Returns:
        기준 Rect 대비 겹침 비율.
    """

    try:
        inter = rect_a & rect_b
        inter_area = _rect_area(inter)
        base_area = _rect_area(rect_a)
        if base_area <= 0:
            return 0.0
        return inter_area / base_area
    except Exception:
        return 0.0


def _merge_rects(rects: List[Any], page_rect: Any, overlap_threshold: float = 0.35) -> List[Any]:
    """겹치는 시각 영역 Rect를 병합한다.

    Args:
        rects: 병합 후보 Rect 목록.
        page_rect: 페이지 경계 Rect.
        overlap_threshold: 병합 기준 비율.

    Returns:
        병합된 Rect 목록.
    """

    merged: List[Any] = []
    for rect in sorted(rects, key=lambda item: (_rect_area(item), float(item.y0), float(item.x0)), reverse=True):
        clipped = rect & page_rect
        if clipped.is_empty or _rect_area(clipped) <= 0:
            continue
        merged_into_existing = False
        for index, existing in enumerate(merged):
            inter = clipped & existing
            if inter.is_empty:
                continue
            min_area = min(_rect_area(clipped), _rect_area(existing))
            if min_area <= 0:
                continue
            if _rect_area(inter) / min_area >= overlap_threshold:
                merged[index] = (existing | clipped) & page_rect
                merged_into_existing = True
                break
        if not merged_into_existing:
            merged.append(clipped)
    merged.sort(key=lambda item: (float(item.y0), float(item.x0)))
    return merged


def extract_pdf(file_path: str) -> List[dict]:
    """PDF에서 텍스트/표/시각 요소를 추출한다.

    Args:
        file_path: 입력 PDF 경로.

    Returns:
        분석용 요소 목록.
    """

    final_results: List[dict] = []
    min_visual_w = 50
    min_visual_h = 50
    min_visual_area = 2500
    visual_padding = 4
    max_visual_regions_per_page = 8
    render_zoom = 2.0

    try:
        doc_fitz = fitz.open(file_path)
        doc_plumber = pdfplumber.open(file_path)
        num_pages = min(len(doc_fitz), len(doc_plumber.pages))

        for page_index in range(num_pages):
            page_fitz = doc_fitz[page_index]
            page_plumber = doc_plumber.pages[page_index]

            final_results.append({"type": "text", "content": f"\n=== Page {page_index + 1} ===\n"})
            elements: List[dict] = []

            tables = page_plumber.find_tables(
                {
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                    "intersection_y_tolerance": 10,
                }
            )
            table_bboxes = []
            table_rects = []

            for table in tables:
                table_bboxes.append(table.bbox)
                table_rect = _bbox_to_rect(table.bbox)
                if table_rect is not None:
                    table_rects.append(table_rect & page_fitz.rect)
                clean_rows = []
                for row in table.extract():
                    clean_rows.append([{"type": "text", "content": (cell or "").strip()} for cell in row])
                elements.append({"y": int(table.bbox[1]), "x": int(table.bbox[0]), "data": {"type": "table", "rows": clean_rows}})

            words = page_plumber.extract_words()
            lines_buffer: Dict[float, List[Tuple[float, str]]] = {}

            for word in words:
                center_x = (word["x0"] + word["x1"]) / 2
                center_y = (word["top"] + word["bottom"]) / 2
                in_table = any(
                    (bbox[0] <= center_x <= bbox[2]) and (bbox[1] <= center_y <= bbox[3])
                    for bbox in table_bboxes
                )
                if in_table:
                    continue

                found_line = False
                for y_key in list(lines_buffer.keys()):
                    if abs(y_key - word["top"]) < 5:
                        lines_buffer[y_key].append((word["x0"], word["text"]))
                        found_line = True
                        break
                if not found_line:
                    lines_buffer[word["top"]] = [(word["x0"], word["text"])]

            for y_key, line_items in lines_buffer.items():
                line_items.sort(key=lambda item: item[0])
                elements.append(
                    {
                        "y": int(y_key),
                        "x": int(line_items[0][0]),
                        "data": {"type": "text", "content": " ".join(item[1] for item in line_items)},
                    }
                )

            image_rects = []
            for image in page_fitz.get_images(full=True):
                xref = image[0]
                try:
                    rects = page_fitz.get_image_rects(xref)
                    if not rects:
                        continue
                    base_image = doc_fitz.extract_image(xref)
                    for rect in rects:
                        clipped = rect & page_fitz.rect
                        image_rects.append(clipped)
                        elements.append(
                            {
                                "y": int(rect.y0),
                                "x": int(rect.x0),
                                "data": {
                                    "type": "image",
                                    "image_data": base_image["image"],
                                    "ext": base_image["ext"],
                                },
                            }
                        )
                except Exception:
                    pass

            try:
                drawings = page_fitz.get_drawings()
            except Exception:
                drawings = []

            vector_candidates = []
            known_rects = table_rects + image_rects
            for drawing in drawings:
                rect = drawing.get("rect")
                if not rect:
                    continue
                rect = fitz.Rect(rect)
                if rect.width < min_visual_w or rect.height < min_visual_h or _rect_area(rect) < min_visual_area:
                    continue
                padded = fitz.Rect(
                    rect.x0 - visual_padding,
                    rect.y0 - visual_padding,
                    rect.x1 + visual_padding,
                    rect.y1 + visual_padding,
                ) & page_fitz.rect
                if padded.is_empty or _rect_area(padded) <= 0:
                    continue
                if any(_overlap_ratio(padded, known) >= 0.7 for known in known_rects):
                    continue
                vector_candidates.append(padded)

            for rect in _merge_rects(vector_candidates, page_fitz.rect)[:max_visual_regions_per_page]:
                try:
                    pix = page_fitz.get_pixmap(matrix=fitz.Matrix(render_zoom, render_zoom), clip=rect, alpha=False)
                    elements.append(
                        {
                            "y": int(rect.y0),
                            "x": int(rect.x0),
                            "data": {"type": "visual", "source": "vector", "image_data": pix.tobytes("png"), "ext": "png"},
                        }
                    )
                except Exception:
                    pass

            if drawings and not image_rects and not vector_candidates:
                try:
                    pix = page_fitz.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    elements.append(
                        {
                            "y": 0,
                            "x": 0,
                            "data": {
                                "type": "visual",
                                "source": "page_fallback",
                                "image_data": pix.tobytes("png"),
                                "ext": "png",
                            },
                        }
                    )
                except Exception:
                    pass

            elements.sort(key=lambda item: (item["y"], item["x"]))
            final_results.extend(item["data"] for item in elements)

        doc_fitz.close()
        doc_plumber.close()
    except Exception as exc:
        log_info(f"[PDF 추출 에러] {exc}")
        return []

    log_info(f"[PDF 추출] {len(final_results)}개 요소 추출 완료")
    return final_results


async def vlm_describe_image_async(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    image_bytes: bytes,
    _: str,
) -> str:
    """PDF 이미지/도형을 VLM으로 설명한다.

    Args:
        sem: 동시성 제어 세마포어.
        session: HTTP 세션.
        image_bytes: 분석할 이미지 바이트.
        _: 확장자. 현재는 로깅 외 사용하지 않음.

    Returns:
        이미지 설명 텍스트. skip이면 빈 문자열.
    """

    if not image_bytes:
        return ""
    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    result = await llm_call_async(
        sem,
        session,
        "You are an AI that analyzes images within documents.",
        """This image is from a PDF document.
1. If it is a table or chart, convert the data into markdown table format accurately.
2. If it is a diagram or flowchart, describe the structure and flow in text.
3. If it is mostly plain paragraph text without visual structure, respond with only 'SKIP'.
4. If it is a decorative icon, background, or meaningless image, respond with only 'SKIP'.""",
        image_base64,
    )
    if "SKIP" in result:
        return ""
    return result


async def convert_pdf_to_text_async(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    parsed_data: List[dict],
) -> str:
    """PDF 추출 요소를 하나의 텍스트로 직렬화한다.

    Args:
        sem: 동시성 제어 세마포어.
        session: HTTP 세션.
        parsed_data: PDF 추출 요소 목록.

    Returns:
        번역용 직렬화 텍스트.
    """

    lines: List[Tuple[str, str]] = []
    vlm_tasks = []
    vlm_indices = []
    vlm_sources = []

    for item in parsed_data:
        if item["type"] == "text":
            lines.append(("text", item["content"]))
        elif item["type"] == "table":
            table_lines = ["[Table]"]
            for row in item["rows"]:
                row_texts = [cell["content"].replace("\n", " ") for cell in row]
                table_lines.append("| " + " | ".join(row_texts) + " |")
            table_lines.append("")
            lines.append(("text", "\n".join(table_lines)))
        elif item["type"] in ("image", "visual") and item.get("image_data"):
            placeholder_index = len(lines)
            lines.append(("image_placeholder", ""))
            vlm_tasks.append(vlm_describe_image_async(sem, session, item["image_data"], item.get("ext", "png")))
            vlm_indices.append(placeholder_index)
            vlm_sources.append(item.get("source", item["type"]))

    if vlm_tasks:
        results = await asyncio.gather(*vlm_tasks)
        for index, result in enumerate(results):
            if result:
                source = vlm_sources[index]
                label = "Image/Chart Analysis" if source == "image" else f"Visual Analysis ({source})"
                lines[vlm_indices[index]] = ("text", f"\n[{label}:\n{result}\n]\n")
            else:
                lines[vlm_indices[index]] = ("text", "")

    return "\n".join(line[1] for line in lines if line[1])


async def translate_long_text_async(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    text: str,
    target_lang: str,
    style_options: dict | None = None,
) -> str:
    """장문 PDF 텍스트를 청크 단위로 번역한다.

    Args:
        sem: 동시성 제어 세마포어.
        session: HTTP 세션.
        text: 번역 대상 장문 텍스트.
        target_lang: 대상 언어.

    Returns:
        번역 결과 문자열.
    """

    if not text.strip():
        return ""

    pages = [page.strip() for page in re.split(r"\n===\s*Page\s+\d+\s*===\n", text) if page.strip()]
    chunks: List[str] = []
    current_chunk: List[str] = []
    current_len = 0

    for page in pages:
        if current_chunk and current_len + len(page) > 6000:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(page)
        current_len += len(page)
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    language_guard = render_prompt("translation_language_guard.jinja", target_lang=target_lang)
    style_context = render_prompt(
        "translation_style_context.jinja",
        style_context=get_translation_style_context(target_lang, style_options),
    )
    system = f"""You are a professional document translator.
Translate the following text into {target_lang} naturally and accurately, following the translation purpose, style, and terminology requirements below when provided.
CRITICAL RULES:
1. Preserve ALL numbers, currency, percentages, dates, units EXACTLY as-is.
2. Preserve ALL proper nouns, company names, person names EXACTLY as-is.
3. Preserve ALL URLs, emails, file paths EXACTLY as-is.
4. Preserve table formatting (markdown pipes |).
5. Preserve the original meaning and intent; adapt tone/register only as required by the selected translation options.
6. {language_guard}
7. Return ONLY the translated text, no explanations.
{style_context}"""

    async def translate_chunk(chunk: str) -> str:
        result = await llm_call_async(sem, session, system, chunk)
        return result if result else chunk

    return "\n\n".join(await asyncio.gather(*[translate_chunk(chunk) for chunk in chunks]))


async def polish_pdf_translation_async(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    translated_text: str,
    target_lang: str,
    style_options: dict | None = None,
) -> str:
    """장문 PDF 번역 결과를 읽기 좋게 다듬는다.

    Args:
        sem: 동시성 제어 세마포어.
        session: HTTP 세션.
        translated_text: 1차 번역 결과.
        target_lang: 대상 언어.

    Returns:
        후처리된 번역 문자열.
    """

    if not translated_text.strip():
        return translated_text

    style_context = render_prompt(
        "translation_style_context.jinja",
        style_context=get_translation_style_context(target_lang, style_options),
    )
    system = f"""You are a professional editor for translated documents.
Polish the translated text in {target_lang} so it is easier for humans to read, following the selected translation purpose, style, and terminology requirements when provided.
CRITICAL RULES:
1. Do NOT add, remove, or reinterpret facts.
2. Preserve ALL numbers, currency, percentages, dates, units EXACTLY as-is.
3. Preserve ALL proper nouns, company/person/place names, ticker symbols EXACTLY as-is.
4. Preserve ALL URLs, emails, and file paths EXACTLY as-is.
5. Preserve markdown tables and list structures.
6. Fix awkward line breaks/spacing and improve paragraph flow only; do not override the selected style requirements.
7. Return ONLY the polished text, no explanations.
{style_context}"""
    result = await llm_call_async(sem, session, system, translated_text)
    return result if result else translated_text


def _find_korean_font() -> Optional[str]:
    """PDF 삽입 시 사용할 한글 폰트 경로를 찾는다.

    Args:
        없음.

    Returns:
        폰트 경로 또는 None.
    """

    candidates = [
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/Library/Fonts/AppleGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/malgun.ttf",
    ]
    return next((path for path in candidates if os.path.exists(path)), None)


def extract_pdf_lines(file_path: str) -> Tuple[Any, List[dict]]:
    """PDF 각 텍스트 line의 위치/크기 정보를 추출한다.

    Args:
        file_path: 입력 PDF 경로.

    Returns:
        PDF 문서 객체와 line 노드 목록.
    """

    doc = fitz.open(file_path)
    lines_out: List[dict] = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_spans = line.get("spans", [])
                line_text = "".join(span.get("text", "") for span in line_spans).strip()
                if not line_text or not is_translatable(line_text):
                    continue

                boxes = [fitz.Rect(span["bbox"]) for span in line_spans if span.get("bbox")]
                line_rect = fitz.Rect(boxes[0]) if boxes else fitz.Rect(line["bbox"])
                for box in boxes[1:]:
                    line_rect.include_rect(box)

                span_sizes = [float(span.get("size", 11)) for span in line_spans if span.get("text", "").strip()]
                line_size = max(span_sizes) if span_sizes else float(line.get("size", 11))
                lines_out.append(
                    {
                        "type": "pdf_line",
                        "group": "pdf_line",
                        "page_num": page_num,
                        "bbox": list(line_rect),
                        "size": line_size,
                        "font_size": line_size,
                        "text": line_text,
                    }
                )
    log_info(f"[PDF line 추출] {len(lines_out)}개 line 추출 완료 (페이지 {len(doc)}장)")
    return doc, lines_out


def inject_pdf(doc: Any, lines: List[dict], trans_map: Dict[str, str]) -> None:
    """PDF 원문 line 위치에 번역문을 주입한다.

    Args:
        doc: PDF 문서 객체.
        lines: PDF line 노드 목록.
        trans_map: 원문/번역 매핑.

    Returns:
        없음.
    """

    min_font_size = 4.5
    size_delta = 1.0
    min_scale = 0.55
    fontfile = _find_korean_font()
    font_archive = None
    font_css = ""
    if fontfile:
        try:
            font_archive = fitz.Archive(os.path.dirname(fontfile))
            font_name = os.path.basename(fontfile)
            font_css = f"""
            @font-face {{
                font-family: 'InjectedPDFText';
                src: url('{font_name}');
            }}
            """
        except Exception:
            font_archive = None
            font_css = ""

    pixmap_cache: Dict[int, Any] = {}

    def get_pixmap(page_num: int) -> Any:
        if page_num not in pixmap_cache:
            try:
                pixmap_cache[page_num] = doc[page_num].get_pixmap(
                    colorspace=fitz.csRGB,
                    alpha=False,
                    matrix=fitz.Matrix(1, 1),
                )
            except Exception:
                pixmap_cache[page_num] = None
        return pixmap_cache[page_num]

    def pixel_at(pix: Any, x: float, y: float) -> Any:
        if pix is None:
            return None
        xi, yi = int(x), int(y)
        if xi < 0 or xi >= pix.width or yi < 0 or yi >= pix.height:
            return None
        idx = yi * pix.stride + xi * pix.n
        if idx + 2 >= len(pix.samples):
            return None
        return (pix.samples[idx], pix.samples[idx + 1], pix.samples[idx + 2])

    def sample_bg_color(page_num: int, rect: fitz.Rect) -> Tuple[float, float, float]:
        pix = get_pixmap(page_num)
        if pix is None:
            return (1, 1, 1)
        page_rect = doc[page_num].rect
        pad = 2
        points = []
        if rect.y0 - pad > page_rect.y0:
            points.extend(
                [
                    (rect.x0 + 1, rect.y0 - pad),
                    ((rect.x0 + rect.x1) / 2, rect.y0 - pad),
                    (rect.x1 - 1, rect.y0 - pad),
                ]
            )
        if rect.y1 + pad < page_rect.y1:
            points.append(((rect.x0 + rect.x1) / 2, rect.y1 + pad))
        if rect.x0 - pad > page_rect.x0:
            points.append((rect.x0 - pad, (rect.y0 + rect.y1) / 2))
        if rect.x1 + pad < page_rect.x1:
            points.append((rect.x1 + pad, (rect.y0 + rect.y1) / 2))

        colors = [color for color in (pixel_at(pix, x, y) for x, y in points) if color is not None]
        if not colors:
            return (1, 1, 1)
        red, green, blue = Counter(colors).most_common(1)[0][0]
        return (red / 255.0, green / 255.0, blue / 255.0)

    def visible_len(text: str) -> int:
        return max(1, len(re.sub(r"\s+", "", text or "")))

    def estimate_font_size(line: dict, translated: str, rect: fitz.Rect) -> float:
        base_size = max(min_font_size, float(line.get("size", 11)) - size_delta)
        height_based = max(min_font_size, rect.height * 0.88)
        original_len = visible_len(line.get("text", ""))
        translated_len = visible_len(translated)
        length_ratio = max(1.0, translated_len / original_len)
        ratio_adjusted = base_size / (length_ratio ** 0.40)
        usable_width = max(rect.width, 1.0)
        approx_char_width = max(base_size * 0.78, 1.0)
        estimated_chars_per_line = max(1.0, usable_width / approx_char_width)
        estimated_line_count = max(1.0, translated_len / estimated_chars_per_line)
        line_adjusted = height_based / (estimated_line_count ** 0.72)
        return max(min_font_size, min(base_size, height_based, ratio_adjusted, line_adjusted))

    def node_translation(line: dict) -> str:
        if line.get("translated_text") is not None:
            return str(line.get("translated_text", ""))
        original = str(line.get("text", ""))
        return str(trans_map.get(original, original))

    work: List[dict] = []
    for line in lines:
        original = str(line["text"])
        translated = node_translation(line)
        if not translated or translated == original:
            continue
        page = doc[line["page_num"]]
        rect = fitz.Rect(line["bbox"])
        expanded = fitz.Rect(
            max(page.rect.x0, rect.x0 - rect.width * 0.02),
            max(page.rect.y0, rect.y0 - rect.height * 0.04),
            min(page.rect.x1, rect.x1 + rect.width * 0.12),
            min(page.rect.y1, rect.y1 + rect.height * 0.65),
        )
        work.append(
            {
                **line,
                "translated": translated,
                "expanded": expanded,
                "bg_color": sample_bg_color(line["page_num"], rect),
                "initial_size": estimate_font_size(line, translated, rect),
            }
        )

    if not work:
        log_info("[PDF 주입] 번역 변경된 line 없음")
        return

    for item in work:
        doc[item["page_num"]].add_redact_annot(fitz.Rect(item["bbox"]), fill=item["bg_color"])
    for page_num in {item["page_num"] for item in work}:
        doc[page_num].apply_redactions()

    def build_html_markup(text: str, fontsize: float) -> Tuple[str, str]:
        safe_text = html.escape(text).replace("\n", "<br>")
        body_css = [
            "margin:0",
            "padding:0",
            f"font-size:{fontsize:.2f}pt",
            "line-height:1.06",
            "color:#000",
            "text-align:left",
            "white-space:pre-wrap",
            "word-break:break-word",
            "overflow-wrap:anywhere",
        ]
        if fontfile:
            body_css.append(
                "font-family:'InjectedPDFText', 'Apple SD Gothic Neo', 'AppleGothic', 'Malgun Gothic', sans-serif"
            )
        else:
            body_css.append("font-family:'Apple SD Gothic Neo', 'AppleGothic', 'Malgun Gothic', sans-serif")
        css = f"""
        {font_css}
        body {{
            {'; '.join(body_css)};
        }}
        p {{
            margin: 0;
            padding: 0;
        }}
        """
        return f"<div>{safe_text}</div>", css

    for item in work:
        final_size = item["initial_size"]
        page = doc[item["page_num"]]
        markup, css = build_html_markup(item["translated"], final_size)
        inserted = False
        try:
            _, scale = page.insert_htmlbox(
                item["expanded"],
                markup,
                css=css,
                scale_low=min_scale,
                archive=font_archive,
            )
            inserted = scale > 0
        except Exception:
            inserted = False

        if not inserted:
            kwargs: Dict[str, Any] = {"fontsize": final_size, "align": 0}
            if fontfile:
                kwargs["fontfile"] = fontfile
                kwargs["fontname"] = "F0"
            page.insert_textbox(item["expanded"], item["translated"], **kwargs)
