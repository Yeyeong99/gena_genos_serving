"""PDF 파이프라인에서 사용하는 공통 타입 정의."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import aiohttp


ParsedData = List[dict]
PdfLineList = List[dict]
Pairs = List[dict]
TranslationMap = Dict[str, str]
DocumentLayout = List[dict]
DownloadPayload = Dict[str, str]

ExtractPdfFn = Callable[[str], ParsedData]
ExtractPdfLinesFn = Callable[[str], Tuple[Any, PdfLineList]]
EmitEventFn = Callable[..., Awaitable[None]]
ConvertPdfToTextFn = Callable[
    [asyncio.Semaphore, aiohttp.ClientSession, ParsedData],
    Awaitable[str],
]
TranslateLongTextFn = Callable[
    ...,
    Awaitable[str],
]
PolishPdfTranslationFn = Callable[
    ...,
    Awaitable[str],
]
BatchTranslateFn = Callable[
    ...,
    Awaitable[TranslationMap],
]
InjectPdfFn = Callable[[Any, PdfLineList, TranslationMap], None]
AssignNodeIdsFn = Callable[[PdfLineList], PdfLineList]
BuildTranslationPairsFn = Callable[[PdfLineList, TranslationMap], Pairs]
BuildDocumentLayoutFn = Callable[[PdfLineList], DocumentLayout]
BuildDownloadPayloadFn = Callable[[str], DownloadPayload]


@dataclass(slots=True)
class PdfParsedBundle:
    """PDF 텍스트 반환 모드에서 사용하는 추출 결과."""

    parsed_data: ParsedData


@dataclass(slots=True)
class PdfLineBundle:
    """PDF 주입 모드에서 사용하는 문서 객체와 line 묶음."""

    doc: Any
    lines: PdfLineList


@dataclass(slots=True)
class PdfTranslationArtifacts:
    """PDF line 번역 단계 결과를 묶는 구조체."""

    pairs: Pairs
    text: str
    trans_map: TranslationMap


@dataclass(slots=True)
class PdfPipelineDeps:
    """PDF 파이프라인이 외부에 의존하는 함수 집합."""

    extract_pdf: ExtractPdfFn
    extract_pdf_lines: ExtractPdfLinesFn
    emit_event: EmitEventFn
    convert_pdf_to_text_async: ConvertPdfToTextFn
    translate_long_text_async: TranslateLongTextFn
    polish_pdf_translation_async: PolishPdfTranslationFn
    batch_translate_async: BatchTranslateFn
    inject_pdf: InjectPdfFn
    assign_node_ids: AssignNodeIdsFn
    build_translation_pairs: BuildTranslationPairsFn
    build_document_layout: BuildDocumentLayoutFn
    build_download_payload: BuildDownloadPayloadFn
