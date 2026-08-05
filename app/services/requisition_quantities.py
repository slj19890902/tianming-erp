from __future__ import annotations


CUTTING_MODE_FACTORS = {
    "一开一": 1,
    "一开二": 2,
    "一开三": 3,
    "一开四": 4,
    "一开五": 5,
    "一开六": 6,
}
DEFAULT_CUTTING_MODE = "一开一"
CUTTING_MODE_BOX_STYLES = {"平卡", "模切内盒", "隔板", "刀卡"}


def cutting_factor(cutting_mode: str | None) -> int:
    return CUTTING_MODE_FACTORS.get(
        (cutting_mode or "").strip(),
        CUTTING_MODE_FACTORS[DEFAULT_CUTTING_MODE],
    )


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
