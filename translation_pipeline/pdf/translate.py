"""PDF 문서 번역 단계 모듈."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .types import PdfPipelineDeps, PdfTranslationArtifacts


async def build_pdf_text_translation(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    full_text: str,
    target_lang: str,
    deps: PdfPipelineDeps,
    style_options: dict[str, Any] | None = None,
) -> str:
    """PDF 텍스트 반환 모드 번역 결과를 생성한다.

    Args:
        sem: LLM 동시성 제어 세마포어.
        session: 번역 API 호출 세션.
        full_text: PDF에서 추출을 마친 원문 텍스트.
        target_lang: 대상 언어.
        deps: 번역 단계에서 필요한 의존성 묶음.

    Returns:
        후처리까지 완료된 번역 문자열.
    """

    if not full_text.strip():
        return ""

    translated = await deps.translate_long_text_async(
        sem,
        session,
        full_text,
        target_lang,
        style_options=style_options,
    )
    return await deps.polish_pdf_translation_async(
        sem,
        session,
        translated,
        target_lang,
        style_options=style_options,
    )


async def translate_pdf_lines(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    lines: list[dict],
    target_lang: str,
    deps: PdfPipelineDeps,
    style_options: dict[str, Any] | None = None,
) -> PdfTranslationArtifacts:
    """PDF line 목록을 배치 번역하고 주입 전 산출물을 만든다.

    Args:
        sem: LLM 동시성 제어 세마포어.
        session: 번역 API 호출 세션.
        lines: 번역 대상 PDF line 목록.
        target_lang: 대상 언어.
        deps: 번역 단계에서 필요한 의존성 묶음.

    Returns:
        번역 맵, 원문/번역 쌍, 표시용 텍스트를 포함한 결과.
    """

    unique_texts = list({line["text"] for line in lines})
    trans_map = await deps.batch_translate_async(
        sem,
        session,
        unique_texts,
        target_lang,
        style_options=style_options,
    )
    pairs = deps.build_translation_pairs(lines, trans_map)
    text = "\n".join(trans_map.get(line["text"], line["text"]) for line in lines)
    return PdfTranslationArtifacts(
        pairs=pairs,
        text=text,
        trans_map=trans_map,
    )
