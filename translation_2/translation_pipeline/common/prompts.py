"""Jinja 기반 프롬프트 렌더링 유틸리티."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@lru_cache(maxsize=1)
def _prompt_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )


def render_prompt(template_name: str, **context: Any) -> str:
    """프롬프트 템플릿을 렌더링한다."""

    template = _prompt_environment().get_template(template_name)
    return template.render(**context).strip()
