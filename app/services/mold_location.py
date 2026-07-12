from __future__ import annotations

import re


_CANONICAL_FLAT_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>[1-3])-D(?P<row>\d+)-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_CANONICAL_VERTICAL_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>[1-3])-V-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_SHORT_FLAT_PATTERN = re.compile(
    r"^(?P<area>[A-Z0-9\u4e00-\u9fff]+)-H(?P<rack>\d+)-L(?P<level>[1-3])-R(?P<row>\d+)-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_SHORT_VERTICAL_PATTERN = re.compile(
    r"^(?P<area>[A-Z0-9\u4e00-\u9fff]+)-H(?P<rack>\d+)-L(?P<level>[1-3])-V(?P<position>\d+)$",
    re.IGNORECASE,
)


def _number(value: str) -> int:
    return int(value.lstrip("0") or "0")


def _area_text(value: str) -> str:
    area = value.upper()
    return area if area.endswith("区") else f"{area}区"


def _floor_text(value: str) -> str:
    floor = _number(value[:-1])
    chinese = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
    return f"{chinese.get(floor, floor)}楼模具区"


def describe_mold_location(value: str) -> dict:
    """Turn a stable rack code into a prompt a workshop worker can follow."""

    raw = (value or "").strip()
    normalized = raw.upper()
    flat = _CANONICAL_FLAT_PATTERN.fullmatch(normalized)
    if flat:
        parts = flat.groupdict()
        prompt = (
            f"前往{_floor_text(parts['floor'])}，第{_number(parts['rack'])}号货架，"
            f"第{_number(parts['level'])}层、第{_number(parts['row'])}排，"
            f"从左到右第{_number(parts['position'])}块。"
            "拿取前请核对模具编号和存货编码。"
        )
        return {
            "kind": "flat",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": _number(parts["row"]),
            "position": _number(parts["position"]),
            "prompt": prompt,
        }
    vertical = _CANONICAL_VERTICAL_PATTERN.fullmatch(normalized)
    if vertical:
        parts = vertical.groupdict()
        level_text = "底层（第1层）" if _number(parts["level"]) == 1 else f"第{_number(parts['level'])}层"
        prompt = (
            f"前往{_floor_text(parts['floor'])}，第{_number(parts['rack'])}号货架，"
            f"{level_text}竖放区，从左到右第{_number(parts['position'])}块。"
            "大模具较重，请按现场要求两人搬运；拿取前核对模具编号和存货编码。"
        )
        return {
            "kind": "vertical",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": None,
            "position": _number(parts["position"]),
            "prompt": prompt,
        }
    flat = _SHORT_FLAT_PATTERN.fullmatch(normalized)
    if flat:
        parts = flat.groupdict()
        return {
            "kind": "flat_legacy",
            "location_code": normalized,
            "floor": None,
            "area": parts["area"].upper(),
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": _number(parts["row"]),
            "position": _number(parts["position"]),
            "prompt": (
                f"前往{_area_text(parts['area'])}第{_number(parts['rack'])}号模具架，"
                f"第{_number(parts['level'])}层、第{_number(parts['row'])}排，"
                f"从左到右第{_number(parts['position'])}块。"
                "该位置使用旧简写，建议现场复核后改为 3F-M 标准位置码；拿取前核对模具编号和存货编码。"
            ),
        }
    vertical = _SHORT_VERTICAL_PATTERN.fullmatch(normalized)
    if vertical:
        parts = vertical.groupdict()
        return {
            "kind": "vertical_legacy",
            "location_code": normalized,
            "floor": None,
            "area": parts["area"].upper(),
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": None,
            "position": _number(parts["position"]),
            "prompt": (
                f"前往{_area_text(parts['area'])}第{_number(parts['rack'])}号模具架，"
                f"第{_number(parts['level'])}层竖放区，从左到右第{_number(parts['position'])}块。"
                "该位置使用旧简写，建议现场复核后改为 3F-M 标准位置码；大模具请两人搬运。"
            ),
        }
    return {
        "kind": "manual",
        "location_code": raw,
        "floor": None,
        "area": None,
        "rack": None,
        "level": None,
        "row": None,
        "position": None,
        "prompt": f"请前往“{raw or '未登记位置'}”查找，拿取前核对模具编号和存货编码。",
    }
