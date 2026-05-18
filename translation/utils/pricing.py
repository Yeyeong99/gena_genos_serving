"""Credit pricing helpers for document translation usage."""

from __future__ import annotations

from contextvars import ContextVar
import logging
import math
import os
from typing import Any


log = logging.getLogger(__name__)

_TOKEN_PRICE_DEFAULTS: dict[str, tuple[str, float, float]] = {
    "zai-org/glm-5.1-fp8": ("GLM51", 0.25, 1.00),
    "qwen/qwen3.5-397b-a17b-fp8": ("QWEN35", 0.40, 1.40),
    "qwen/qwen3.5-397b-a17b": ("QWEN35", 0.40, 1.40),
    "moonshotai/kimi-k2.6": ("KIMI_K2", 0.40, 1.40),
}

_IMAGE_PRICE_DEFAULTS: dict[str, tuple[str, float]] = {
    "qwen/qwen-image-2512": ("QWEN_IMAGE_GEN", 0.20),
    "qwen/qwen-image-edit-2511": ("QWEN_IMAGE_EDIT", 0.30),
}

_usage_turns: ContextVar[list[dict[str, Any]]] = ContextVar("translation_usage_turns", default=[])
_image_counts: ContextVar[dict[str, int]] = ContextVar("translation_image_counts", default={})


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def _exchange_rate() -> float:
    return _env_float("CREDIT_EXCHANGE_RATE_KRW", 1500.0)


def _token_prices_usd(model: str) -> tuple[float, float] | None:
    entry = _TOKEN_PRICE_DEFAULTS.get(model)
    if entry is None:
        return None
    key, default_input, default_output = entry
    return (
        _env_float(f"CREDIT_{key}_INPUT_USD_PER_1M", default_input),
        _env_float(f"CREDIT_{key}_OUTPUT_USD_PER_1M", default_output),
    )


def _image_price_usd(model: str) -> float | None:
    entry = _IMAGE_PRICE_DEFAULTS.get(model)
    if entry is None:
        return None
    key, default_price = entry
    return _env_float(f"CREDIT_{key}_USD_PER_IMAGE", default_price)


def reset_usage() -> None:
    """Reset usage counters for the current request context."""

    _usage_turns.set([])
    _image_counts.set({})


def record_llm_usage(model: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    """Accumulate one LLM call's token usage in the current request context."""

    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    if not model or (prompt == 0 and completion == 0):
        return
    turns = _usage_turns.get()
    turns.append(
        {
            "model": model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
    )


def collect_image_usage(model: str, count: int = 1) -> None:
    """Accumulate image generation/edit counts in the current request context."""

    if not model or count <= 0:
        return
    image_counts = _image_counts.get()
    image_counts[model] = int(image_counts.get(model) or 0) + int(count)


def get_usage_turns() -> list[dict[str, Any]]:
    return list(_usage_turns.get() or [])


def get_image_counts() -> dict[str, int]:
    return dict(_image_counts.get() or {})


def calculate_credit_usage(
    turns: list[dict[str, Any]],
    image_counts: dict[str, int],
) -> float:
    """Calculate total credit usage in KRW."""

    total_usd = 0.0
    model_tokens: dict[str, dict[str, int]] = {}

    for turn in turns or []:
        model = str(turn.get("model") or "")
        bucket = model_tokens.setdefault(model, {"prompt": 0, "completion": 0})
        bucket["prompt"] += int(turn.get("prompt_tokens") or 0)
        bucket["completion"] += int(turn.get("completion_tokens") or 0)

    for model, tokens in model_tokens.items():
        prices = _token_prices_usd(model)
        if prices is None:
            log.warning("pricing: no token price for model %r; cost skipped", model)
            continue
        input_per_1m, output_per_1m = prices
        total_usd += (tokens["prompt"] / 1_000_000) * input_per_1m
        total_usd += (tokens["completion"] / 1_000_000) * output_per_1m

    for model, count in (image_counts or {}).items():
        price = _image_price_usd(str(model))
        if price is None:
            log.warning("pricing: no image price for model %r; cost skipped", model)
            continue
        total_usd += max(0, int(count or 0)) * price

    krw = total_usd * _exchange_rate()
    return round(krw, 6) if math.isfinite(krw) and krw > 0 else 0.0


def usage_total() -> dict[str, Any]:
    """Return aggregated token/image usage for the current request context."""

    turns = get_usage_turns()
    image_counts = get_image_counts()
    prompt_tokens = sum(int(turn.get("prompt_tokens") or 0) for turn in turns)
    completion_tokens = sum(int(turn.get("completion_tokens") or 0) for turn in turns)
    return {
        "turns": turns,
        "image_counts": image_counts,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def credit_payload() -> dict[str, Any]:
    """Return the payload fields consumed by agent-service/billing."""

    turns = get_usage_turns()
    image_counts = get_image_counts()
    return {
        "usage_total": usage_total(),
        "gena_credit_usage": calculate_credit_usage(turns, image_counts),
    }
