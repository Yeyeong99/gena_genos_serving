"""Office 문서 주입/저장 단계 모듈."""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import os
import tempfile
import zipfile

from .types import DownloadPayload, OfficePipelineDeps, Pairs


def inject_translated_office_document(
    ext: str,
    obj: object,
    nodes: list[dict],
    trans_map: dict[str, str],
    deps: OfficePipelineDeps,
) -> None:
    """번역 결과를 문서 객체에 주입한다.

    Args:
        ext: 파일 확장자.
        obj: 포맷별 문서 객체.
        nodes: 번역 노드 목록.
        trans_map: 원문/번역 매핑.
        deps: 주입 단계에서 필요한 의존성 묶음.

    Returns:
        없음.
    """

    injector = deps.injectors.get(ext)
    if injector is None:
        raise ValueError(f"지원하지 않는 Office 포맷: {ext}")

    injector(obj, nodes, trans_map)


def inject_edited_office_document(
    ext: str,
    obj: object,
    nodes: list[dict],
    _: dict[int, str],
    deps: OfficePipelineDeps,
) -> None:
    """사용자가 수정한 텍스트를 반영한 문서를 주입한다.

    Args:
        ext: 파일 확장자.
        obj: 포맷별 문서 객체.
        nodes: 수정 결과가 반영된 노드 목록.
        _: 인터페이스 일관성을 위한 더미 인자.
        deps: 주입 단계에서 필요한 의존성 묶음.

    Returns:
        없음.
    """

    injector = deps.injectors.get(ext)
    if injector is None:
        raise ValueError(f"지원하지 않는 Office 포맷: {ext}")

    injector(obj, nodes, {})


def save_translated_office_document(
    file_path: str,
    ext: str,
    obj: object,
    deps: OfficePipelineDeps,
) -> DownloadPayload:
    """번역이 주입된 Office 문서를 저장한다.

    Args:
        file_path: 원본 문서 경로.
        ext: 파일 확장자.
        obj: 포맷별 문서 객체.
        deps: 저장 단계에서 필요한 의존성 묶음.

    Returns:
        저장된 파일의 다운로드 payload.
    """

    base, extension = os.path.splitext(file_path)
    output_path = f"{base}_translated{extension}"
    _save_office_document(obj, ext, output_path, deps)
    return deps.build_download_payload(output_path)


def save_edited_office_document(
    file_path: str,
    ext: str,
    obj: object,
    deps: OfficePipelineDeps,
) -> DownloadPayload:
    """사용자 수정본이 주입된 Office 문서를 저장한다.

    Args:
        file_path: 원본 문서 경로.
        ext: 파일 확장자.
        obj: 포맷별 문서 객체.
        deps: 저장 단계에서 필요한 의존성 묶음.

    Returns:
        저장된 파일의 다운로드 payload.
    """

    base, extension = os.path.splitext(file_path)
    output_path = f"{base}_edited_translated{extension}"
    _save_office_document(obj, ext, output_path, deps)
    return deps.build_download_payload(output_path)


def apply_edited_pairs_to_pairs(
    pairs: Pairs,
    edited_text_by_id: dict[int, str],
) -> Pairs:
    """수정 텍스트를 translation pair에 반영한다.

    Args:
        pairs: 원문/번역 쌍 목록.
        edited_text_by_id: 노드 ID별 수정 텍스트.

    Returns:
        수정 텍스트가 반영된 원문/번역 쌍 목록.
    """

    for pair in pairs:
        if pair["id"] in edited_text_by_id:
            pair["translated"] = edited_text_by_id[pair["id"]]
    return pairs


def _save_office_document(
    obj: object,
    ext: str,
    output_path: str,
    deps: OfficePipelineDeps,
) -> None:
    """포맷에 맞는 저장 함수를 호출한다.

    Args:
        obj: 포맷별 문서 객체.
        ext: 파일 확장자.
        output_path: 저장할 파일 경로.
        deps: 저장 단계에서 필요한 의존성 묶음.

    Returns:
        없음.
    """

    if ext == ".docx":
        deps.save_docx(obj, output_path)
        return

    if ext == ".xlsx":
        try:
            calculation = getattr(obj, "calculation", None)
            if calculation is not None:
                calculation.calcMode = "auto"
                calculation.fullCalcOnLoad = True
                calculation.forceFullCalc = True
                calculation.calcOnSave = True
        except Exception:
            pass

    # openpyxl / python-pptx 객체는 save 메서드를 제공한다.
    obj.save(output_path)  # type: ignore[attr-defined]
    if ext == ".xlsx":
        _restore_xlsx_external_link_rels(obj, output_path)


def _restore_xlsx_external_link_rels(obj: object, output_path: str) -> None:
    """openpyxl 저장 중 깨질 수 있는 externalLink rels를 원본에서 복구한다."""

    source_path = getattr(obj, "_ai_translation_source_path", "")
    if not source_path or not os.path.exists(source_path) or not os.path.exists(output_path):
        return

    prefix = "xl/externalLinks/_rels/"
    try:
        with zipfile.ZipFile(source_path, "r") as source_zip:
            rel_payloads = {
                name: source_zip.read(name)
                for name in source_zip.namelist()
                if name.startswith(prefix) and name.endswith(".rels")
            }
        if not rel_payloads:
            return

        with zipfile.ZipFile(output_path, "r") as saved_zip:
            saved_items = saved_zip.infolist()
            saved_payloads = {item.filename: saved_zip.read(item.filename) for item in saved_items}

        if not any(name in saved_payloads for name in rel_payloads):
            return

        fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as target_zip:
                written = set()
                for item in saved_items:
                    data = rel_payloads.get(item.filename, saved_payloads[item.filename])
                    target_zip.writestr(item, data)
                    written.add(item.filename)
                for name, data in rel_payloads.items():
                    if name not in written:
                        target_zip.writestr(name, data)
            os.replace(temp_path, output_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except Exception as exc:
        log_info(f"[XLSX externalLink rels 복구] 실패 - 저장본은 유지: {exc}")
