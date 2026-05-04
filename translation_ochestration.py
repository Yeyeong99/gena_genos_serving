"""문서 번역 상위 엔트리포인트와 포맷 라우팅만 담당하는 모듈."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from typing import Any, Dict, Tuple

import aiohttp

from translation_pipeline.common.events import emit_event
from translation_pipeline.common.llm import (
    LLM_CONCURRENCY,
    batch_translate_async,
    translate_single_async,
)
from translation_pipeline.common.nodes import (
    assign_node_ids,
    assign_preview_bboxes,
    apply_node_translations,
    build_edited_text_by_id,
    build_document_layout,
    build_download_payload,
    build_translation_pairs,
)
from translation_pipeline.common.preview import (
    build_office_preview_payload,
    externalize_preview_payload,
)
from translation_pipeline.common.translation_jobs import get_translation_job
from translation_pipeline.office.runtime import (
    extract_docx,
    extract_pptx,
    extract_xlsx,
    inject_docx,
    inject_pptx,
    inject_xlsx,
    save_docx,
)
from translation_pipeline.office.pipeline import (
    revise_office_translation_job as modular_revise_office_translation_job,
    run_office_pipeline as modular_run_office_pipeline,
    save_edited_office_file as modular_save_edited_office_file,
    start_office_pipeline_job as modular_start_office_pipeline_job,
)
from translation_pipeline.office.types import OfficePipelineDeps
from translation_pipeline.pdf.pipeline import run_pdf_pipeline as modular_run_pdf_pipeline
from translation_pipeline.pdf.runtime import (
    convert_pdf_to_text_async,
    extract_pdf,
    extract_pdf_lines,
    inject_pdf,
    polish_pdf_translation_async,
    translate_long_text_async,
)
from translation_pipeline.pdf.types import PdfPipelineDeps


def _parse_is_return_file(value: Any) -> bool:
    """파일 저장 여부 입력값을 bool로 변환한다.

    Args:
        value: 사용자 입력 원본 값.

    Returns:
        파일 저장 여부.
    """

    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _resolve_file_input(file_value: str, filename: str) -> Tuple[str, str | None]:
    """파일 경로 또는 base64 입력을 실제 파일 경로로 정규화한다.

    Args:
        file_value: 파일 경로 또는 base64 문자열.
        filename: base64 입력 시 확장자 판별에 사용할 파일명.

    Returns:
        실제 처리에 사용할 파일 경로와 임시파일 경로.
    """

    normalized_value = file_value.strip()

    if os.path.isfile(normalized_value):
        return normalized_value, None

    expanded_path = os.path.expanduser(normalized_value)
    if os.path.isfile(expanded_path):
        return expanded_path, None

    path_like = (
        normalized_value.startswith(os.sep)
        or normalized_value.startswith("./")
        or normalized_value.startswith("../")
        or normalized_value.startswith("~")
        or (len(normalized_value) > 2 and normalized_value[1] == ":" and normalized_value[2] in ("\\", "/"))
    )

    base64_value = normalized_value
    if ";base64," in base64_value:
        base64_value = base64_value.split(";base64,", 1)[1]
    base64_value = "".join(base64_value.split())

    try:
        file_bytes = base64.b64decode(base64_value, validate=True)
    except Exception as exc:
        if path_like:
            raise FileNotFoundError(normalized_value) from exc
        raise ValueError(f"base64 디코딩 실패: {exc}") from exc

    if not file_bytes:
        if path_like:
            raise FileNotFoundError(normalized_value)
        raise ValueError("base64 디코딩 결과가 비어 있습니다.")

    ext = os.path.splitext(filename)[1].lower() if filename else ".bin"
    if not ext:
        ext = ".bin"

    fd, temp_path = tempfile.mkstemp(suffix=ext)
    with os.fdopen(fd, "wb") as file_handle:
        file_handle.write(file_bytes)
    return temp_path, temp_path


def _merge_result(data: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """파이프라인 결과를 응답 데이터에 병합한다.

    Args:
        data: 원본 요청/응답 데이터.
        result: 하위 파이프라인 실행 결과.

    Returns:
        병합된 응답 데이터.
    """

    merged: Dict[str, Any] = dict(data)
    merged["text"] = result.get("text", merged.get("text", ""))

    for key in (
        "job_id",
        "pairs",
        "translation_pairs",
        "translation_status",
        "translation_error",
        "translation_notice",
        "translation_skipped_reason",
        "document_blocks",
        "original_preview_images",
        "translated_preview_images",
        "translated_preview_job_id",
        "translated_preview_status",
        "preview_page_sizes",
        "preview_render_mode",
        "original_preview_html_url",
        "translated_preview_html_url",
        "file_path",
        "output_filename",
        "file_base64",
        "mime_type",
    ):
        if key in result:
            merged[key] = result[key]

    if "pairs" in result:
        merged["translation_pairs"] = result["pairs"]

    return merged


async def _run_plain_text_translation(
    plain_text: str,
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
) -> str:
    """플레인 텍스트 번역을 수행한다.

    Args:
        plain_text: 번역할 원문 텍스트.
        target_lang: 대상 언어.

    Returns:
        번역 결과 문자열.
    """

    sem = asyncio.Semaphore(1)
    session: aiohttp.ClientSession | None = None
    try:
        session = aiohttp.ClientSession()
        return await translate_single_async(sem, session, plain_text, target_lang, style_options)
    finally:
        if session and not session.closed:
            await session.close()


def _build_pdf_pipeline_deps() -> PdfPipelineDeps:
    """PDF 파이프라인에서 사용할 함수 의존성을 구성한다.

    Args:
        없음.

    Returns:
        단계별 PDF 파이프라인 의존성 객체.
    """

    return PdfPipelineDeps(
        extract_pdf=extract_pdf,
        extract_pdf_lines=extract_pdf_lines,
        emit_event=emit_event,
        convert_pdf_to_text_async=convert_pdf_to_text_async,
        translate_long_text_async=translate_long_text_async,
        polish_pdf_translation_async=polish_pdf_translation_async,
        batch_translate_async=batch_translate_async,
        inject_pdf=inject_pdf,
        assign_node_ids=assign_node_ids,
        build_translation_pairs=build_translation_pairs,
        build_document_layout=build_document_layout,
        build_download_payload=build_download_payload,
    )


def _build_office_pipeline_deps() -> OfficePipelineDeps:
    """Office 파이프라인에서 사용할 함수 의존성을 구성한다.

    Args:
        없음.

    Returns:
        단계별 Office 파이프라인 의존성 객체.
    """

    return OfficePipelineDeps(
        extractors={
            ".docx": extract_docx,
            ".xlsx": extract_xlsx,
            ".pptx": extract_pptx,
        },
        injectors={
            ".docx": inject_docx,
            ".xlsx": inject_xlsx,
            ".pptx": inject_pptx,
        },
        emit_event=emit_event,
        batch_translate_async=batch_translate_async,
        assign_node_ids=assign_node_ids,
        assign_preview_bboxes=assign_preview_bboxes,
        build_translation_pairs=build_translation_pairs,
        apply_node_translations=apply_node_translations,
        build_office_preview_payload=build_office_preview_payload,
        externalize_preview_payload=externalize_preview_payload,
        build_document_layout=build_document_layout,
        save_docx=save_docx,
        build_download_payload=build_download_payload,
        build_edited_text_by_id=build_edited_text_by_id,
    )


async def _run_file_translation(
    data: Dict[str, Any],
    file_path: str,
    target_lang: str,
    translator_mode: str | None,
    is_return_file: bool,
    callback_url: str,
    preview_output_dir: str,
    preview_base_url: str,
    style_options: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """파일 번역 파이프라인을 확장자에 따라 분기 실행한다.

    Args:
        data: 원본 요청/응답 데이터.
        file_path: 실제 처리할 파일 경로.
        target_lang: 대상 언어.
        translator_mode: 번역기 모드.
        is_return_file: 파일 저장 여부.
        callback_url: 진행 상태 콜백 URL.
        preview_output_dir: preview 파일 저장 디렉터리.
        preview_base_url: preview 파일 접근 base URL.

    Returns:
        병합된 응답 데이터.
    """

    extension = os.path.splitext(file_path)[1].lower()
    semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
    session: aiohttp.ClientSession | None = None

    try:
        session = aiohttp.ClientSession()
        if extension in (".docx", ".xlsx", ".pptx"):
            result = await modular_run_office_pipeline(
                semaphore,
                session,
                file_path,
                extension,
                target_lang,
                _build_office_pipeline_deps(),
                translator_mode=translator_mode,
                style_options=style_options,
                is_return_file=is_return_file,
                callback_url=callback_url,
                preview_output_dir=preview_output_dir,
                preview_base_url=preview_base_url,
            )
            return _merge_result(data, result)

        if extension == ".pdf":
            result = await modular_run_pdf_pipeline(
                semaphore,
                session,
                file_path,
                target_lang,
                _build_pdf_pipeline_deps(),
                is_return_file=is_return_file,
                callback_url=callback_url,
                style_options=style_options,
            )
            return _merge_result(data, result)

        return {
            **data,
            "text": f"[에러] 지원하지 않는 파일 형식: {extension}",
        }
    finally:
        if session and not session.closed:
            await session.close()


async def _run_existing_job_download(
    data: Dict[str, Any],
    job_id: str,
    edited_pairs: list[dict],
    preview_output_dir: str,
    preview_base_url: str,
) -> Dict[str, Any]:
    """이미 번역/주입된 job 파일을 기준으로 빠른 다운로드 payload를 만든다."""

    job = get_translation_job(job_id)
    payload = job.get("payload", {}) if job else {}
    file_path = payload.get("_translated_file_path")
    extension = payload.get("_translated_file_ext") or (
        os.path.splitext(str(file_path))[1].lower() if file_path else ""
    )

    if not file_path or not os.path.exists(str(file_path)):
        return {
            **data,
            "text": "[에러] 저장할 번역 완료 파일을 찾을 수 없습니다. 번역 완료 후 다시 시도해 주세요.",
        }

    if extension not in (".docx", ".xlsx", ".pptx"):
        return {**data, "text": f"[에러] 빠른 저장을 지원하지 않는 파일 형식: {extension}"}

    deps = _build_office_pipeline_deps()
    if not edited_pairs:
        return {
            **data,
            "text": payload.get("text", ""),
            **deps.build_download_payload(str(file_path)),
        }

    result = await modular_save_edited_office_file(
        str(file_path),
        extension,
        edited_pairs,
        deps,
        preview_output_dir=preview_output_dir,
        preview_base_url=preview_base_url,
        include_preview=False,
    )
    return _merge_result(data, result)


async def run(data: Dict[str, Any]) -> Dict[str, Any]:
    """문서 번역 상위 엔트리포인트를 실행한다.

    Args:
        data: 번역 요청 데이터.

    Returns:
        번역 결과가 반영된 응답 데이터.
    """

    target_lang = data.get("format", "")
    file_value = data.get("file", "")
    plain_text = data.get("input_text", "")
    filename = data.get("filename") or data.get("file_name", "unknown.txt")
    is_return_file = _parse_is_return_file(data.get("is_return_file", False))
    edited_pairs = data.get("edited_translation_pairs") or []
    job_id = str(data.get("job_id") or "")
    translator_mode = data.get("translator_mode")
    style_options = data.get("style_options") if isinstance(data.get("style_options"), dict) else None
    callback_url = data.get("callback_url", "")
    preview_output_dir = data.get("_preview_output_dir", "")
    preview_base_url = data.get("_preview_base_url", "")

    if not target_lang:
        return {**data, "text": "[에러] format(번역 대상 언어)이 비어있습니다."}

    if is_return_file and job_id:
        job_download_result = await _run_existing_job_download(
            data,
            job_id,
            edited_pairs if isinstance(edited_pairs, list) else [],
            preview_output_dir,
            preview_base_url,
        )
        if job_download_result.get("file_base64") or not file_value:
            return job_download_result

    if not file_value and not plain_text:
        return {**data, "text": "[에러] file 또는 input_text가 비어있습니다."}

    if plain_text and not file_value:
        try:
            translated_text = await _run_plain_text_translation(plain_text, target_lang, style_options)
            return {**data, "text": translated_text}
        except Exception as exc:
            return {**data, "text": f"[에러] 처리 실패: {exc}"}

    temp_path: str | None = None
    try:
        try:
            file_path, temp_path = _resolve_file_input(file_value, filename)
        except FileNotFoundError:
            return {**data, "text": f"[에러] 파일을 찾을 수 없습니다: {file_value}"}
        except ValueError as exc:
            return {**data, "text": f"[에러] {exc}"}

        return await _run_file_translation(
            data=data,
            file_path=file_path,
            target_lang=target_lang,
            translator_mode=translator_mode,
            is_return_file=is_return_file,
            callback_url=callback_url,
            preview_output_dir=preview_output_dir,
            preview_base_url=preview_base_url,
            style_options=style_options,
        )
    except Exception as exc:
        await emit_event("ERROR", callback_url, detail=str(exc)[:200])
        return {**data, "text": f"[에러] 처리 실패: {exc}"}
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


async def start_streaming(data: Dict[str, Any]) -> Dict[str, Any]:
    """원본 preview 선표시 + 백그라운드 번역/SSE용 시작 엔트리포인트."""

    target_lang = data.get("format", "")
    file_value = data.get("file", "")
    filename = data.get("filename") or data.get("file_name", "unknown.txt")
    translator_mode = data.get("translator_mode")
    style_options = data.get("style_options") if isinstance(data.get("style_options"), dict) else None
    preview_output_dir = data.get("_preview_output_dir", "")
    preview_base_url = data.get("_preview_base_url", "")

    if not target_lang:
        return {**data, "text": "[에러] format(번역 대상 언어)이 비어있습니다."}
    if not file_value:
        return {**data, "text": "[에러] file이 비어있습니다."}

    try:
        try:
            file_path, temp_path = _resolve_file_input(file_value, filename)
        except FileNotFoundError:
            return {**data, "text": f"[에러] 파일을 찾을 수 없습니다: {file_value}"}
        except ValueError as exc:
            return {**data, "text": f"[에러] {exc}"}

        extension = os.path.splitext(file_path)[1].lower()
        if extension not in (".docx", ".xlsx", ".pptx"):
            return {**data, "text": f"[에러] 스트리밍 시작은 Office 형식만 지원합니다: {extension}"}

        result = await modular_start_office_pipeline_job(
            file_path=file_path,
            ext=extension,
            target_lang=target_lang,
            deps=_build_office_pipeline_deps(),
            translator_mode=translator_mode,
            style_options=style_options,
            preview_output_dir=preview_output_dir,
            preview_base_url=preview_base_url,
            cleanup_path=temp_path,
        )
        return _merge_result(data, result)
    except Exception as exc:
        return {**data, "text": f"[에러] 처리 실패: {exc}"}


async def revise_translation(data: Dict[str, Any]) -> Dict[str, Any]:
    """완료된 translation job을 기준으로 수정 번역을 수행한다."""

    job_id = str(data.get("job_id") or "")
    target_lang = data.get("format", "")
    scope = data.get("scope") if isinstance(data.get("scope"), dict) else None
    style_options = data.get("style_options") if isinstance(data.get("style_options"), dict) else None
    instruction = str(data.get("instruction") or "")
    translator_mode = data.get("translator_mode")
    preview_output_dir = data.get("_preview_output_dir", "")
    preview_base_url = data.get("_preview_base_url", "")

    if not job_id:
        return {**data, "text": "[에러] job_id가 비어있습니다."}

    try:
        result = await modular_revise_office_translation_job(
            job_id,
            scope,
            target_lang,
            _build_office_pipeline_deps(),
            translator_mode=translator_mode,
            style_options=style_options,
            instruction=instruction,
            preview_output_dir=preview_output_dir,
            preview_base_url=preview_base_url,
        )
        return _merge_result(data, result)
    except Exception as exc:
        return {**data, "text": f"[에러] 수정 번역 실패: {exc}", "translation_error": str(exc)}
