"""Office 파이프라인에서 사용하는 공통 타입 정의."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import aiohttp


NodeList = List[dict]
TranslationMap = Dict[str, str]
PreviewPayload = Dict[str, Any]
DownloadPayload = Dict[str, str]
Pairs = List[dict]
DocumentLayout = List[dict]

ExtractorFn = Callable[[str], Tuple[Any, NodeList]]
InjectorFn = Callable[[Any, NodeList, TranslationMap], None]
EmitEventFn = Callable[..., Awaitable[None]]
BatchTranslateFn = Callable[..., Awaitable[TranslationMap]]
AssignNodeIdsFn = Callable[[NodeList], None]
AssignPreviewBboxesFn = Callable[[NodeList, str], None]
BuildTranslationPairsFn = Callable[[NodeList, TranslationMap], Pairs]
ApplyNodeTranslationsFn = Callable[..., None]
BuildOfficePreviewPayloadFn = Callable[[str, NodeList, str], PreviewPayload]
ExternalizePreviewPayloadFn = Callable[[PreviewPayload, str, str], PreviewPayload]
BuildDocumentLayoutFn = Callable[[NodeList], DocumentLayout]
SaveDocxFn = Callable[[Any, str], None]
BuildDownloadPayloadFn = Callable[[str], DownloadPayload]
BuildEditedTextByIdFn = Callable[[List[dict]], Dict[int, str]]


@dataclass(slots=True)
class OfficeDocumentBundle:
    """추출 단계에서 사용하는 문서 객체와 노드 묶음."""

    obj: Any
    nodes: NodeList


@dataclass(slots=True)
class InjectionUnit:
    """문서에 다시 주입할 최소 단위."""

    injection_unit_id: int
    node_id: int
    text: str
    node: dict
    source: str = ""
    group: str = ""
    node_type: str = ""
    doc_format: str = ""
    table_index: int | None = None
    bbox: List[int] | None = None
    slide_index: int | None = None
    sheet_name: str = ""
    row: int | None = None
    col: int | None = None
    shape_name: str = ""
    page_num: int | None = None
    element_type: str = ""
    is_header: bool = False


@dataclass(slots=True)
class TranslationTarget:
    """번역 단위가 매핑될 주입 단위의 세부 위치."""

    injection_unit_id: int
    fragment_index: int = 0
    fragment_count: int = 1


@dataclass(slots=True)
class TranslationUnit:
    """외부 번역기에 전달할 최소 단위."""

    translation_unit_id: int
    text: str
    targets: List[TranslationTarget]
    context_scope: str = ""
    context_text: str = ""
    element_type: str = ""


@dataclass(slots=True)
class ResolvedInjection:
    """번역 결과가 반영된 주입 단위."""

    injection_unit_id: int
    node_id: int
    original_text: str
    translated_text: str
    translated_fragments: List[str] | None = None


@dataclass(slots=True)
class OfficeTranslationArtifacts:
    """번역 단계 결과를 묶어서 전달하기 위한 구조체."""

    pairs: Pairs
    text: str
    trans_map: TranslationMap
    injection_units: List[InjectionUnit]
    translation_units: List[TranslationUnit]
    translated_by_unit_id: Dict[int, str]
    resolved_injections: List[ResolvedInjection]
    translation_error: str = ""
    temporary_glossary: Dict[str, Any] | None = None
    pre_translation_analysis: Dict[str, Any] | None = None
    document_term_memory: Dict[str, Any] | None = None


@dataclass(slots=True)
class OfficePipelineDeps:
    """Office 파이프라인이 외부에 의존하는 함수 집합."""

    extractors: Dict[str, ExtractorFn]
    injectors: Dict[str, InjectorFn]
    emit_event: EmitEventFn
    batch_translate_async: BatchTranslateFn
    assign_node_ids: AssignNodeIdsFn
    assign_preview_bboxes: AssignPreviewBboxesFn
    build_translation_pairs: BuildTranslationPairsFn
    apply_node_translations: ApplyNodeTranslationsFn
    build_office_preview_payload: BuildOfficePreviewPayloadFn
    externalize_preview_payload: ExternalizePreviewPayloadFn
    build_document_layout: BuildDocumentLayoutFn
    save_docx: SaveDocxFn
    build_download_payload: BuildDownloadPayloadFn
    build_edited_text_by_id: BuildEditedTextByIdFn
