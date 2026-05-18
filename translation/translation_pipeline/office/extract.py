"""Office 문서 추출 단계 모듈."""

from __future__ import annotations

from .types import OfficeDocumentBundle, OfficePipelineDeps


def load_office_document(
    file_path: str,
    ext: str,
    deps: OfficePipelineDeps,
) -> OfficeDocumentBundle:
    """Office 문서를 읽고 번역 노드와 preview bbox를 준비한다.

    Args:
        file_path: 입력 문서 경로.
        ext: 파일 확장자.
        deps: 추출 단계에서 필요한 의존성 묶음.

    Returns:
        문서 객체와 번역 노드를 포함한 번들.
    """

    extractor = deps.extractors.get(ext)
    if extractor is None:
        raise ValueError(f"지원하지 않는 Office 포맷: {ext}")

    obj, nodes = extractor(file_path)
    deps.assign_node_ids(nodes)
    deps.assign_preview_bboxes(nodes, ext)
    return OfficeDocumentBundle(obj=obj, nodes=nodes)

