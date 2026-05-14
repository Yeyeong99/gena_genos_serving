"""Fast target-language compatibility checks for translation requests."""

from __future__ import annotations

import re
from typing import Iterable


_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)?")
_LATIN_SENTENCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9\s,.;:!?()'\"$%&/\-–—]{24,}")


def _normalize_target(target_lang: str) -> str:
    target = (target_lang or "").strip().lower()
    if target in {"ko", "kor", "korean", "한국어"}:
        return "ko"
    if target in {"en", "eng", "english", "영어"}:
        return "en"
    if target in {"ja", "jp", "jpn", "japanese", "일본어"}:
        return "ja"
    if target in {"zh", "cn", "chi", "zho", "chinese", "중국어"}:
        return "zh"
    return target


def _is_hangul(char: str) -> bool:
    code = ord(char)
    return 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F


def _is_latin(char: str) -> bool:
    return ("A" <= char <= "Z") or ("a" <= char <= "z")


def _is_kana(char: str) -> bool:
    code = ord(char)
    return 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF


def _is_han(char: str) -> bool:
    code = ord(char)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF


def _letter_counts(text: str) -> dict[str, int]:
    counts = {"ko": 0, "latin": 0, "kana": 0, "han": 0}
    for char in text:
        if _is_hangul(char):
            counts["ko"] += 1
        elif _is_latin(char):
            counts["latin"] += 1
        elif _is_kana(char):
            counts["kana"] += 1
        elif _is_han(char):
            counts["han"] += 1
    return counts


def _has_latin_sentence(text: str) -> bool:
    for match in _LATIN_SENTENCE_RE.finditer(text):
        words = _LATIN_WORD_RE.findall(match.group(0))
        if len(words) >= 4:
            return True
    return False


def _has_meaningful_letters(text: str) -> bool:
    return any(
        _is_hangul(char) or _is_latin(char) or _is_kana(char) or _is_han(char)
        for char in text
    )


def _join_texts(texts: Iterable[str], *, max_chars: int = 20000) -> str:
    parts: list[str] = []
    remaining = max_chars
    for text in texts:
        value = str(text or "").strip()
        if not value:
            continue
        if remaining <= 0:
            break
        parts.append(value[:remaining])
        remaining -= len(parts[-1])
    return "\n".join(parts)


def has_text_requiring_translation(texts: Iterable[str], target_lang: str) -> bool:
    """Return whether any source text appears to need translation into target_lang.

    The check is intentionally conservative: it only skips when the document is
    already readable in the target language and contains no substantial foreign
    language sentence/script. Short Latin names, numbers, URLs, and symbols do
    not make a Korean document go through the full LLM pipeline.
    """

    target = _normalize_target(target_lang)
    text = _join_texts(texts)
    if not text or not _has_meaningful_letters(text):
        return False

    counts = _letter_counts(text)
    total_letters = sum(counts.values())
    if total_letters == 0:
        return False

    if target == "ko":
        return bool(
            counts["kana"] > 0
            or counts["han"] >= 2
            or _has_latin_sentence(text)
        )

    if target == "en":
        return bool(counts["ko"] > 0 or counts["kana"] > 0 or counts["han"] > 0)

    if target == "ja":
        if counts["ko"] > 0 or _has_latin_sentence(text):
            return True
        # Han-only text is ambiguous between Chinese and Japanese. Skip only
        # when Japanese kana is already present or there is no Han text.
        return bool(counts["han"] > 0 and counts["kana"] == 0)

    if target == "zh":
        return bool(counts["ko"] > 0 or counts["kana"] > 0 or _has_latin_sentence(text))

    # Unknown target language: prefer the existing translation path.
    return True


def build_same_language_skip_notice(target_lang: str) -> str:
    target = _normalize_target(target_lang)
    label_by_target = {
        "ko": "한국어",
        "en": "영어",
        "zh": "중국어",
    }
    label = label_by_target.get(target, target_lang or "선택한 언어")
    return f"원본과 같은 언어({label})가 선택된 것 같습니다. 대상 언어를 다시 선택해 주세요."
