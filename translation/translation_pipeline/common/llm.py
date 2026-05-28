"""파이프라인 공통 LLM 번역 런타임."""

from __future__ import annotations

from translation_pipeline.common.logging_utils import log_info

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import aiohttp
from dotenv import load_dotenv
from openai import APIStatusError, AsyncOpenAI, BadRequestError
from translation_pipeline.common.prompt_builder import (
    build_batch_user_prompt,
    build_single_user_prompt,
    get_single_translation_system_prompt,
    get_translation_system_prompt,
)
from translation_pipeline.common.validation import validate_translation_batch_response
from utils.pricing import record_llm_usage

# override 로딩은 쓰지 않는다 — .env.local.fullstack 의 빈 값 (예: 비활성된
# AZURE_STORAGE_CONNECTION_STRING) 이 export 된 정상 값을 덮어쓰는 회귀가 있었다.
load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env.local.fullstack", override=False)


LLM_CONCURRENCY = 15
MAX_CHARS_PER_BATCH = 4000
MAX_ITEMS_PER_BATCH = 10


class Config:
    """LLM 호출에 필요한 환경설정 묶음."""

    MODEL_API_BASE_URL = os.getenv("MODEL_API_BASE_URL", "").rstrip("/")
    MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", os.getenv("MODEL_NAME", "qwen/qwen3.5-397b-a17b"))
    DEFAULT_LIGHT_MODEL = os.getenv("DEFAULT_LIGHT_MODEL", DEFAULT_MODEL)
    DEFAULT_VLM_MODEL = os.getenv("DEFAULT_VLM_MODEL", DEFAULT_MODEL)
    DEFAULT_RESEARCH_MODEL = os.getenv("DEFAULT_RESEARCH_MODEL", DEFAULT_MODEL)
    DEFAULT_TRANSLATION_MODEL = os.getenv(
        "DEFAULT_TRANSLATION_MODEL",
        os.getenv("DEFAULT_LIGHT_MODEL", DEFAULT_MODEL),
    )
    GENOS_URL = os.getenv("GENOS_URL", "https://genos.genon.ai/api/gateway/")
    SERVING_ID = int(os.getenv("SERVING_ID", "676"))
    BEARER_TOKEN = os.getenv("BEARER_TOKEN", "5b48c081d00b4e58823b18b10849c802")
    MODEL_NAME = DEFAULT_TRANSLATION_MODEL
    RES_TIMEOUT = int(os.getenv("res_timeout", os.getenv("RES_TIMEOUT", "90")))
    LLM_RETRY_COUNT = int(os.getenv("LLM_RETRY_COUNT", "2"))
    MODEL_TEMP = float(os.getenv("MODEL_TEMP", "0.3"))
    MAX_TOKENS = int(os.getenv("MAX_TOKENS", os.getenv("GENOS_MAX_TOKENS", "16384")))
    LLM_API_PROVIDER_SORT = (
        os.getenv("OPENROUTER_PROVIDER_SORT") or os.getenv("LLM_API_PROVIDER_SORT") or "throughput"
    ).strip()
    OPENROUTER_ALLOW_FALLBACKS = (
        os.getenv("OPENROUTER_ALLOW_FALLBACKS", "").strip().lower() in {"1", "true", "yes", "on"}
    )
    OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL") or os.getenv("HTTP_REFERER")
    OPENROUTER_SITE_TITLE = os.getenv("OPENROUTER_SITE_TITLE") or os.getenv("X_TITLE")
    DISABLE_THINKING = (
        os.getenv("AI_TRANSLATION_DISABLE_THINKING", "0").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    DEEPSEEK_ID = int(os.getenv("DEEPSEEK_ID", "655"))
    DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "4cf827c97f32444bb4e34d3c7f22461c")
    DEEPSEEK_NAME = os.getenv("DEEPSEEK_NAME", "deepseek/deepseek-r1-0528")

    GPTOSS_ID = int(os.getenv("GPTOSS_ID", "589"))
    GPTOSS_KEY = os.getenv("GPTOSS_KEY", "01ae848e68be4314a9ca7d99abde139b")
    GPROSS_NAME = os.getenv("GPROSS_NAME", "openai/gpt-oss-120b")


