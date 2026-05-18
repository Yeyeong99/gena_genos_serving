"""파이프라인 공통 LLM 번역 런타임."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import aiohttp
from dotenv import load_dotenv
from openai import APIStatusError, AsyncOpenAI, BadRequestError

# override 로딩은 쓰지 않는다 — .env.local.fullstack 의 빈 값 (예: 비활성된
# AZURE_STORAGE_CONNECTION_STRING) 이 export 된 정상 값을 덮어쓰는 회귀가 있었다.
load_dotenv()


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
                                return content.replace("```json", "").replace("```", "").strip()
                        except Exception as retry_exc:
                            _LAST_LLM_ERROR = str(retry_exc)
                            exc = retry_exc
                if attempt < retry_count - 1:
                    print(f"   [LLM 재시도] {attempt + 1}/{retry_count} 실패: {exc!r}")
                    await asyncio.sleep(0.2)
                else:
                    print(f"   [LLM 실패] {retry_count}회 재시도 후 포기. 에러: {exc!r}")
                    return ""
            except Exception as exc:
                _LAST_LLM_ERROR = str(exc)
                if attempt < retry_count - 1:
                    print(f"   [LLM 재시도] {attempt + 1}/{retry_count} 실패: {exc!r}")
                    await asyncio.sleep(0.2)
                else:
                    print(f"   [LLM 실패] {retry_count}회 재시도 후 포기. 에러: {exc!r}")
                    return ""
    return ""


def build_translation_style_instruction(
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
) -> str:
    """프론트 번역 스타일 선택값을 LLM 지시문으로 변환한다."""

    if not isinstance(style_options, dict) or not style_options:
        return ""

    purpose_map = {
        "presentation": "Adapt the translation for presentation use: concise, easy to scan, and natural when read on slides.",
        "casual_use": "Adapt the translation for everyday use: clear, approachable, and easy for general readers to understand.",
        "business": "Adapt the translation for business use: professional, polished, and suitable for workplace documents.",
    }
    legacy_tone_map = {
        "report": "Adapt the translation for business use: polished and suitable for reports or formal documents.",
        "presentation": purpose_map["presentation"],
        "formal": "Use a formal and respectful register.",
        "natural": "Use natural, fluent wording.",
        "native_natural": "Use natural native-speaker wording.",
        "concise": "Keep wording concise without dropping meaning.",
        "business": purpose_map["business"],
        "polite": "Use polite wording.",
    }
    formality_map = {
        "formal_hamnida": "For Korean, use formal polite 다나까-style endings consistently: '-합니다'/'-습니다' for statements and '-합니까'/'-습니까' for questions. Avoid casual 해요체 endings such as '-요' except inside direct quotations. For other target languages, use a formal and respectful register.",
        "plain_declarative": "For Korean, use written declarative endings such as '-이다' and '-했다'. For other target languages, use a neutral written style.",
        "informal_friendly": "For Korean, use a friendly conversational 해요체 style. Prefer natural '-요' endings for direct address, questions, recommendations, and calls to action, but mix neutral '-다' endings for headlines, detached factual statements, or places where '-요' would sound forced. Avoid formal 다나까-style endings such as '-습니다'/'-습니까' unless they appear in direct quotations or fixed source text. For other target languages, use friendly, conversational wording without becoming sloppy or overly casual.",
        "eum_ham": "For Korean, use compact report-style nominal phrasing. The core style is NOT simply ending every sentence with '-음' or '-함'. Prefer concise nominalized or noun-phrase endings whenever possible: e.g. '좋은 아침입니다.' -> '좋은 아침.'; '세계 최강의 미국인 두 명—교황과 대통령—이 충돌하고 있습니다.' -> '세계 최강의 미국인 두 명—교황과 대통령—이 충돌 중.'; '방문은 감동을 주기 위한 것이었습니다.' -> '방문은 감동을 주기 위한 것.'; '이곳을 방문하고 있습니다.' -> '이곳을 방문.' Use '-음'/'-함' endings when they are natural, but also use noun phrases, '-중', '-것', and compact fragments. Do not force long narrative sentences into awkward '-음'/'-함'. Avoid polite endings such as '-습니다' and casual 해요체 endings such as '-요' except inside direct quotations. For other target languages, use concise note-style phrasing.",
    }
    legacy_ending_map = {
        "hamnida": formality_map["formal_hamnida"],
        "haetseumnida": "For Korean, use polite past-tense endings such as '-했습니다' where appropriate.",
        "nominal": "For Korean, prefer nominalized endings suitable for reports.",
        "eum_ham": formality_map["eum_ham"],
    }
    terminology_map = {
        "preserve_key_terms": "Preserve key technical terms, product names, and proper nouns in the source language when natural.",
        "natural_translation": "Translate terminology naturally for the target-language reader.",
        "technical_terms": "Prefer precise technical terminology over casual paraphrases.",
    }
    script_map = {
        "simplified": "For Chinese, use Simplified Chinese.",
        "traditional": "For Chinese, use Traditional Chinese.",
    }

    target = (target_lang or "").lower()
    instructions: list[str] = []
    purpose = str(style_options.get("purpose") or "")
    if purpose and purpose != "default" and purpose in purpose_map:
        instructions.append(purpose_map[purpose])

    formality = str(style_options.get("formality") or "")
    if formality and formality in formality_map:
        instructions.append(formality_map[formality])

    legacy_tone = str(style_options.get("tone") or "")
    if not purpose and legacy_tone and legacy_tone != "default" and legacy_tone in legacy_tone_map:
        instructions.append(legacy_tone_map[legacy_tone])

    legacy_ending = str(style_options.get("ending") or "")
    if not formality and legacy_ending and ("korean" in target or "한국" in target) and legacy_ending in legacy_ending_map:
        instructions.append(legacy_ending_map[legacy_ending])

    terminology = str(style_options.get("terminology") or "")
    if terminology and terminology in terminology_map:
        instructions.append(terminology_map[terminology])

    script = str(style_options.get("script") or "")
    if script and ("chinese" in target or "중국" in target) and script in script_map:
        instructions.append(script_map[script])

    revision_instruction = str(style_options.get("_revision_instruction") or "").strip()
    if revision_instruction:
        instructions.append(
            "This is a revision pass for an already translated document. "
            "Prioritize this user revision instruction over the default translation style when they conflict. "
            "Apply it as an editing instruction to the previous translation when previous_t/PREVIOUS_TRANSLATION is provided; "
            "this may include casing changes, wording replacements, shortening, tone changes, or terminology changes, "
            "not only re-translation from the source text. User revision instruction: "
            f"{revision_instruction}"
        )

    if not instructions:
        return ""
    return "\n\nTRANSLATION STYLE REQUIREMENTS:\n" + "\n".join(
        f"- {item}" for item in instructions
    )


_TARGET_ALIAS_TO_CANONICAL = {
    "ko": "Korean",
    "kor": "Korean",
    "korean": "Korean",
    "한국어": "Korean",
    "en": "English",
    "eng": "English",
    "english": "English",
    "영어": "English",
    "ja": "Japanese",
    "jp": "Japanese",
    "jpn": "Japanese",
    "japanese": "Japanese",
    "일본어": "Japanese",
    "zh": "Chinese",
    "cn": "Chinese",
    "chi": "Chinese",
    "zho": "Chinese",
    "chinese": "Chinese",
    "중국어": "Chinese",
}


def _resolve_target_label(target_lang: str) -> Tuple[str, str, str]:
    """Return (display_label, canonical, raw) where canonical is one of
    Korean/English/Japanese/Chinese or "" for unknown labels."""

    raw = (target_lang or "").strip()
    if not raw:
        return ("the target language", "", "")
    canonical = _TARGET_ALIAS_TO_CANONICAL.get(raw.lower(), "")
    display = canonical or raw
    return (display, canonical, raw)


def build_target_language_guard(target_lang: str) -> str:
    """대상 언어 외 문자/언어 혼입을 막는 공통 지시문을 생성한다.

    타겟 언어별로 분기된 가드를 만들어 시스템 프롬프트에 항상
    "Translate fragments fully into Korean" 지시가 누설되던 회귀를 차단한다.
    한자 위주 한국어 어휘가 "이미 일본어/중국어" 로 오판되어 그대로 남던
    누락도 일본어/중국어 타겟에서는 명시 절로 강제 번역한다.
    """

    display, canonical, raw = _resolve_target_label(target_lang)
    raw_suffix = f" (target requested as: {raw})" if raw and raw != display else ""

    base = (
        f"MUST ONLY USE {display}{raw_suffix}. DO NOT USE OTHER LANGUAGES in the translated output. "
        f"Every translated value must be written in {display}, except for numbers, symbols, URLs, formulas, file paths, email addresses, and proper nouns or technical terms that are explicitly preserved by the terminology option. "
        "Do not leave accidental mixed-language fragments from the source or model output. "
    )

    if canonical == "Korean":
        return base + (
            "If the output language is Korean, Chinese/Japanese text, Hanja, Kanji, Kana, or mixed-script fragments such as '图中', '何处', '历来', or '話を' MUST NOT BE INCLUDED unless they are part of an explicitly preserved proper noun or technical term. Translate those fragments fully into Korean. "
            "Apply the same rule for foreign-language fragments unrelated to Korean: do not include them unless explicitly preserved as proper nouns or technical terms."
        )

    if canonical == "Japanese":
        return base + (
            "Korean (Hangul) script MUST NOT BE INCLUDED unless explicitly preserved as a proper noun or technical term. Translate any Korean fragments fully into Japanese. "
            "Korean lexical items written mostly in Hanja that look superficially Japanese (for example, '業務管理', '技術部門', '經濟成長') ARE STILL KOREAN and MUST STILL BE TRANSLATED into natural Japanese — do NOT return them unchanged just because the characters overlap with Kanji."
        )

    if canonical == "Chinese":
        return base + (
            "Korean (Hangul) script MUST NOT BE INCLUDED unless explicitly preserved as a proper noun or technical term. Translate any Korean fragments fully into Chinese. "
            "Korean lexical items written mostly in Hanja that look superficially Chinese (for example, '業務管理', '技術部門', '經濟成長') ARE STILL KOREAN and MUST STILL BE TRANSLATED into natural Chinese — do NOT return them unchanged just because the characters overlap with Han."
        )

    if canonical == "English":
        return base + (
            "Korean (Hangul), Japanese (Kana), and Chinese/Japanese-only Han fragments MUST NOT BE INCLUDED unless explicitly preserved as a proper noun or technical term. Translate any non-English fragments fully into English."
        )

    # Unknown target label — keep the generic guard but never force Korean output.
    return base + (
        f"Apply the same rule for foreign-language fragments unrelated to {display}: do not include them unless explicitly preserved as proper nouns or technical terms."
    )


def get_translation_system_prompt(
    target_lang: str,
    style_options: Dict[str, Any] | None = None,
) -> str:
    """배치 번역용 시스템 프롬프트를 생성한다.

    Args:
        target_lang: 대상 언어.

    Returns:
        배치 번역용 시스템 프롬프트 문자열.
    """

    style_instruction = build_translation_style_instruction(target_lang, style_options)
    language_guard = build_target_language_guard(target_lang)
    return f"""You are a professional document translator.
