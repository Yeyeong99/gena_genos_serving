"""XLSX 표시 텍스트/서식 렌더링 helper."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any

from openpyxl.utils.datetime import from_excel

_KOREAN_WEEKDAYS_LONG = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
_KOREAN_WEEKDAYS_SHORT = ["월", "화", "수", "목", "금", "토", "일"]
_ENGLISH_WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ENGLISH_WEEKDAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_HANGUL_FINANCIAL_DIGITS = {
    0: "",
    1: "壹",
    2: "貳",
    3: "參",
    4: "四",
    5: "伍",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
}
_HANGUL_FINANCIAL_SMALL_UNITS = ["", "拾", "百", "阡"]
_HANGUL_FINANCIAL_BIG_UNITS = ["", "萬", "億", "兆"]
_XLSX_FORMAT_TOKEN_RE = re.compile(
    r'"(?P<quoted>[^"]*)"?'
    r"|\[[^\]]*\]"
    r"|\\(?P<escaped>.)"
    r"|[_*].?"
    r"|@"
    r"|[0#?][0#?,.]*"
    r"|.",
    flags=re.DOTALL,
)


def _xlsx_format_section(number_format: str, value: Any, *, text_value: bool = False) -> str:
    sections = (number_format or "").split(";")
    if text_value and len(sections) >= 4:
        return sections[3]
    if not sections:
        return ""
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return sections[0]
    if numeric_value > 0:
        return sections[0]
    if numeric_value < 0 and len(sections) >= 2:
        return sections[1]
    if numeric_value == 0 and len(sections) >= 3:
        return sections[2]
    return sections[0]


def _xlsx_hanja_amount(value: int) -> str:
    if value == 0:
        return "零"

    def render_group(group_value: int) -> str:
        parts: list[str] = []
        digits = [
            group_value // 1000 % 10,
            group_value // 100 % 10,
            group_value // 10 % 10,
            group_value % 10,
        ]
        units = ["阡", "百", "拾", ""]
        for digit, unit in zip(digits, units):
            if digit == 0:
                continue
            if digit == 1 and unit in {"拾", "百", "阡"}:
                parts.append(unit)
            else:
                parts.append(f"{_HANGUL_FINANCIAL_DIGITS[digit]}{unit}")
        return "".join(parts)

    parts: list[str] = []
    group_index = 0
    remaining = abs(value)
    while remaining:
        group = remaining % 10000
        if group:
            rendered = render_group(group)
            big_unit = _HANGUL_FINANCIAL_BIG_UNITS[group_index] if group_index < len(_HANGUL_FINANCIAL_BIG_UNITS) else ""
            parts.insert(0, f"{rendered}{big_unit}")
        remaining //= 10000
        group_index += 1
    return "".join(parts)


def _xlsx_format_arabic_number(value: int | float, placeholder: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    decimal_places = 0
    if "." in placeholder:
        decimal_part = placeholder.rsplit(".", 1)[1]
        decimal_places = sum(1 for char in decimal_part if char in "0#?")
    use_comma = "," in placeholder
    if decimal_places > 0:
        formatted = f"{number:,.{decimal_places}f}" if use_comma else f"{number:.{decimal_places}f}"
        if "#" in placeholder.rsplit(".", 1)[1]:
            formatted = formatted.rstrip("0").rstrip(".")
    else:
        rounded = int(round(number))
        formatted = f"{rounded:,}" if use_comma else str(rounded)
    if formatted.startswith("0.") and placeholder.startswith("#"):
        formatted = formatted[1:]
    return formatted


def _xlsx_render_format_pattern(
    value: Any,
    number_format: str,
    *,
    text_value: bool = False,
) -> str:
    fmt = _xlsx_format_section(number_format, value, text_value=text_value)
    if not fmt or fmt.lower() == "general":
        return str(value)

    dbnum_match = re.search(r"\[dbnum(?P<kind>\d+)\]", fmt, flags=re.IGNORECASE)
    dbnum_kind = int(dbnum_match.group("kind")) if dbnum_match else None
    placeholder_inserted = False
    recognized = False
    rendered: list[str] = []

    def render_placeholder(placeholder: str) -> str:
        nonlocal placeholder_inserted, recognized
        placeholder_inserted = True
        recognized = True
        if dbnum_kind == 2:
            try:
                return _xlsx_hanja_amount(int(round(float(value))))
            except (TypeError, ValueError):
                return str(value)
        return _xlsx_format_arabic_number(value, placeholder)

    for match in _XLSX_FORMAT_TOKEN_RE.finditer(fmt):
        token = match.group(0)
        quoted = match.group("quoted")
        escaped = match.group("escaped")

        if quoted is not None:
            rendered.append(quoted)
            continue
        if token.startswith("["):
            continue
        if escaped is not None:
            rendered.append(escaped)
            continue
        if token.startswith(("_", "*")):
            continue
        if text_value and token == "@":
            rendered.append(str(value))
            placeholder_inserted = True
            recognized = True
            continue
        if not text_value and token[0] in "0#?":
            rendered.append(render_placeholder(token))
            continue
        rendered.append(token)

    if text_value and not placeholder_inserted:
        return str(value)
    if not text_value and dbnum_kind and not placeholder_inserted:
        rendered.append(render_placeholder("0"))
    return "".join(rendered).strip() if recognized or dbnum_kind else str(value)


def _xlsx_number_format_has_text_literal(number_format: str) -> bool:
    fmt = _xlsx_format_section(number_format, 1)
    if re.search(r"\[dbnum\d+\]", fmt, flags=re.IGNORECASE):
        return True
    without_brackets = re.sub(r"\[[^\]]*\]", "", fmt)
    without_quotes = re.sub(r'"[^"]*"', "", without_brackets)
    literals = re.sub(r"[0#?,._*\\/@Ee+\-\s;:$€£¥₩%()]", "", without_quotes)
    quoted_literals = "".join(re.findall(r'"([^"]*)"', fmt))
    return bool(re.search(r"[^\d\s.,+\-/%()]", literals + quoted_literals))
    

def _xlsx_format_date_text(value: date | datetime, number_format: str) -> str:
    fmt = (number_format or "").split(";", 1)[0]
    month = value.month
    day = value.day
    year = value.year
    weekday = value.weekday()
    long_weekday = _KOREAN_WEEKDAYS_LONG[weekday]
    short_weekday = _KOREAN_WEEKDAYS_SHORT[weekday]
    long_weekday_en = _ENGLISH_WEEKDAYS_LONG[weekday]
    short_weekday_en = _ENGLISH_WEEKDAYS_SHORT[weekday]

    def render_token(token: str) -> str:
        lower = token.lower()
        size = len(token)
        if lower.startswith("y"):
            if size <= 2:
                return f"{year % 100:02d}"
            return f"{year:04d}"
        if lower.startswith("m"):
            if size == 1:
                return str(month)
            if size == 2:
                return f"{month:02d}"
            if size in (3, 4):
                # Korean Excel commonly displays mmm/mmmm as "12월".
                return f"{month}월"
            return str(month)[0]
        if lower.startswith("d"):
            if size == 1:
                return str(day)
            if size == 2:
                return f"{day:02d}"
            if size == 3:
                return short_weekday_en
            return long_weekday_en
        if lower.startswith("a"):
            if size >= 4:
                return long_weekday
            return short_weekday
        return token

    rendered: list[str] = []
    index = 0
    recognized = False
    while index < len(fmt):
        char = fmt[index]
        if char == '"':
            end = fmt.find('"', index + 1)
            if end == -1:
                rendered.append(fmt[index + 1 :])
                break
            rendered.append(fmt[index + 1 : end])
            index = end + 1
            continue
        if char == "[":
            end = fmt.find("]", index + 1)
            index = len(fmt) if end == -1 else end + 1
            continue
        if char == "\\":
            if index + 1 < len(fmt):
                rendered.append(fmt[index + 1])
                index += 2
            else:
                index += 1
            continue
        if char == "_":
            index += 2
            continue
        if char == "*":
            index += 2
            continue
        if char.isalpha():
            end = index + 1
            while end < len(fmt) and fmt[end].lower() == char.lower():
                end += 1
            token = fmt[index:end]
            if token.lower()[0] in {"y", "m", "d", "a"}:
                rendered.append(render_token(token))
                recognized = True
            else:
                rendered.append(token)
            index = end
            continue
        rendered.append(char)
        index += 1

    if recognized:
        return "".join(rendered).strip()

    if "aaa" in fmt and "일" in fmt and "(" in fmt:
        return f"{day}일({short_weekday})"
    if "aaaa" in fmt and "일" not in fmt and "월" not in fmt:
        return long_weekday
    if "aaa" in fmt and "일" not in fmt and "월" not in fmt:
        return short_weekday
    if "월" in fmt and "일" in fmt:
        month_text = f"{month:02d}" if "mm" in fmt.lower() else str(month)
        day_text = f"{day:02d}" if "dd" in fmt.lower() else str(day)
        return f"{month_text}월 {day_text}일"
    if "월" in fmt:
        month_text = f"{month:02d}" if "mm" in fmt.lower() else str(month)
        return f"{month_text}월"
    if "일" in fmt:
        day_text = f"{day:02d}" if "dd" in fmt.lower() else str(day)
        return f"{day_text}일"
    return value.isoformat()


def _xlsx_number_format_looks_like_date(number_format: str) -> bool:
    fmt = (number_format or "").split(";", 1)[0]
    if not fmt or fmt.lower() == "general":
        return False
    normalized = re.sub(r"\[[^\]]*\]", "", fmt)
    normalized = re.sub(r'"[^"]*"', "", normalized)
    lower = normalized.lower()
    return any(token in lower for token in ("y", "d", "aaa", "월", "일")) or bool(
        re.search(r"(^|[^a-z])m{1,5}([^a-z]|$)", lower)
    )


def _xlsx_display_text(cell: Any, cached_cell: Any | None = None) -> str:
    raw_value = cell.value
    cached_value = getattr(cached_cell, "value", None) if cached_cell is not None else None
    value = cached_value if isinstance(raw_value, str) and raw_value.startswith("=") else raw_value
    if value is None and cached_value is not None:
        value = cached_value
    if value is None:
        return ""
    number_format = str(getattr(cell, "number_format", "") or "")
    if isinstance(value, str):
        if "@" in _xlsx_format_section(number_format, "", text_value=True):
            return _xlsx_render_format_pattern(value, number_format, text_value=True)
        return value
    if isinstance(value, (datetime, date)):
        return _xlsx_format_date_text(value, number_format)
    if getattr(cell, "is_date", False) and isinstance(cached_value, (datetime, date)):
        return _xlsx_format_date_text(cached_value, number_format)
    if isinstance(value, (int, float)) and _xlsx_number_format_looks_like_date(number_format):
        try:
            epoch = getattr(getattr(cell, "parent", None).parent, "epoch", None)
            converted = from_excel(value, epoch=epoch) if epoch is not None else from_excel(value)
            if isinstance(converted, (datetime, date)):
                return _xlsx_format_date_text(converted, number_format)
        except Exception:
            return ""
    if isinstance(value, (int, float)) and _xlsx_number_format_has_text_literal(number_format):
        return _xlsx_render_format_pattern(value, number_format)
    return ""