select_model = 0
_LAST_LLM_ERROR = ""
_CLIENT: AsyncOpenAI | None = None


def _record_response_usage(model_name: str, response: Any) -> None:
    """OpenAI-compatible response.usage 를 번역 요청 컨텍스트에 누적한다."""

    usage = getattr(response, "usage", None)
    if usage is None and hasattr(response, "model_dump"):
        try:
            dumped = response.model_dump()
            usage = dumped.get("usage") if isinstance(dumped, dict) else None
        except Exception:
            usage = None
    if usage is None:
        return

    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    else:
        prompt_tokens = (
            getattr(usage, "prompt_tokens", None)
            or getattr(usage, "input_tokens", None)
            or 0
        )
        completion_tokens = (
            getattr(usage, "completion_tokens", None)
            or getattr(usage, "output_tokens", None)
            or 0
        )

    record_llm_usage(
        model_name,
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
    )


def _resolve_client() -> AsyncOpenAI:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    if Config.MODEL_API_BASE_URL and Config.MODEL_API_KEY:
        _CLIENT = AsyncOpenAI(
            base_url=Config.MODEL_API_BASE_URL,
            api_key=Config.MODEL_API_KEY,
            timeout=Config.RES_TIMEOUT,
        )
    else:
        _CLIENT = AsyncOpenAI(
            base_url=f"{Config.GENOS_URL.rstrip('/')}/rep/serving/{Config.SERVING_ID}/v1",
            api_key=Config.BEARER_TOKEN,
            timeout=Config.RES_TIMEOUT,
        )
    return _CLIENT


def _get_openrouter_options(existing: Dict[str, Any] | None = None) -> Dict[str, Any]:
    existing = existing or {}
    headers = dict(existing.get("extra_headers") or {})
    if Config.OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = Config.OPENROUTER_SITE_URL
    if Config.OPENROUTER_SITE_TITLE:
        headers["X-Title"] = Config.OPENROUTER_SITE_TITLE

    body = dict(existing.get("extra_body") or {})
    if Config.DISABLE_THINKING:
        chat_template_kwargs = dict(body.get("chat_template_kwargs") or {})
        chat_template_kwargs["enable_thinking"] = False
        body["chat_template_kwargs"] = chat_template_kwargs
    provider_obj = dict(body.get("provider") or {})
    if Config.LLM_API_PROVIDER_SORT:
        provider_obj["sort"] = Config.LLM_API_PROVIDER_SORT
    if Config.OPENROUTER_ALLOW_FALLBACKS:
        provider_obj["allow_fallbacks"] = True
    if provider_obj:
        body["provider"] = provider_obj

    options: Dict[str, Any] = {}
    if headers:
        options["extra_headers"] = headers
    if body:
        options["extra_body"] = body
    return options


def clear_last_llm_error() -> None:
    global _LAST_LLM_ERROR
    _LAST_LLM_ERROR = ""


def get_last_llm_error() -> str:
    return _LAST_LLM_ERROR


def _extract_response_error(response: Any) -> str:
    """OpenAI 호환 래퍼가 200 응답 안에 담아주는 error payload를 읽는다."""

    error = getattr(response, "error", None)
    if error is None and hasattr(response, "model_dump"):
        try:
            dumped = response.model_dump()
            if isinstance(dumped, dict):
                error = dumped.get("error")
        except Exception:
            error = None
    if not error:
        return ""
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or str(error)
        code = error.get("code")
        return f"{message} ({code})" if code else str(message)
    message = getattr(error, "message", None) or getattr(error, "detail", None)
    code = getattr(error, "code", None)
    if message:
        return f"{message} ({code})" if code else str(message)
    return str(error)


