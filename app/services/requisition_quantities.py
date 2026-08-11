from __future__ import annotations

import re


LEGACY_CUTTING_MODE_FACTORS = {
    "一开一": 1,
    "一开二": 2,
    "一开三": 3,
    "一开四": 4,
    "一开五": 5,
    "一开六": 6,
}
CUTTING_MODE_FACTORS = dict(LEGACY_CUTTING_MODE_FACTORS)
DEFAULT_CUTTING_MODE = "一开一"
CUTTING_MODE_BOX_STYLES = {"衬板", "平卡", "模切内盒", "隔板", "刀卡"}
_CUTTING_MODE_PATTERN = re.compile(r"^一开([1-9]\d*)$")


class CuttingModeError(ValueError):
    pass


def normalize_cutting_mode(
    cutting_mode: object,
    *,
    strict: bool = False,
) -> str:
    """Return the stable business label for a positive sheet yield.

    Existing one-to-six facts retain their historical Chinese labels. New
    larger yields use ``一开N`` so old order snapshots do not need rewriting.
    Numeric UI values are accepted and converted at the boundary.
    """
    text = str(cutting_mode or "").strip()
    if text in LEGACY_CUTTING_MODE_FACTORS:
        return text
    factor: int | None = None
    if len(text) <= 18 and text.isdigit() and text == str(int(text or 0)):
        factor = int(text)
    else:
        match = _CUTTING_MODE_PATTERN.fullmatch(text)
        if match is not None and len(match.group(1)) <= 18:
            factor = int(match.group(1))
    if factor is not None and factor > 0:
        for label, legacy_factor in LEGACY_CUTTING_MODE_FACTORS.items():
            if legacy_factor == factor:
                return label
        label = f"一开{factor}"
        if len(label) <= 20:
            return label
    if strict:
        raise CuttingModeError("开料方式请输入大于0的整数")
    return DEFAULT_CUTTING_MODE


def cutting_mode_input_value(cutting_mode: object) -> int:
    """Return the positive integer shown in number inputs and task forms."""
    normalized = normalize_cutting_mode(cutting_mode)
    if normalized in LEGACY_CUTTING_MODE_FACTORS:
        return LEGACY_CUTTING_MODE_FACTORS[normalized]
    match = _CUTTING_MODE_PATTERN.fullmatch(normalized)
    return int(match.group(1)) if match is not None else 1


def cutting_factor(cutting_mode: object) -> int:
    return cutting_mode_input_value(cutting_mode)


def required_piece_quantity(order_quantity: int, pieces_per_box: int) -> int:
    return max(int(order_quantity or 0), 0) * max(int(pieces_per_box or 1), 1)


def purchase_sheet_quantity(
    required_piece_qty: int,
    inventory_deducted_piece_qty: int,
    cutting_mode: str | None,
) -> int:
    remaining = max(
        int(required_piece_qty or 0)
        - max(int(inventory_deducted_piece_qty or 0), 0),
        0,
    )
    factor = cutting_factor(cutting_mode)
    return (remaining + factor - 1) // factor
