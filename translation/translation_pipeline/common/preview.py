"""파이프라인 공통 preview 생성/외부화 유틸."""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from .nodes import PREVIEW_HEIGHT, PREVIEW_WIDTH, _safe_float

try:
    import fitz  # type: ignore

    PDF_AVAILABLE = True
except Exception:
    fitz = None  # type: ignore
    PDF_AVAILABLE = False

try:
    from PIL import Image, ImageDraw  # type: ignore

    PIL_AVAILABLE = True
except Exception:
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    PIL_AVAILABLE = False


PreviewPayload = Dict[str, Any]
PreviewTransform = Tuple[float, float, float, int, int]


def _normalize_text(value: Any) -> str:
    """입력값을 비교 가능한 문자열로 정규화한다.

    Args:
        value: 정규화할 원본 값.

    Returns:
        공백을 유지한 문자열.
    """

    if value is None:
        return ""
    return str(value).strip()


def _compact_match_text(value: str) -> str:
    """텍스트 매칭에 사용할 압축 문자열을 만든다.

    Args:
        value: 원본 문자열.

    Returns:
        공백을 제거하고 소문자로 변환한 문자열.
    """

    return "".join(unicodedata.normalize("NFC", _normalize_text(value)).split()).lower()


def _looks_like_punctuation_only(value: str) -> bool:
    """문장부호만 있는 line인지 판별한다."""

    normalized = _normalize_text(value)
    if not normalized:
        return True
    return not any(char.isalnum() for char in normalized)