async def llm_call_async(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    system_prompt: str,
    user_text: str,
    image_base64: str | None = None,
) -> str:
    """LLM chat completion 호출을 수행한다.

    Args:
        sem: 동시성 제어 세마포어.
        session: HTTP 세션.
        system_prompt: 시스템 프롬프트.
        user_text: 사용자 입력 텍스트.
        image_base64: 멀티모달 입력용 base64 이미지.

    Returns:
        모델 응답 문자열. 실패 시 빈 문자열.
    """

    global _LAST_LLM_ERROR

    if not user_text:
        return ""

    if image_base64:
        model_name = Config.DEFAULT_VLM_MODEL
    elif select_model == 1:
        model_name = Config.DEFAULT_RESEARCH_MODEL
    elif select_model == 2:
        model_name = Config.DEFAULT_LIGHT_MODEL
    else:
        model_name = Config.DEFAULT_TRANSLATION_MODEL

    def extract_message_content(message_content: Any) -> str:
        if isinstance(message_content, str):
            return message_content.strip()
        if isinstance(message_content, list):
            parts: List[str] = []
            for item in message_content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text")
                    if text is not None:
                        parts.append(str(text))
            return "".join(parts).strip()
        if isinstance(message_content, dict):
            text = message_content.get("text")
            if text is not None:
                return str(text).strip()
        return ""

    if image_base64:
        user_content: Any = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_base64}"},
            },
        ]
    else:
        user_content = user_text

    kwargs: Dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "model": model_name,
        "temperature": Config.MODEL_TEMP,
    }
    if Config.MAX_TOKENS > 0:
        kwargs["max_tokens"] = Config.MAX_TOKENS
    try:
        kwargs.update(_get_openrouter_options(existing=kwargs))
    except Exception:
        pass

    retry_count = max(1, Config.LLM_RETRY_COUNT)
    client = _resolve_client()

    async with sem:
        for attempt in range(retry_count):
            try:
                response = await client.chat.completions.create(
                    timeout=Config.RES_TIMEOUT,
                    **kwargs,
                )
                response_error = _extract_response_error(response)
                if response_error:
                    _LAST_LLM_ERROR = response_error
                    raise RuntimeError(response_error)
                choice = response.choices[0] if response.choices else None
                message = getattr(choice, "message", None) if choice else None
                content = extract_message_content(getattr(message, "content", "") if message else "")
                if not content:
                    _LAST_LLM_ERROR = "LLM 응답에 choices/message.content가 없습니다."
                    raise RuntimeError(_LAST_LLM_ERROR)
                _record_response_usage(model_name, response)
                return content.replace("```json", "").replace("```", "").strip()
            except APIStatusError as exc:
                _LAST_LLM_ERROR = str(exc)
                if isinstance(exc, BadRequestError):
                    body = getattr(exc, "body", None)
                    message = body.get("message") if isinstance(body, dict) else str(body)
                    if "exceeds" not in message and "context length" not in message:
                        try:
                            retry_kwargs = {
                                key: value
                                for key, value in kwargs.items()
                                if key not in {"extra_headers", "extra_body"}
                            }
                            response = await client.chat.completions.create(
                                timeout=Config.RES_TIMEOUT,
                                **retry_kwargs,
                            )
                            choice = response.choices[0] if response.choices else None
                            message_obj = getattr(choice, "message", None) if choice else None
                            content = extract_message_content(
                                getattr(message_obj, "content", "") if message_obj else ""
                            )
                            if content:
                                _record_response_usage(model_name, response)
                                return content.replace("```json", "").replace("```", "").strip()
                        except Exception as retry_exc:
                            _LAST_LLM_ERROR = str(retry_exc)
                            exc = retry_exc
                if attempt < retry_count - 1:
                    log_info(f"   [LLM 재시도] {attempt + 1}/{retry_count} 실패: {exc!r}")
                    await asyncio.sleep(0.2)
                else:
                    log_info(f"   [LLM 실패] {retry_count}회 재시도 후 포기. 에러: {exc!r}")
                    return ""
            except Exception as exc:
                _LAST_LLM_ERROR = str(exc)
                if attempt < retry_count - 1:
                    log_info(f"   [LLM 재시도] {attempt + 1}/{retry_count} 실패: {exc!r}")
                    await asyncio.sleep(0.2)
                else:
                    log_info(f"   [LLM 실패] {retry_count}회 재시도 후 포기. 에러: {exc!r}")
                    return ""
    return ""


async def translate_single_async(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    text: str,
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
) -> str:
    """텍스트 한 건을 번역한다.

    Args:
        sem: 동시성 제어 세마포어.
        session: HTTP 세션.
        text: 번역할 텍스트.
        target_lang: 대상 언어.

    Returns:
        번역 결과. 실패 시 원문.
    """

    system = get_single_translation_system_prompt(target_lang)
    user_prompt = build_single_user_prompt(
        text,
        target_lang=target_lang,
        style_options=style_options,
    )
    result = await llm_call_async(sem, session, system, user_prompt)
    return result if result else text


