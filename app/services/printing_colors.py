from __future__ import annotations

import re
from typing import Any


class PrintingColorError(ValueError):
    """Raised when a printing-colour value cannot be saved safely."""


_EXPLICIT_SEPARATOR = re.compile(r"[,，、/／|｜;；+＋]")
_INTERNAL_WHITESPACE = re.compile(r"\s+")
_NO_PRINT_VALUES = frozenset({"", "无印刷", "无", "否", "不印刷"})
_DIRECT_PRINT_COLOR_COUNTS = {
    "单色印刷": 1,
    "双色印刷": 2,
    "三色印刷": 3,
    # Historical products used this value before the three-colour wording was
    # fixed.  It remains readable/restorable but is still bounded to 3 colours.
    "多色印刷": 3,
}
MAX_PRINTING_COLOR_LENGTH = 40
MAX_PRINTING_COLORS_STORAGE_LENGTH = 150
PRINTING_COLOR_SEPARATOR = "＋"


def _normalize_color_name(value: Any) -> str:
    return _INTERNAL_WHITESPACE.sub(" ", str(value or "").strip())


def parse_printing_colors(value: Any) -> list[str]:
    """Parse ordered colours using explicit legacy separators only.

    Ordinary whitespace is deliberately not a separator: real spot-colour
    names such as ``PANTONE 186 C`` must remain one colour.
    """

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = _EXPLICIT_SEPARATOR.split(str(value))
    return [_normalize_color_name(item) for item in raw_items if _normalize_color_name(item)]


def normalize_printing_colors(print_content: str | None, value: Any) -> str | None:
    """Validate a new save and return the canonical persisted colour text."""

    content = str(print_content or "").strip()
    if content in _NO_PRINT_VALUES:
        return None
    required = _DIRECT_PRINT_COLOR_COUNTS.get(content)
    if required is None:
        raise PrintingColorError("印刷情况只支持无印刷、单色、双色或三色印刷")

    colors = parse_printing_colors(value)
    if len(colors) != required:
        raise PrintingColorError(f"{content}必须按顺序填写 {required} 个颜色")
    if any(len(color) > MAX_PRINTING_COLOR_LENGTH for color in colors):
        raise PrintingColorError(
            f"每个印刷颜色名称不能超过 {MAX_PRINTING_COLOR_LENGTH} 个字符"
        )
    normalized_keys = [color.casefold() for color in colors]
    if len(set(normalized_keys)) != len(normalized_keys):
        raise PrintingColorError("同一印刷颜色不能重复填写")

    canonical = PRINTING_COLOR_SEPARATOR.join(colors)
    if len(canonical) > MAX_PRINTING_COLORS_STORAGE_LENGTH:
        raise PrintingColorError(
            f"印刷颜色合计不能超过 {MAX_PRINTING_COLORS_STORAGE_LENGTH} 个字符"
        )
    return canonical


def printing_color_summary(print_content: str | None, value: Any) -> str:
    """Return a readable summary without inventing missing historical facts."""

    content = str(print_content or "").strip()
    if content in _NO_PRINT_VALUES:
        return "无印刷"
    colors = parse_printing_colors(value)
    return PRINTING_COLOR_SEPARATOR.join(colors) if colors else "颜色待完善"
