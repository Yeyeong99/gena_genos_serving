"""PDF 문서 추출 단계 모듈."""

from __future__ import annotations

from .types import PdfLineBundle, PdfParsedBundle, PdfPipelineDeps


def load_pdf_text_content(
    file_path: str,
    deps: PdfPipelineDeps,
) -> PdfParsedBundle:
    """PDF 텍스트 반환 모드에 필요한 추출 결과를 준비한다.

    Args:
        file_path: 입력 PDF 경로.
        deps: 추출 단계에서 필요한 의존성 묶음.

    Returns:
        PDF 파싱 결과를 담은 번들.
    """

    parsed_data = deps.extract_pdf(file_path)
    return PdfParsedBundle(parsed_data=parsed_data)


def load_pdf_lines_for_injection(
    file_path: str,
    deps: PdfPipelineDeps,
) -> PdfLineBundle:
    """PDF 번역 주입 모드에 필요한 line 정보를 준비한다.

    Args:
        file_path: 입력 PDF 경로.
        deps: 추출 단계에서 필요한 의존성 묶음.

    Returns:
        PDF 문서 객체와 line 목록을 포함한 번들.
    """

    doc, lines = deps.extract_pdf_lines(file_path)
    deps.assign_node_ids(lines)
    return PdfLineBundle(doc=doc, lines=lines)