async def batch_translate_async(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    unique_texts: List[str],
    target_lang: str,
    *,
    style_options: Dict[str, Any] | None = None,
    max_chars_per_batch: int = MAX_CHARS_PER_BATCH,
    max_items_per_batch: int = MAX_ITEMS_PER_BATCH,
) -> Dict[str, str]:
    """텍스트 목록을 배치로 번역한다.

    Args:
        sem: 동시성 제어 세마포어.
        session: HTTP 세션.
        unique_texts: 중복 제거된 원문 목록.
        target_lang: 대상 언어.

    Returns:
        원문/번역 매핑 딕셔너리.
    """

    if not unique_texts:
        return {}

    trans_map: Dict[str, str] = {}
    system_prompt = get_translation_system_prompt(target_lang, style_options)
    batches: List[List[Tuple[int, str]]] = []
    current_batch: List[Tuple[int, str]] = []
    current_chars = 0
    global_id = 0
    id_to_text: Dict[int, str] = {}

    for text in unique_texts:
        text_len = len(text)
        if current_batch and (
            current_chars + text_len > max_chars_per_batch or len(current_batch) >= max_items_per_batch
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append((global_id, text))
        id_to_text[global_id] = text
        global_id += 1
        current_chars += text_len

    if current_batch:
        batches.append(current_batch)

    log_info(f"[번역] {len(unique_texts)}개 텍스트 -> {len(batches)}개 배치")

    def normalize_batch_items(
        parsed_items: Any,
        batch: List[Tuple[int, str]],
    ) -> tuple[Dict[int, str], list[str]]:
        expected = {tid: text for tid, text in batch}
        validation = validate_translation_batch_response(parsed_items, expected)
        if validation.hard_errors:
            log_info(
                "[번역 응답 검증] hard validation failed: "
                + "; ".join(validation.hard_errors[:5])
            )
        if validation.soft_warnings:
            log_info(
                "[번역 응답 검증] warnings: "
                + "; ".join(validation.soft_warnings[:5])
            )
        return validation.normalized, validation.hard_errors

    def is_s_schema_only(parsed_items: Any) -> bool:
        if not isinstance(parsed_items, list) or not parsed_items:
            return False
        checked = 0
        s_only = 0
        for item in parsed_items:
            if not isinstance(item, dict) or "id" not in item:
                continue
            checked += 1
            if "t" not in item and "s" in item:
                s_only += 1
        return checked > 0 and s_only == checked

    async def fill_missing_ids_with_single(
        batch: List[Tuple[int, str]],
        partial: Dict[int, str],
    ) -> Dict[int, str]:
        missing = [(tid, text) for tid, text in batch if tid not in partial]
        for tid, text in missing:
            partial[tid] = await translate_single_async(
                sem,
                session,
                text,
                target_lang,
                style_options,
            )
        return partial

    async def process_batch(batch: List[Tuple[int, str]], retry: int = 0) -> Dict[int, str]:
        raw = await llm_call_async(sem, session, system_prompt, build_batch_user_prompt(batch))
        if not raw:
            if retry < 2:
                await asyncio.sleep(0.5)
                return await process_batch(batch, retry + 1)
            return await fill_missing_ids_with_single(batch, {})

        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception:
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(raw[start : end + 1])
                except Exception:
                    parsed = None

        if is_s_schema_only(parsed):
            return await fill_missing_ids_with_single(batch, {})

        normalized, hard_errors = normalize_batch_items(parsed, batch)
        if normalized and not hard_errors:
            return normalized

        if retry < 2:
            await asyncio.sleep(0.5)
            return await process_batch(batch, retry + 1)
        return await fill_missing_ids_with_single(batch, {})

    results = await asyncio.gather(*[process_batch(batch) for batch in batches])

    for batch_result in results:
        for tid, translated in batch_result.items():
            original = id_to_text.get(tid, "")
            if original:
                trans_map[original] = translated

    for text in unique_texts:
        if text not in trans_map:
            trans_map[text] = text

    return trans_map