Translate the given text into {target_lang} naturally and accurately, following the translation purpose, style, and terminology requirements below when provided.

CRITICAL RULES:
1. Preserve ALL numbers, currency symbols, percentages, dates, and units EXACTLY as-is.
2. Preserve ALL proper nouns (company names, person names, place names, ticker symbols) EXACTLY as-is.
3. Preserve ALL URLs, email addresses, and file paths EXACTLY as-is.
4. Preserve ALL mathematical expressions and formulas EXACTLY as-is.
5. Preserve the original meaning and intent; adapt tone/register only as required by the selected translation options.
6. Do NOT add explanations, notes, or commentary.
7. If the input is already in {target_lang}, return it unchanged.
8. {language_guard}
9. Keep each "id" unchanged and put translated text in key "t". Never use key "s" in output.

You will receive a JSON array of texts to translate.
Return a JSON array with the same structure: [{{"id": 0, "t": "translated text"}}, ...]
Return ONLY the JSON array, no other text.{style_instruction}"""


def build_batch_user_prompt(texts_with_ids: List[Tuple[int, str]]) -> str:
    """배치 번역용 사용자 프롬프트를 생성한다.

    Args:
        texts_with_ids: ID와 원문 텍스트 목록.

    Returns:
        JSON 문자열 프롬프트.
    """

    items = [{"id": tid, "s": text} for tid, text in texts_with_ids]
    return json.dumps(items, ensure_ascii=False)


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

    system = (
        f"You are a professional translator. "
        f"Translate the following text into {target_lang} naturally and accurately, "
        "following the selected translation purpose, style, and terminology requirements when provided. "
        f"Return ONLY the translated text."
        f"{build_translation_style_instruction(target_lang, style_options)}"
    )
    result = await llm_call_async(sem, session, system, text)
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

    print(f"[번역] {len(unique_texts)}개 텍스트 -> {len(batches)}개 배치")

    def normalize_batch_items(parsed_items: Any) -> Dict[int, str]:
        normalized: Dict[int, str] = {}
        if not isinstance(parsed_items, list):
            return normalized
        for item in parsed_items:
            if not isinstance(item, dict) or "id" not in item or "t" not in item:
                continue
            try:
                tid = int(item["id"])
            except (TypeError, ValueError):
                continue
            if tid in id_to_text:
                normalized[tid] = str(item["t"])
        return normalized

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

        normalized = normalize_batch_items(parsed)
        if normalized:
            if len(normalized) < len(batch):
                return await fill_missing_ids_with_single(batch, normalized)
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
