"""Static domain profile helpers for document translation.

Domain profiles are prior knowledge, not document-generated summaries. They
are meant to complement DelTA-style bilingual summaries, not replace them.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


_RULES_DIR = Path(__file__).resolve().parent / "translation_rules"
_DOMAIN_PROFILE_DIR = _RULES_DIR / "domain_profiles"
_DEFAULT_DOMAIN_PROFILE = os.getenv("AI_TRANSLATION_DEFAULT_DOMAIN_PROFILE", "")


def document_profile_enabled(style_options: dict[str, Any] | None = None) -> bool:
    """Return whether static domain profile injection should run."""

    if isinstance(style_options, dict) and style_options.get("document_profile") is False:
        return False
    value = os.getenv("AI_TRANSLATION_DOCUMENT_PROFILE_ENABLED", "0").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _profile_key(style_options: dict[str, Any] | None = None) -> str:
    options = style_options if isinstance(style_options, dict) else {}
    explicit = (
        options.get("document_profile")
        or options.get("domain_profile")
        or options.get("domain")
        or _DEFAULT_DOMAIN_PROFILE
    )
    if explicit is True:
        explicit = _DEFAULT_DOMAIN_PROFILE
    return str(explicit or "").strip().lower().replace("-", "_")


@lru_cache(maxsize=16)
def _load_profile(profile_key: str) -> dict[str, Any]:
    if not profile_key:
        return {}
    path = _DOMAIN_PROFILE_DIR / f"{profile_key}.json"
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def get_static_document_profile(
    style_options: dict[str, Any] | None = None,
    *,
    target_lang: str = "",
) -> dict[str, Any]:
    """Return a static domain profile selected by style options or env."""

    explicit_profile = (
        isinstance(style_options, dict)
        and (
            style_options.get("domain_profile")
            or (
                isinstance(style_options.get("document_profile"), str)
                and style_options.get("document_profile")
            )
            or style_options.get("domain")
        )
    )
    if not explicit_profile and not document_profile_enabled(style_options):
        return {}
    if isinstance(style_options, dict) and isinstance(style_options.get("_source_document_profile"), dict):
        return style_options["_source_document_profile"]

    profile = dict(_load_profile(_profile_key(style_options)))
    if not profile:
        return {}
    if target_lang:
        profile["target_lang"] = target_lang
    return profile
