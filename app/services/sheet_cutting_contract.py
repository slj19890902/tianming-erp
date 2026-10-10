"""Versioned supplier-sheet cutting, independent of die-cut mold yield.

The caller first expands assembly/BOM requirements into physical pieces of
one compatible group. Inventory deductions must be expressed in those same
pieces. This module never interprets historical ``一开N`` snapshots as v2.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from app.services.requisition_quantities import normalize_cutting_mode


SCHEMA_VERSION = 2
MAX_SAFE_INTEGER = 2**53 - 1
MAX_DIMENSION_MM = Decimal("9999999999.99")  # Existing NUMERIC(12, 2) contract.


def cutting_work_instruction(snapshot):
    if snapshot is None:
        return None
    contract = SheetCuttingContract.from_snapshot(snapshot)
    if contract.cutting_factor == 1:
        split = "一开一"
    else:
        split = f"报料纸先按长{contract.length_parts}×宽{contract.width_parts}分切成{contract.cutting_factor}片"
    theory = f"每片{_decimal_text(contract.theoretical_length_mm)}×{_decimal_text(contract.theoretical_width_mm)}mm"
    process = f"再按{contract.mold_count}模加工" if contract.is_die_cut else "每片加工1片产品"
    trim = ""
    if contract.has_trim:
        length, width = contract.required_supplier_size_mm
        trim = f"先修边至{_decimal_text(length)}×{_decimal_text(width)}mm；"
    return f"{trim}{split}；{theory}；{process}；每张报料纸产出{contract.yield_per_supplier_sheet}片产品"


class SheetCuttingContractError(ValueError):
    pass


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise SheetCuttingContractError(f"{label}须为不小于{minimum}的有效整数")
    return value


def _dimension(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SheetCuttingContractError(f"{label}须为正数尺寸")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or not 0 < result <= MAX_DIMENSION_MM:
            raise ValueError
        if result != result.quantize(Decimal("0.01")):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise SheetCuttingContractError(f"{label}须为有效正数，最多保留两位小数") from None
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class SheetPurchaseQuantity:
    net_piece_qty: int
    supplier_sheet_qty: int
    theoretical_sheet_qty: int
    produced_piece_qty: int
    surplus_piece_qty: int


@dataclass(frozen=True)
class SheetCuttingContract:
    theoretical_length_mm: Decimal
    theoretical_width_mm: Decimal
    length_parts: int = 1
    width_parts: int = 1
    is_die_cut: bool = False
    mold_count: int = 1
    actual_supplier_length_mm: Decimal | None = None
    actual_supplier_width_mm: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "theoretical_length_mm",
                           _dimension(self.theoretical_length_mm, "理论报料长度"))
        object.__setattr__(self, "theoretical_width_mm",
                           _dimension(self.theoretical_width_mm, "理论报料宽度"))
        _integer(self.length_parts, "长向份数")
        _integer(self.width_parts, "宽向份数")
        _integer(self.mold_count, "模数")
        if type(self.is_die_cut) is not bool:
            raise SheetCuttingContractError("模切标记须为明确的布尔值")
        if not self.is_die_cut and self.mold_count != 1:
            raise SheetCuttingContractError("非模切产品不能填写多模产出")
        _integer(self.cutting_factor, "开料份数")
        _integer(self.yield_per_supplier_sheet, "每张供应商纸产出")
        if (self.actual_supplier_length_mm is None) != (self.actual_supplier_width_mm is None):
            raise SheetCuttingContractError("供应商实际长宽须同时填写")
        if self.actual_supplier_length_mm is not None:
            for axis in ("length", "width"):
                field = f"actual_supplier_{axis}_mm"
                object.__setattr__(self, field, _dimension(getattr(self, field), "供应商实际尺寸"))
            if any(actual < required for actual, required in zip(self.supplier_size_mm, self.required_supplier_size_mm)):
                raise SheetCuttingContractError("供应商实际长宽不能小于理论尺寸乘开料份数")
        length, width = self.supplier_size_mm
        _dimension(length, "供应商报料长度")
        _dimension(width, "供应商报料宽度")

    @property
    def cutting_factor(self) -> int:
        return self.length_parts * self.width_parts

    @property
    def cutting_mode(self) -> str:
        return normalize_cutting_mode(str(self.cutting_factor), strict=True)

    @property
    def supplier_size_mm(self) -> tuple[Decimal, Decimal]:
        if self.actual_supplier_length_mm is not None:
            return self.actual_supplier_length_mm, self.actual_supplier_width_mm
        return self.required_supplier_size_mm

    @property
    def required_supplier_size_mm(self) -> tuple[Decimal, Decimal]:
        return (self.theoretical_length_mm * self.length_parts,
                self.theoretical_width_mm * self.width_parts)

    @property
    def has_trim(self) -> bool:
        return self.supplier_size_mm != self.required_supplier_size_mm

    @property
    def yield_per_supplier_sheet(self) -> int:
        return self.cutting_factor * self.mold_count

    def purchase_quantity(self, *, required_piece_qty: int,
                          inventory_deducted_piece_qty: int = 0) -> SheetPurchaseQuantity:
        required = _integer(required_piece_qty, "所需实物片数", minimum=0)
        deducted = _integer(inventory_deducted_piece_qty, "库存抵扣片数", minimum=0)
        if deducted > required:
            raise SheetCuttingContractError("库存抵扣片数不能超过所需实物片数")
        net = required - deducted
        per_sheet = self.yield_per_supplier_sheet
        sheets = (net + per_sheet - 1) // per_sheet
        theoretical_sheets = sheets * self.cutting_factor
        produced = sheets * per_sheet
        _integer(theoretical_sheets, "理论纸片总数", minimum=0)
        _integer(produced, "理论实物产出", minimum=0)
        return SheetPurchaseQuantity(net, sheets, theoretical_sheets, produced, produced - net)

    def to_snapshot(self) -> dict:
        length, width = self.supplier_size_mm
        result = {
            "schema_version": 3 if self.actual_supplier_length_mm is not None else SCHEMA_VERSION,
            "theoretical_length_mm": _decimal_text(self.theoretical_length_mm),
            "theoretical_width_mm": _decimal_text(self.theoretical_width_mm),
            "length_parts": self.length_parts,
            "width_parts": self.width_parts,
            "is_die_cut": self.is_die_cut,
            "mold_count": self.mold_count,
            "supplier_length_mm": _decimal_text(length),
            "supplier_width_mm": _decimal_text(width),
            "cutting_factor": self.cutting_factor,
            "yield_per_supplier_sheet": self.yield_per_supplier_sheet,
        }
        if result["schema_version"] == 3:
            result.update(actual_supplier_length_mm=_decimal_text(length), actual_supplier_width_mm=_decimal_text(width))
        return result

    @classmethod
    def from_snapshot(cls, snapshot: Mapping) -> SheetCuttingContract:
        if not isinstance(snapshot, Mapping):
            raise SheetCuttingContractError("开料快照须为完整对象")
        version = snapshot.get("schema_version")
        if type(version) is not int or version not in (2, 3):
            raise SheetCuttingContractError("开料快照版本不支持；旧单据须沿用原数量合同")
        fields = {"theoretical_length_mm", "theoretical_width_mm", "length_parts",
                  "width_parts", "is_die_cut", "mold_count"}
        if version == 3:
            fields |= {"actual_supplier_length_mm", "actual_supplier_width_mm"}
        derived = {"supplier_length_mm", "supplier_width_mm", "cutting_factor",
                   "yield_per_supplier_sheet", "schema_version"}
        if set(snapshot) != fields | derived:
            raise SheetCuttingContractError("开料快照字段不完整或包含未知字段")
        result = cls(**{key: snapshot[key] for key in fields})
        expected = result.to_snapshot()
        if any(type(snapshot[key]) is not type(value) or snapshot[key] != value
               for key, value in expected.items()):
            raise SheetCuttingContractError("开料快照与理论尺寸、方向份数或模数不一致")
        return result
