"""실시간 텍스트 번역 엔트리포인트."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import aiohttp

from .llm import translate_single_async


async def run(data: Dict[str, Any]) -> Dict[str, Any]:
    """실시간 번역 요청을 처리한다.

    Args:
        data: 입력 텍스트와 대상 언어를 포함한 요청 데이터.

    Returns:
        번역 결과가 반영된 응답 데이터.
    """

    original_text = data.get("input_text", "")
    target_lang = data.get("format", "")
    style_options = data.get("style_options") if isinstance(data.get("style_options"), dict) else None

    data["input_text"] = original_text
    data["text"] = ""

    if not original_text or not str(original_text).strip():
        data["text"] = "[에러] text가 비어있습니다."
        return data

    if not target_lang:
        data["text"] = "[에러] format(번역 대상 언어)이 비어있습니다."
        return data

    try:
        sem = asyncio.Semaphore(1)
        async with aiohttp.ClientSession() as session:
            data["text"] = await translate_single_async(
                sem,
                session,
                str(original_text),
                str(target_lang),
                style_options,
            )
    except Exception as exc:
        print(f"번역 실패: {exc}")
        data["text"] = f"[에러] {exc}"

    return data