def _find_libreoffice_bin() -> str:
    """LibreOffice 실행 파일 경로를 찾는다.

    Args:
        없음.

    Returns:
        실행 파일 경로. 없으면 빈 문자열.
    """

    candidates = [
        os.getenv("LIBREOFFICE_BIN", ""),
        shutil.which("soffice") or "",
        shutil.which("libreoffice") or "",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def _libreoffice_timeout() -> float | None:
    """LibreOffice 변환 timeout. 0 이하이면 대용량 테스트용으로 timeout을 끈다."""

    raw = os.getenv("AI_TRANSLATION_LIBREOFFICE_TIMEOUT", "120").strip()
    try:
        timeout = float(raw)
    except ValueError:
        timeout = 120.0
    return None if timeout <= 0 else timeout


def _convert_office_to_pdf(file_path: str, output_dir: str) -> str:
    """Office 문서를 PDF로 변환한다.

    Args:
        file_path: 입력 Office 문서 경로.
        output_dir: PDF 출력 디렉터리.

    Returns:
        생성된 PDF 경로.
    """

    libreoffice_bin = _find_libreoffice_bin()
    if not libreoffice_bin:
        raise RuntimeError("LibreOffice 실행 파일을 찾을 수 없음")

    profile_dir = os.path.join(output_dir, "lo-profile")
    os.makedirs(profile_dir, exist_ok=True)
    args = [
        "--headless",
        "--invisible",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        "--norestore",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to",
        "pdf",
        "--outdir",
        output_dir,
        file_path,
    ]
    completed = _run_libreoffice_export(libreoffice_bin, args, timeout=_libreoffice_timeout())
    if completed.returncode != 0:
        raise RuntimeError(
            f"LibreOffice PDF 변환 실패: {completed.stderr.strip() or completed.stdout.strip()}"
        )

    expected = os.path.join(
        output_dir,
        f"{os.path.splitext(os.path.basename(file_path))[0]}.pdf",
    )
    if os.path.exists(expected):
        return expected

    pdf_files = [
        os.path.join(output_dir, name)
        for name in os.listdir(output_dir)
        if name.lower().endswith(".pdf")
    ]
    if not pdf_files:
        raise RuntimeError("LibreOffice 변환 결과 PDF를 찾을 수 없음")
    return pdf_files[0]


def _run_libreoffice_export(
    libreoffice_bin: str,
    args: List[str],
    *,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """LibreOffice 직접 실행이 macOS 앱 등록에서 실패하면 open 경로로 재시도한다."""

    direct = subprocess.run(
        [libreoffice_bin, *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if direct.returncode == 0 or sys.platform != "darwin":
        return direct

    app_target = "/Applications/LibreOffice.app"
    open_target = [app_target] if os.path.exists(app_target) else ["-a", "LibreOffice"]
    via_open = subprocess.run(
        ["open", "-W", "-n", *open_target, "--args", *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if via_open.returncode == 0:
        return via_open

    combined_stderr = "\n".join(
        part for part in (direct.stderr.strip(), via_open.stderr.strip()) if part
    )
    combined_stdout = "\n".join(
        part for part in (direct.stdout.strip(), via_open.stdout.strip()) if part
    )
    return subprocess.CompletedProcess(
        args=direct.args,
        returncode=via_open.returncode,
        stdout=combined_stdout,
        stderr=combined_stderr,
    )


def _pdf_page_transform(page: Any) -> PreviewTransform:
    """PDF 페이지를 preview 좌표계로 매핑하는 변환 정보를 계산한다.

    Args:
        page: PyMuPDF 페이지 객체.

    Returns:
        배율/오프셋/preview 크기 정보.
    """

    page_width = float(page.rect.width or 1)
    page_height = float(page.rect.height or 1)
    scale = PREVIEW_WIDTH / page_width
    preview_height = max(1, int(round(page_height * scale)))
    return scale, 0.0, 0.0, PREVIEW_WIDTH, preview_height


def _map_pdf_bbox_to_preview(
    bbox: Tuple[float, float, float, float],
    transform: PreviewTransform,
) -> List[int]:
    """PDF bbox를 preview 픽셀 좌표로 변환한다.

    Args:
        bbox: PDF 좌표계 bbox.
        transform: 페이지 변환 정보.

    Returns:
        preview 좌표계 bbox.
    """

    scale, offset_x, offset_y, _, _ = transform
    x0, y0, x1, y1 = bbox
    return [
        int(round(offset_x + x0 * scale)),
        int(round(offset_y + y0 * scale)),
        int(round(offset_x + x1 * scale)),
        int(round(offset_y + y1 * scale)),
    ]


def _render_pdf_preview_pages(
    pdf_path: str,
) -> Tuple[List[Any], List[PreviewTransform], List[Dict[str, int]]]:
    """PDF를 preview 이미지 목록으로 렌더링한다.

    Args:
        pdf_path: 렌더링할 PDF 경로.

    Returns:
        이미지 객체 목록, 페이지 변환 정보, 페이지 크기 목록.
    """

    if not (PDF_AVAILABLE and PIL_AVAILABLE):
        raise RuntimeError("PDF/PIL 렌더링 라이브러리가 준비되지 않음")

    images: List[Any] = []
    transforms: List[PreviewTransform] = []
    page_sizes: List[Dict[str, int]] = []
    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            transform = _pdf_page_transform(page)
            scale, offset_x, offset_y, preview_width, preview_height = transform
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            rendered = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
            canvas = Image.new("RGB", (preview_width, preview_height), "#ffffff")
            canvas.paste(rendered, (int(round(offset_x)), int(round(offset_y))))
            images.append(canvas)
            transforms.append(transform)
            page_sizes.append({"width": preview_width, "height": preview_height})
    finally:
        doc.close()
    return images, transforms, page_sizes


def _render_pdf_preview_svgs(pdf_path: str) -> List[str]:
    """PDF 페이지를 SVG 문자열 목록으로 렌더링한다.

    PNG 와 달리 SVG 는 텍스트가 ``<text>`` 요소로 살아남아 (a) iframe 안에서 텍스트
    선택·검색이 가능하고 (b) 향후 ``data-node-id`` 주입을 통한 블록 단위 편집·
    하이라이트의 토대가 된다. 또한 벡터라 줌 시 픽셀화가 없고 동일 슬라이드 기준
    PNG 의 ~10% 크기로 가볍다.

    ``text_as_path=0`` 은 텍스트를 path 가 아닌 ``<text>`` 로 보존하기 위한 플래그.

    Args:
        pdf_path: 렌더링할 PDF 경로.

    Returns:
        페이지 순서대로의 SVG 문자열 목록.
    """

    if not PDF_AVAILABLE:
        raise RuntimeError("PyMuPDF 가 준비되지 않음")

    svgs: List[str] = []
    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            svgs.append(page.get_svg_image(text_as_path=0))
    finally:
        doc.close()
    return svgs


def _extract_pdf_lines_for_preview(
    pdf_path: str,
    transforms: List[PreviewTransform],
) -> List[dict]:
    """PDF preview 매칭용 line bbox 목록을 추출한다.

    Args:
        pdf_path: 원본 PDF 경로.
        transforms: 페이지별 preview 변환 정보.

    Returns:
        텍스트/페이지/bbox를 포함한 line 목록.
    """

    if not PDF_AVAILABLE:
        return []

    lines: List[dict] = []
    doc = fitz.open(pdf_path)
    try:
        for page_index, page in enumerate(doc):
            transform = transforms[page_index] if page_index < len(transforms) else _pdf_page_transform(page)
            page_dict = page.get_text("dict")
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    spans = [
                        span
                        for span in line.get("spans", [])
                        if _normalize_text(span.get("text", ""))
                    ]
                    if not spans:
                        continue
                    text = _normalize_text("".join(span.get("text", "") for span in spans))
                    if not text or _looks_like_punctuation_only(text):
                        continue
                    weighted_font_total = 0.0
                    weighted_char_total = 0
                    preview_font_sizes: List[float] = []
                    for span in spans:
                        span_text = _normalize_text(span.get("text", ""))
                        span_chars = max(1, len(span_text.strip()) or len(span_text))
                        span_size = _safe_float(span.get("size"))
                        if span_size:
                            preview_font = max(1.0, span_size * float(transform[0]))
                            preview_font_sizes.append(preview_font)
                            weighted_font_total += preview_font * span_chars
                            weighted_char_total += span_chars
                    x0 = min(float(span["bbox"][0]) for span in spans)
                    y0 = min(float(span["bbox"][1]) for span in spans)
                    x1 = max(float(span["bbox"][2]) for span in spans)
                    y1 = max(float(span["bbox"][3]) for span in spans)
                    font_size = None
                    if weighted_char_total > 0:
                        font_size = weighted_font_total / weighted_char_total
                    elif preview_font_sizes:
                        font_size = sum(preview_font_sizes) / len(preview_font_sizes)
                    lines.append(
                        {
                            "text": text,
                            "compact": _compact_match_text(text),
                            "page_num": page_index + 1,
                            "bbox": _map_pdf_bbox_to_preview((x0, y0, x1, y1), transform),
                            "font_size": font_size,
                        }
                    )
    finally:
        doc.close()
    return lines


def _union_preview_bboxes(lines: List[dict]) -> List[int]:
    """여러 line bbox를 하나의 bbox로 합친다.

    Args:
        lines: bbox를 포함한 line 목록.

    Returns:
        합쳐진 bbox.
    """

    return [
        min(line["bbox"][0] for line in lines),
        min(line["bbox"][1] for line in lines),
        max(line["bbox"][2] for line in lines),
        max(line["bbox"][3] for line in lines),
    ]


def _scale_bbox_to_page(bbox: List[int], page_size: dict) -> List[int]:
    """synthetic bbox를 실제 preview 페이지 크기에 맞게 스케일한다."""

    page_width = max(1, int(page_size.get("width", PREVIEW_WIDTH) or PREVIEW_WIDTH))
    page_height = max(1, int(page_size.get("height", PREVIEW_HEIGHT) or PREVIEW_HEIGHT))
    scale_x = page_width / PREVIEW_WIDTH
    scale_y = page_height / PREVIEW_HEIGHT
    return [
        int(round(bbox[0] * scale_x)),
        int(round(bbox[1] * scale_y)),
        int(round(bbox[2] * scale_x)),
        int(round(bbox[3] * scale_y)),
    ]


def _merge_source_bbox_with_matched(ext: str, node: dict, source_bbox: List[int], matched_bbox: List[int]) -> List[int]:
    """소스 문서 bbox와 PDF 매칭 bbox를 포맷별 규칙으로 합친다."""

    if ext == ".pptx":
        return [
            min(source_bbox[0], matched_bbox[0]),
            min(source_bbox[1], matched_bbox[1]),
            max(source_bbox[2], matched_bbox[2]),
            max(source_bbox[3], matched_bbox[3]),
        ]

    if ext == ".xlsx":
        row_span = int(node.get("row_span", 1) or 1)
        if row_span > 1:
            return [
                min(source_bbox[0], matched_bbox[0]),
                min(source_bbox[1], matched_bbox[1]),
                max(source_bbox[2], matched_bbox[2]),
                max(source_bbox[3], matched_bbox[3]),
            ]
        return [
            min(source_bbox[0], matched_bbox[0]),
            matched_bbox[1],
            max(source_bbox[2], matched_bbox[2]),
            matched_bbox[3],
        ]

    if ext == ".docx":
        return matched_bbox

    return matched_bbox


def _match_nodes_to_pdf_lines(
    nodes: List[dict],
    pdf_lines: List[dict],
    page_sizes: List[dict],
    ext: str,
) -> int:
    """문서 노드를 PDF line bbox와 매칭한다.

    Args:
        nodes: 원본 문서에서 추출한 노드 목록.
        pdf_lines: PDF에서 추출한 line 목록.
        page_sizes: preview 페이지 크기 목록.
        ext: 파일 확장자.

    Returns:
        매칭 성공한 노드 수.
    """

    used: set[int] = set()
    matched_count = 0
    ordered_cursor = 0

    for node in nodes:
        target = _compact_match_text(node.get("text", ""))
        if not target:
            continue

        best_score = 0.0
        best_indices: List[int] = []
        max_window = 8
        if ext == ".docx":
            max_window = min(48, max(12, len(target) // 70 + 8))

        start_range = range(len(pdf_lines))
        if ext == ".docx":
            start_range = range(ordered_cursor, len(pdf_lines))

        for start_index in start_range:
            if start_index in used:
                continue
            window_lines: List[dict] = []
            compact_parts: List[str] = []
            for end_index in range(start_index, min(len(pdf_lines), start_index + max_window)):
                if end_index in used:
                    break
                line = pdf_lines[end_index]
                window_lines.append(line)
                compact_parts.append(line["compact"])
                candidate = "".join(compact_parts)
                if not candidate:
                    continue
                if candidate == target:
                    score = 1.0
                elif target in candidate or candidate in target:
                    score = min(len(candidate), len(target)) / max(len(candidate), len(target))
                else:
                    score = SequenceMatcher(None, target, candidate).ratio()
                if ext == ".docx" and isinstance(node.get("bbox"), list) and len(node["bbox"]) >= 4:
                    page_num = int(node.get("page_num", 1) or 1)
                    page_index = max(0, min(len(page_sizes) - 1, page_num - 1))
                    if page_sizes:
                        expected_bbox = _scale_bbox_to_page(node["bbox"][:4], page_sizes[page_index])
                        union_bbox = _union_preview_bboxes(window_lines)
                        expected_y = (expected_bbox[1] + expected_bbox[3]) / 2
                        candidate_y = (union_bbox[1] + union_bbox[3]) / 2
                        expected_h = max(24, expected_bbox[3] - expected_bbox[1])
                        y_gap_ratio = min(1.0, abs(candidate_y - expected_y) / (expected_h * 6))
                        score *= 1.0 - (y_gap_ratio * 0.35)
                if score > best_score:
                    best_score = score
                    best_indices = list(range(start_index, end_index + 1))
                if len(candidate) > len(target) * 1.15 and score >= 0.985:
                    break
                if len(candidate) > len(target) * 1.35 and score < 0.92:
                    break

        if best_score < 0.62 or not best_indices:
            continue

        matched_lines = [pdf_lines[index] for index in best_indices]
        node["page_num"] = matched_lines[0]["page_num"]
        matched_bbox = _union_preview_bboxes(matched_lines)
        matched_font_sizes = [
            _safe_float(line.get("font_size"))
            for line in matched_lines
            if _safe_float(line.get("font_size")) is not None
        ]
        if matched_font_sizes:
            avg_matched_font = sum(matched_font_sizes) / len(matched_font_sizes)
            node["font_size"] = round(avg_matched_font, 2)
        source_bbox = node.get("bbox")
        page_index = matched_lines[0]["page_num"] - 1
        if (
            isinstance(source_bbox, list)
            and len(source_bbox) >= 4
            and 0 <= page_index < len(page_sizes)
        ):
            scaled_source_bbox = _scale_bbox_to_page(source_bbox[:4], page_sizes[page_index])
            node["bbox"] = _merge_source_bbox_with_matched(ext, node, scaled_source_bbox, matched_bbox)
        else:
            node["bbox"] = matched_bbox
        used.update(best_indices)
        if ext == ".docx":
            ordered_cursor = max(ordered_cursor, best_indices[-1] + 1)
        matched_count += 1

    return matched_count


def _encode_image_to_base64(image: Any) -> str:
    """PIL 이미지를 base64 PNG 문자열로 변환한다.

    Args:
        image: 변환할 PIL 이미지.

    Returns:
        base64 인코딩된 PNG 문자열.
    """

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _build_translated_preview_pages(
    images: List[Any],
    nodes: List[dict],
) -> List[Any]:
    """오른쪽 번역 preview용 빈 배경 이미지를 생성한다.

    Args:
        images: 원본 preview 이미지 목록.
        nodes: page_num/bbox가 반영된 문서 노드 목록.

    Returns:
        텍스트 영역이 가려진 preview 이미지 목록.
    """

    if not PIL_AVAILABLE:
        return images

    translated_pages: List[Any] = []
    for page_index, image in enumerate(images, start=1):
        canvas = image.copy().convert("RGB")
        draw = ImageDraw.Draw(canvas)
        page_nodes = [
            node
            for node in nodes
            if int(node.get("page_num", 1) or 1) == page_index and isinstance(node.get("bbox"), list)
        ]
        for node in page_nodes:
            bbox = node.get("bbox", [])
            if len(bbox) < 4:
                continue
            x0, y0, x1, y1 = [int(value) for value in bbox[:4]]
            if x1 <= x0 or y1 <= y0:
                continue
            # 번역 편집 영역이 또렷하게 보이도록 텍스트 영역을 흰색으로 덮는다.
            draw.rounded_rectangle([x0, y0, x1, y1], radius=6, fill="#ffffff")
        translated_pages.append(canvas)
    return translated_pages


def build_office_preview_payload(
    file_path: str,
    nodes: List[dict],
    ext: str,
) -> PreviewPayload:
    """Office 문서 preview payload를 생성한다.

    Args:
        file_path: 입력 문서 경로.
        nodes: preview 좌표를 갱신할 문서 노드 목록.
        ext: 파일 확장자.

    Returns:
        preview 이미지/페이지 크기/렌더 모드를 포함한 payload.
    """

    if ext not in (".docx", ".xlsx", ".pptx"):
        return {
            "original_preview_images": [],
            "translated_preview_images": [],
            "preview_page_sizes": [],
            "preview_render_mode": "synthetic",
        }

    if not (PDF_AVAILABLE and PIL_AVAILABLE):
        return {
            "original_preview_images": [],
            "translated_preview_images": [],
            "preview_page_sizes": [],
            "preview_render_mode": "synthetic",
        }

    try:
        with tempfile.TemporaryDirectory(prefix="office-preview-") as tmpdir:
            pdf_path = _convert_office_to_pdf(file_path, tmpdir)
            original_pages, transforms, page_sizes = _render_pdf_preview_pages(pdf_path)
            pdf_lines = _extract_pdf_lines_for_preview(pdf_path, transforms)
            matched_count = _match_nodes_to_pdf_lines(nodes, pdf_lines, page_sizes, ext)
            log_info(
                f"[실제 Preview] PDF 렌더 기반 preview 생성: pages={len(original_pages)}, "
                f"pdf_lines={len(pdf_lines)}, matched_nodes={matched_count}/{len(nodes)}"
            )
            # 좌/우 surface가 동일한 page raster를 기준으로 스케일링되어야 bbox가 정확히 맞는다.
            # 번역본은 배경을 따로 렌더링하지 않고 동일한 원본 page image 위에 overlay만 덮는다.
            translated_pages = original_pages
            return {
                "original_preview_images": [
                    _encode_image_to_base64(image) for image in original_pages
                ],
                "translated_preview_images": [
                    _encode_image_to_base64(image) for image in translated_pages
                ],
                "preview_page_sizes": page_sizes,
                "preview_render_mode": "actual",
            }
    except Exception as exc:
        log_info(f"[실제 Preview] 실패 - 빈 preview fallback: {exc}")
        return {
            "original_preview_images": [],
            "translated_preview_images": [],
            "preview_page_sizes": [],
            "preview_render_mode": "synthetic",
        }


def externalize_preview_payload(
    preview_payload: PreviewPayload,
    preview_output_dir: str,
    preview_base_url: str,
) -> PreviewPayload:
    """base64 preview 이미지를 정적 파일 URL로 외부화한다.

    Args:
        preview_payload: preview 이미지 payload.
        preview_output_dir: 이미지 저장 디렉터리 루트.
        preview_base_url: 저장 이미지 접근용 base URL.

    Returns:
        이미지 URL로 치환된 preview payload.
    """

    if not preview_output_dir or not preview_base_url:
        return preview_payload

    cleanup_preview_output_dir(preview_output_dir)

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(preview_output_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)

    result = dict(preview_payload)
    for key, prefix in (
        ("original_preview_images", "original"),
        ("translated_preview_images", "translated"),
    ):
        values = preview_payload.get(key, [])
        if not isinstance(values, list):
            continue

        urls: List[str] = []
        for index, item in enumerate(values, start=1):
            if not isinstance(item, str) or item.startswith("http://") or item.startswith("https://"):
                if isinstance(item, str):
                    urls.append(item)
                continue
            output_name = f"{prefix}-{index}.png"
            output_path = os.path.join(job_dir, output_name)
            with open(output_path, "wb") as file_handle:
                file_handle.write(base64.b64decode(item))
            urls.append(f"{preview_base_url.rstrip('/')}/{job_id}/{output_name}")
        result[key] = urls

    return result


def cleanup_preview_output_dir(preview_output_dir: str, max_age_seconds: int = 60 * 60 * 6) -> None:
    """preview 루트 아래의 오래된 job 디렉터리를 정리한다."""

    if not preview_output_dir or not os.path.isdir(preview_output_dir):
        return

    now = time.time()
    try:
        with os.scandir(preview_output_dir) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                try:
                    age = now - entry.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age <= max_age_seconds:
                    continue
                try:
                    shutil.rmtree(entry.path, ignore_errors=True)
                except Exception:
                    continue
    except FileNotFoundError:
        return
