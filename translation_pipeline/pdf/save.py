"""PDF 문서 주입/저장 단계 모듈."""

from __future__ import annotations

import os

from .types import DownloadPayload, PdfPipelineDeps


def inject_translated_pdf(
    doc: object,
    lines: list[dict],
    trans_map: dict[str, str],
    deps: PdfPipelineDeps,
) -> None:
    """번역 결과를 PDF 문서 객체에 주입한다.

    Args:
        doc: PDF 문서 객체.
        lines: 번역 대상 line 목록.
        trans_map: 원문/번역 매핑.
        deps: 주입 단계에서 필요한 의존성 묶음.

    Returns:
        없음.
    """

    deps.inject_pdf(doc, lines, trans_map)


def save_translated_pdf(
    file_path: str,
    doc: object,
    deps: PdfPipelineDeps,
) -> DownloadPayload:
    """번역 결과가 주입된 PDF를 저장한다.

    Args:
        file_path: 원본 PDF 경로.
        doc: 저장할 PDF 문서 객체.
        deps: 저장 단계에서 필요한 의존성 묶음.

    Returns:
        다운로드 payload.
    """

    base, extension = os.path.splitext(file_path)
    output_path = f"{base}_translated{extension}"
    doc.save(output_path, garbage=4, deflate=True)  # type: ignore[attr-defined]
    return deps.build_download_payload(output_path)
