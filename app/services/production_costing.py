"""Pure production-cost calculations for an immutable batch snapshot.

The functions in this module do not read or write the database.  Callers must
provide the rates that were effective for the batch, including both the
configured default and the rate actually applied.  This keeps the calculation
testable and prevents a future rate-card change from rewriting history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


SQUARE_MILLIMETRES_PER_SQUARE_METRE = Decimal("1000000")
AREA_QUANTUM = Decimal("0.000001")
COST_QUANTUM = Decimal("0.0001")

COLOR_TIERS = {"single", "double", "multi"}
DIECUT_SIZE_TIERS = {"normal", "oversized"}
MATERIAL_SOURCE_KINDS = {
    "actual_material_input",
    "semi_finished_inventory",
    "finished_inventory",
    "customer_supplied",
    "mixed",
}


class ProductionCostError(ValueError):
    """Raised when a batch cannot produce a trustworthy cost snapshot."""


def _decimal(value: object, label: str, *, allow_zero: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProductionCostError(f"{label}必须是有效数字") from error
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        qualifier = "非负数" if allow_zero else "正数"
        raise ProductionCostError(f"{label}必须是{qualifier}")
    return result


def _integer(value: object, label: str, *, allow_zero: bool = True) -> int:
    number = _decimal(value, label, allow_zero=allow_zero)
    integral = number.to_integral_value()
    if number != integral:
        raise ProductionCostError(f"{label}必须是整数")
    return int(integral)


def _cost(value: Decimal) -> Decimal:
    return value.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RateSelection:
    """Default and applied rate values captured for one batch."""

    default_rate: Decimal | None
    applied_rate: Decimal
    override_reason: str | None = None

    def validated(self, label: str) -> "RateSelection":
        default = (
            None
            if self.default_rate is None
            else _decimal(self.default_rate, f"{label}默认费率")
        )
        applied = _decimal(self.applied_rate, f"{label}采用费率")
        reason = (self.override_reason or "").strip() or None
        if default is None or applied != default:
            if not reason:
                raise ProductionCostError(f"{label}采用费率发生人工覆盖时必须填写原因")
        return RateSelection(default, applied, reason)


@dataclass(frozen=True)
class CostComputationContext:
    """Audit identity captured with one immutable batch calculation."""

    batch_reference: str
    formula_version: str
    rate_card_version: str
    rate_effective_from: date
    calculated_by: str
    calculated_at: datetime
    material_source_kind: str

    def validated(self) -> "CostComputationContext":
        values = {
            "生产批次编号": self.batch_reference,
            "成本公式版本": self.formula_version,
            "费率卡版本": self.rate_card_version,
            "计算人": self.calculated_by,
        }
        cleaned = {label: (value or "").strip() for label, value in values.items()}
        for label, value in cleaned.items():
            if not value:
                raise ProductionCostError(f"{label}不能为空")
        source_kind = (self.material_source_kind or "").strip().lower()
        if source_kind not in MATERIAL_SOURCE_KINDS:
            raise ProductionCostError(
                "材料来源必须是 actual_material_input、semi_finished_inventory、"
                "finished_inventory、customer_supplied 或 mixed"
            )
        if self.calculated_at.tzinfo is None:
            raise ProductionCostError("成本计算时间必须包含时区")
        return CostComputationContext(
            batch_reference=cleaned["生产批次编号"],
            formula_version=cleaned["成本公式版本"],
            rate_card_version=cleaned["费率卡版本"],
            rate_effective_from=self.rate_effective_from,
            calculated_by=cleaned["计算人"],
            calculated_at=self.calculated_at,
            material_source_kind=source_kind,
        )


@dataclass(frozen=True)
class MaterialCostInput:
    source_reference: str
    component_reference: str
    component_label: str
    report_length_mm: Decimal
    report_width_mm: Decimal
    actual_sheet_count: int
    square_metre_rate: RateSelection


@dataclass(frozen=True)
class PrintingCostInput:
    operation_reference: str
    component_reference: str
    component_label: str
    printed_sheet_count: int
    color_count: int
    color_tier: str
    print_passes: int
    print_setup_count: int
    variable_rate_per_sheet_pass: RateSelection
    setup_rate: RateSelection


@dataclass(frozen=True)
class DiecutCostInput:
    operation_reference: str
    component_reference: str
    component_label: str
    diecut_sheet_count: int
    diecut_size_tier: str
    diecut_passes: int
    diecut_setup_count: int
    variable_rate_per_sheet_pass: RateSelection
    setup_rate: RateSelection


@dataclass(frozen=True)
class LaborCostInput:
    operation_reference: str
    process_name: str
    worker_count: int
    hours_per_worker: Decimal
    hourly_rate: RateSelection


@dataclass(frozen=True)
class OtherCostInput:
    cost_reference: str
    category: str
    amount: Decimal
    reason: str
    voucher_reference: str | None = None


@dataclass(frozen=True)
class CarriedMaterialCostInput:
    source_reference: str
    source_kind: str
    component_reference: str
    cost_snapshot_reference: str | None
    amount: Decimal
    reason: str


@dataclass(frozen=True)
class MaterialCostDetail:
    source_reference: str
    component_reference: str
    component_label: str
    area_m2_per_sheet: Decimal
    actual_sheet_count: int
    default_rate: Decimal | None
    applied_rate: Decimal
    override_reason: str | None
    amount: Decimal


@dataclass(frozen=True)
class PrintingCostDetail:
    operation_reference: str
    component_reference: str
    component_label: str
    printed_sheet_count: int
    color_count: int
    color_tier: str
    print_passes: int
    print_setup_count: int
    chargeable_sheet_passes: int
    variable_default_rate: Decimal | None
    variable_applied_rate: Decimal
    variable_override_reason: str | None
    setup_default_rate: Decimal | None
    setup_applied_rate: Decimal
    setup_override_reason: str | None
    amount: Decimal


@dataclass(frozen=True)
class DiecutCostDetail:
    operation_reference: str
    component_reference: str
    component_label: str
    diecut_sheet_count: int
    diecut_size_tier: str
    diecut_passes: int
    diecut_setup_count: int
    chargeable_sheet_passes: int
    variable_default_rate: Decimal | None
    variable_applied_rate: Decimal
    variable_override_reason: str | None
    setup_default_rate: Decimal | None
    setup_applied_rate: Decimal
    setup_override_reason: str | None
    amount: Decimal


@dataclass(frozen=True)
class LaborCostDetail:
    operation_reference: str
    process_name: str
    worker_count: int
    hours_per_worker: Decimal
    total_labor_hours: Decimal
    default_rate: Decimal | None
    applied_rate: Decimal
    override_reason: str | None
    amount: Decimal


@dataclass(frozen=True)
class OtherCostDetail:
    cost_reference: str
    category: str
    amount: Decimal
    reason: str
    voucher_reference: str | None


@dataclass(frozen=True)
class CarriedMaterialCostDetail:
    source_reference: str
    source_kind: str
    component_reference: str
    cost_snapshot_reference: str | None
    amount: Decimal
    reason: str


@dataclass(frozen=True)
class ProductionCostBreakdown:
    context: CostComputationContext
    good_quantity: int
    material_cost: Decimal
    printing_cost: Decimal
    diecut_cost: Decimal
    labor_cost: Decimal
    other_cost: Decimal
    rounding_adjustment: Decimal
    total_batch_cost: Decimal
    unit_complete_cost: Decimal
    total_labor_hours: Decimal
    material_details: tuple[MaterialCostDetail, ...]
    carried_material_details: tuple[CarriedMaterialCostDetail, ...]
    printing_details: tuple[PrintingCostDetail, ...]
    diecut_details: tuple[DiecutCostDetail, ...]
    labor_details: tuple[LaborCostDetail, ...]
    other_cost_details: tuple[OtherCostDetail, ...]


def _validate_color_tier(color_count: int, color_tier: str) -> None:
    expected = "single" if color_count == 1 else "double" if color_count == 2 else "multi"
    if color_tier not in COLOR_TIERS:
        raise ProductionCostError("印刷色数档必须是 single、double 或 multi")
    if color_tier != expected:
        raise ProductionCostError(
            f"实际色数 {color_count} 与色数档 {color_tier} 不一致，应为 {expected}"
        )


def calculate_production_batch_cost(
    *,
    context: CostComputationContext,
    good_quantity: int,
    materials: Iterable[MaterialCostInput] = (),
    carried_material_costs: Iterable[CarriedMaterialCostInput] = (),
    printing_operations: Iterable[PrintingCostInput] = (),
    diecut_operations: Iterable[DiecutCostInput] = (),
    labor_operations: Iterable[LaborCostInput] = (),
    other_costs: Iterable[OtherCostInput] = (),
) -> ProductionCostBreakdown:
    """Calculate one batch using actual input and qualified output quantities."""

    audit_context = context.validated()
    good_count = _integer(good_quantity, "本批合格成品数量", allow_zero=False)
    material_total = Decimal("0")
    printing_total = Decimal("0")
    diecut_total = Decimal("0")
    labor_total = Decimal("0")
    labor_hours_total = Decimal("0")
    material_details: list[MaterialCostDetail] = []
    carried_material_details: list[CarriedMaterialCostDetail] = []
    printing_details: list[PrintingCostDetail] = []
    diecut_details: list[DiecutCostDetail] = []
    labor_details: list[LaborCostDetail] = []
    other_cost_details: list[OtherCostDetail] = []

    for row in materials:
        source_reference = (row.source_reference or "").strip()
        component_reference = (row.component_reference or "").strip()
        if not source_reference or not component_reference:
            raise ProductionCostError("片料来源编号和 BOM 部件编号不能为空")
        label = (row.component_label or "").strip() or "未命名片料"
        length = _decimal(row.report_length_mm, f"{label}报料长", allow_zero=False)
        width = _decimal(row.report_width_mm, f"{label}报料宽", allow_zero=False)
        sheets = _integer(row.actual_sheet_count, f"{label}实际投料张数", allow_zero=False)
        rate = row.square_metre_rate.validated(f"{label}纸板平方价")
        raw_area = length * width / SQUARE_MILLIMETRES_PER_SQUARE_METRE
        area = raw_area.quantize(AREA_QUANTUM, rounding=ROUND_HALF_UP)
        amount = raw_area * Decimal(sheets) * rate.applied_rate
        material_total += amount
        material_details.append(
            MaterialCostDetail(
                source_reference=source_reference,
                component_reference=component_reference,
                component_label=label,
                area_m2_per_sheet=area,
                actual_sheet_count=sheets,
                default_rate=rate.default_rate,
                applied_rate=rate.applied_rate,
                override_reason=rate.override_reason,
                amount=amount,
            )
        )

    for row in carried_material_costs:
        source_reference = (row.source_reference or "").strip()
        source_kind = (row.source_kind or "").strip().lower()
        component_reference = (row.component_reference or "").strip()
        snapshot_reference = (row.cost_snapshot_reference or "").strip() or None
        reason = (row.reason or "").strip()
        if not source_reference or not component_reference or not reason:
            raise ProductionCostError("承接材料成本的来源编号、BOM 部件编号和原因不能为空")
        if source_kind not in {
            "semi_finished_inventory",
            "finished_inventory",
            "customer_supplied",
        }:
            raise ProductionCostError("承接材料成本来源必须是半成品、成品库存或客户自带料")
        amount = _decimal(
            row.amount,
            f"{source_reference}承接材料成本",
            allow_zero=source_kind == "customer_supplied",
        )
        if source_kind != "customer_supplied" and not snapshot_reference:
            raise ProductionCostError("库存材料成本必须关联已确认成本快照")
        carried_material_details.append(
            CarriedMaterialCostDetail(
                source_reference=source_reference,
                source_kind=source_kind,
                component_reference=component_reference,
                cost_snapshot_reference=snapshot_reference,
                amount=amount,
                reason=reason,
            )
        )
        material_total += amount

    source_kind = audit_context.material_source_kind
    if source_kind == "actual_material_input" and not material_details:
        raise ProductionCostError("实际投料批次至少需要一条片料成本明细")
    if source_kind in {
        "semi_finished_inventory",
        "finished_inventory",
        "customer_supplied",
    }:
        matching = [row for row in carried_material_details if row.source_kind == source_kind]
        if not matching:
            raise ProductionCostError("库存或客户自带料必须提供明确的承接材料成本依据")
    if source_kind == "mixed" and not (material_details or carried_material_details):
        raise ProductionCostError("混合材料来源至少需要一条实际投料或承接成本明细")

    for row in printing_operations:
        operation_reference = (row.operation_reference or "").strip()
        component_reference = (row.component_reference or "").strip()
        if not operation_reference or not component_reference:
            raise ProductionCostError("印刷工序编号和 BOM 部件编号不能为空")
        label = (row.component_label or "").strip() or "未命名印刷件"
        sheets = _integer(row.printed_sheet_count, f"{label}实际印刷张数", allow_zero=False)
        colors = _integer(row.color_count, f"{label}实际色数", allow_zero=False)
        tier = (row.color_tier or "").strip().lower()
        _validate_color_tier(colors, tier)
        passes = _integer(row.print_passes, f"{label}每张印刷遍数", allow_zero=False)
        setups = _integer(row.print_setup_count, f"{label}印刷开机/调机次数")
        variable_rate = row.variable_rate_per_sheet_pass.validated(
            f"{label}{tier}印刷加工"
        )
        setup_rate = row.setup_rate.validated(f"{label}印刷开机")
        variable_amount = Decimal(sheets * passes) * variable_rate.applied_rate
        setup_amount = Decimal(setups) * setup_rate.applied_rate
        amount = variable_amount + setup_amount
        printing_total += amount
        printing_details.append(
            PrintingCostDetail(
                operation_reference=operation_reference,
                component_reference=component_reference,
                component_label=label,
                printed_sheet_count=sheets,
                color_count=colors,
                color_tier=tier,
                print_passes=passes,
                print_setup_count=setups,
                chargeable_sheet_passes=sheets * passes,
                variable_default_rate=variable_rate.default_rate,
                variable_applied_rate=variable_rate.applied_rate,
                variable_override_reason=variable_rate.override_reason,
                setup_default_rate=setup_rate.default_rate,
                setup_applied_rate=setup_rate.applied_rate,
                setup_override_reason=setup_rate.override_reason,
                amount=amount,
            )
        )

    for row in diecut_operations:
        operation_reference = (row.operation_reference or "").strip()
        component_reference = (row.component_reference or "").strip()
        if not operation_reference or not component_reference:
            raise ProductionCostError("模切工序编号和 BOM 部件编号不能为空")
        label = (row.component_label or "").strip() or "未命名模切件"
        sheets = _integer(row.diecut_sheet_count, f"{label}实际模切张数", allow_zero=False)
        tier = (row.diecut_size_tier or "").strip().lower()
        if tier not in DIECUT_SIZE_TIERS:
            raise ProductionCostError("模切尺寸档必须是 normal 或 oversized")
        passes = _integer(row.diecut_passes, f"{label}每张模切遍数", allow_zero=False)
        setups = _integer(row.diecut_setup_count, f"{label}模切开机/调机次数")
        variable_rate = row.variable_rate_per_sheet_pass.validated(
            f"{label}{tier}模切加工"
        )
        setup_rate = row.setup_rate.validated(f"{label}模切开机")
        variable_amount = Decimal(sheets * passes) * variable_rate.applied_rate
        setup_amount = Decimal(setups) * setup_rate.applied_rate
        amount = variable_amount + setup_amount
        diecut_total += amount
        diecut_details.append(
            DiecutCostDetail(
                operation_reference=operation_reference,
                component_reference=component_reference,
                component_label=label,
                diecut_sheet_count=sheets,
                diecut_size_tier=tier,
                diecut_passes=passes,
                diecut_setup_count=setups,
                chargeable_sheet_passes=sheets * passes,
                variable_default_rate=variable_rate.default_rate,
                variable_applied_rate=variable_rate.applied_rate,
                variable_override_reason=variable_rate.override_reason,
                setup_default_rate=setup_rate.default_rate,
                setup_applied_rate=setup_rate.applied_rate,
                setup_override_reason=setup_rate.override_reason,
                amount=amount,
            )
        )

    for row in labor_operations:
        operation_reference = (row.operation_reference or "").strip()
        if not operation_reference:
            raise ProductionCostError("人工工序记录编号不能为空")
        process = (row.process_name or "").strip()
        if not process:
            raise ProductionCostError("人工工序名称不能为空")
        workers = _integer(row.worker_count, f"{process}人数", allow_zero=False)
        hours = _decimal(row.hours_per_worker, f"{process}每人工时", allow_zero=False)
        rate = row.hourly_rate.validated(f"{process}人工小时")
        total_hours = Decimal(workers) * hours
        amount = total_hours * rate.applied_rate
        labor_hours_total += total_hours
        labor_total += amount
        labor_details.append(
            LaborCostDetail(
                operation_reference=operation_reference,
                process_name=process,
                worker_count=workers,
                hours_per_worker=hours,
                total_labor_hours=total_hours,
                default_rate=rate.default_rate,
                applied_rate=rate.applied_rate,
                override_reason=rate.override_reason,
                amount=amount,
            )
        )

    extra = Decimal("0")
    for row in other_costs:
        cost_reference = (row.cost_reference or "").strip()
        category = (row.category or "").strip()
        reason = (row.reason or "").strip()
        voucher = (row.voucher_reference or "").strip() or None
        if not cost_reference or not category or not reason:
            raise ProductionCostError("其他费用编号、类别和原因不能为空")
        amount = _decimal(row.amount, f"{category}金额", allow_zero=False)
        extra += amount
        other_cost_details.append(
            OtherCostDetail(
                cost_reference=cost_reference,
                category=category,
                amount=amount,
                reason=reason,
                voucher_reference=voucher,
            )
        )
    raw_total = material_total + printing_total + diecut_total + labor_total + extra
    total = _cost(raw_total)
    unit = _cost(raw_total / Decimal(good_count))
    rounded_material = _cost(material_total)
    rounded_printing = _cost(printing_total)
    rounded_diecut = _cost(diecut_total)
    rounded_labor = _cost(labor_total)
    rounded_other = _cost(extra)
    rounding_adjustment = _cost(
        total
        - (
            rounded_material
            + rounded_printing
            + rounded_diecut
            + rounded_labor
            + rounded_other
        )
    )
    return ProductionCostBreakdown(
        context=audit_context,
        good_quantity=good_count,
        material_cost=rounded_material,
        printing_cost=rounded_printing,
        diecut_cost=rounded_diecut,
        labor_cost=rounded_labor,
        other_cost=rounded_other,
        rounding_adjustment=rounding_adjustment,
        total_batch_cost=total,
        unit_complete_cost=unit,
        total_labor_hours=labor_hours_total,
        material_details=tuple(material_details),
        carried_material_details=tuple(carried_material_details),
        printing_details=tuple(printing_details),
        diecut_details=tuple(diecut_details),
        labor_details=tuple(labor_details),
        other_cost_details=tuple(other_cost_details),
    )
