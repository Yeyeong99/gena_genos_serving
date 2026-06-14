"""Small preview helper wrappers for Office pipelines."""

from __future__ import annotations

from typing import Any

from .preview import (
    build_docx_html_preview_url,
    build_pptx_html_preview_url,
    build_xlsx_html_preview_url,
)


def build_html_preview_url(
    ext: str,
    file_path: str,
    preview_output_dir: str,
    preview_base_url: str,
    *,
    job_token: str | None = None,
    subdir: str | None = None,
    visible_slides: int | None = None,
    visible_sheets: int | None = None,
) -> str | None:
    """확장자에 맞는 HTML preview URL 생성 함수를 호출한다."""

    if ext == ".pptx":
        return build_pptx_html_preview_url(
            file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_token,
            subdir=subdir or default_html_preview_subdir(ext),
            visible_slides=visible_slides,
        )
    if ext == ".docx":
        return build_docx_html_preview_url(
            file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_token,
            subdir=subdir or default_html_preview_subdir(ext),
        )
    if ext == ".xlsx":
        return build_xlsx_html_preview_url(
            file_path,
            preview_output_dir,
            preview_base_url,
            job_token=job_token,
            subdir=subdir or default_html_preview_subdir(ext),
            visible_sheets=visible_sheets,
        )
    return None


def default_html_preview_subdir(ext: str) -> str:
    """문서 타입별 HTML preview 엔진 이름을 subdir에 반영한다."""

    return "libreoffice-svg-html" if ext == ".pptx" else "libreoffice-html"


def translated_html_preview_subdir(ext: str, *, version: str | None = None) -> str:
    engine = "libreoffice-svg" if ext == ".pptx" else "libreoffice"
    base = f"translated-{engine}-html-live"
    return f"{base}/{version}" if version else base


def translated_html_preview_job_subdir(ext: str) -> str:
    engine = "libreoffice-svg" if ext == ".pptx" else "libreoffice"
    return f"translated-{engine}-html-preview-job"


def html_only_preview_payload() -> dict[str, Any]:
    """HTML iframe preview를 사용할 때 이미지/PDF preview 생성을 생략한다."""

    return {
        "original_preview_images": [],
        "translated_preview_images": [],
        "preview_page_sizes": [],
        "preview_render_mode": "html",
    }
