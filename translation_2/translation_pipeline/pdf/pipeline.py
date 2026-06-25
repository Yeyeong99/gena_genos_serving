"""PDF 문서 파이프라인 orchestration 모듈."""

from __future__ import annotations

import asyncio

import aiohttp

from .extract import load_pdf_lines_for_injection, load_pdf_text_content
from .save import inject_translated_pdf, save_translated_pdf
from .translate import build_pdf_text_translation, translate_pdf_lines
from .types import PdfPipelineDeps


async def run_pdf_pipeline(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    file_path: str,
    target_lang: str,
    deps: PdfPipelineDeps,
    is_return_file: bool = False,
    callback_url: str = "",
    style_options: dict | None = None,
) -> dict:
    """PDF 문서 번역 파이프라인을 단계별로 실행한다.

    Args:
        sem: LLM 동시성 제어 세마포어.
        session: 번역 API 호출 세션.
        file_path: 입력 PDF 경로.
        target_lang: 대상 언어.
        deps: 단계별 의존성 묶음.
        is_return_file: 번역 파일 저장 여부.
        callback_url: 진행 상태 전송용 callback URL.

    Returns:
        PDF 번역 결과 딕셔너리.
    """

    await deps.emit_event("EXTRACT_START", callback_url)

    if is_return_file:
        bundle = load_pdf_lines_for_injection(file_path, deps)
        try:
            if not bundle.lines:
                return {"text": "[에러] PDF에서 번역 가능한 텍스트 line을 찾지 못했습니다."}

            unique_texts = list({line["text"] for line in bundle.lines})
            await deps.emit_event(
                "EXTRACT_DONE",
                callback_url,
                nodes=len(bundle.lines),
                unique=len(unique_texts),
            )

            await deps.emit_event("TRANSLATE_START", callback_url, unique=len(unique_texts))
            artifacts = await translate_pdf_lines(
                sem,
                session,
                bundle.lines,
                target_lang,
                deps,
                style_options=style_options,
            )
            await deps.emit_event("TRANSLATE_DONE", callback_url)

            await deps.emit_event("INJECT_START", callback_url)
            inject_translated_pdf(bundle.doc, bundle.lines, artifacts.trans_map, deps)
            await deps.emit_event("INJECT_DONE", callback_url)

            await deps.emit_event("SAVE_START", callback_url)
            download_payload = save_translated_pdf(file_path, bundle.doc, deps)
            await deps.emit_event("SAVE_DONE", callback_url)

            return {
                "text": artifacts.text,
                "pairs": artifacts.pairs,
                "document_blocks": deps.build_document_layout(bundle.lines),
                **download_payload,
            }
        finally:
            bundle.doc.close()  # type: ignore[attr-defined]

    parsed_bundle = load_pdf_text_content(file_path, deps)
    if not parsed_bundle.parsed_data:
        return {"text": "[에러] PDF에서 콘텐츠를 추출할 수 없습니다."}

    full_text = await deps.convert_pdf_to_text_async(sem, session, parsed_bundle.parsed_data)
    if not full_text.strip():
        return {"text": "[에러] PDF에서 텍스트를 추출할 수 없습니다."}

    await deps.emit_event("EXTRACT_DONE", callback_url, chars=len(full_text))
    await deps.emit_event("TRANSLATE_START", callback_url, chars=len(full_text))
    polished = await build_pdf_text_translation(
        sem,
        session,
        full_text,
        target_lang,
        deps,
        style_options=style_options,
    )
    await deps.emit_event("TRANSLATE_DONE", callback_url)
    await deps.emit_event("INJECT_START", callback_url)
    await deps.emit_event("INJECT_DONE", callback_url)
    await deps.emit_event("SAVE_START", callback_url)
    await deps.emit_event("SAVE_DONE", callback_url)
    return {"text": polished}
