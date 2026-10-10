from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.production_costing import (
    CarriedMaterialCostInput,
    CostComputationContext,
    DiecutCostInput,
    LaborCostInput,
    MaterialCostInput,
    OtherCostInput,
    PrintingCostInput,
    ProductionCostError,
    RateSelection,
    calculate_production_batch_cost,
)


def rate(value: str) -> RateSelection:
    return RateSelection(default_rate=Decimal(value), applied_rate=Decimal(value))


def context(material_source_kind: str = "customer_supplied") -> CostComputationContext:
    return CostComputationContext(
        batch_reference="PB-20260720-001",
        formula_version="production-cost-v1",
        rate_card_version="RATE-202607",
        rate_effective_from=date(2026, 7, 1),
        calculated_by="admin",
        calculated_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        material_source_kind=material_source_kind,
    )


def customer_supplied_material() -> tuple[CarriedMaterialCostInput, ...]:
    return (
        CarriedMaterialCostInput(
            source_reference="CUSTOMER-MATERIAL-1",
            source_kind="customer_supplied",
            component_reference="BOM-COVER",
            cost_snapshot_reference=None,
            amount=Decimal("0"),
            reason="客户提供材料，材料成本不计入本批",
        ),
    )


def test_printing_cost_keeps_color_tier_passes_and_setup_separate() -> None:
    results = {}
    for colors, tier, variable in ((1, "single", "0.01"), (2, "double", "0.02"), (4, "multi", "0.04")):
        result = calculate_production_batch_cost(
            context=context(),
            good_quantity=100,
            carried_material_costs=customer_supplied_material(),
            printing_operations=(
                PrintingCostInput(
                    operation_reference=f"PRINT-{colors}",
                    component_reference="BOM-COVER",
                    component_label="测试箱",
                    printed_sheet_count=100,
                    color_count=colors,
                    color_tier=tier,
                    print_passes=1,
                    print_setup_count=1,
                    variable_rate_per_sheet_pass=rate(variable),
                    setup_rate=rate("10"),
                ),
            ),
        )
        results[tier] = result.printing_cost

    assert results == {
        "single": Decimal("11.0000"),
        "double": Decimal("12.0000"),
        "multi": Decimal("14.0000"),
    }


def test_oversized_diecut_twice_charges_two_sheet_passes_but_one_setup() -> None:
    result = calculate_production_batch_cost(
        context=context(),
        good_quantity=1000,
        carried_material_costs=customer_supplied_material(),
        diecut_operations=(
            DiecutCostInput(
                operation_reference="DIECUT-OVERSIZED-1",
                component_reference="BOM-COVER",
                component_label="超大纸箱",
                diecut_sheet_count=1000,
                diecut_size_tier="oversized",
                diecut_passes=2,
                diecut_setup_count=1,
                variable_rate_per_sheet_pass=rate("0.05"),
                setup_rate=rate("20"),
            ),
        ),
    )

    assert result.diecut_details[0].chargeable_sheet_passes == 2000
    assert result.diecut_details[0].diecut_setup_count == 1
    assert result.diecut_cost == Decimal("120.0000")


def test_grid_product_records_each_labor_process_and_uses_good_sets() -> None:
    result = calculate_production_batch_cost(
        context=context("semi_finished_inventory"),
        good_quantity=2000,
        carried_material_costs=(
            CarriedMaterialCostInput(
                "SEMI-LOT-1", "semi_finished_inventory", "BOM-COVER",
                "COST-SNAPSHOT-1", Decimal("400"), "领用已确认半成品库存成本",
            ),
        ),
        labor_operations=(
            LaborCostInput("LABOR-DIECUT", "人工模切", 1, Decimal("8"), rate("30")),
            LaborCostInput("LABOR-CLEAN", "清理模切废料", 2, Decimal("8"), rate("20")),
            LaborCostInput("LABOR-ASSEMBLE", "插卡组装", 7, Decimal("8"), rate("25")),
        ),
    )

    assert result.total_labor_hours == Decimal("80")
    assert result.material_cost == Decimal("400.0000")
    assert result.labor_cost == Decimal("1960.0000")
    assert result.unit_complete_cost == Decimal("1.1800")
    assert [row.process_name for row in result.labor_details] == [
        "人工模切",
        "清理模切废料",
        "插卡组装",
    ]


def test_complete_batch_uses_actual_material_sheets_and_all_components() -> None:
    result = calculate_production_batch_cost(
        context=context("actual_material_input"),
        good_quantity=100,
        materials=(
            MaterialCostInput("LOT-COVER", "BOM-COVER", "盖", Decimal("1000"), Decimal("800"), 102, rate("2")),
            MaterialCostInput("LOT-BASE", "BOM-BASE", "底", Decimal("900"), Decimal("700"), 101, rate("2")),
        ),
        printing_operations=(
            PrintingCostInput("PRINT-COVER", "BOM-COVER", "盖", 102, 2, "double", 1, 1, rate("0.02"), rate("10")),
        ),
        diecut_operations=(
            DiecutCostInput("DIECUT-COVER", "BOM-COVER", "盖", 102, "normal", 1, 1, rate("0.03"), rate("20")),
            DiecutCostInput("DIECUT-BASE", "BOM-BASE", "底", 101, "normal", 1, 0, rate("0.03"), rate("20")),
        ),
        labor_operations=(LaborCostInput("LABOR-GLUE", "粘箱", 2, Decimal("4"), rate("20")),),
        other_costs=(OtherCostInput("OTHER-1", "打包耗材", Decimal("15"), "本批实际领用"),),
    )

    assert len(result.material_details) == 2
    assert result.total_batch_cost == Decimal("503.5900")
    assert result.unit_complete_cost == Decimal("5.0359")


def test_manual_rate_override_requires_reason_and_preserves_both_values() -> None:
    with pytest.raises(ProductionCostError, match="必须填写原因"):
        calculate_production_batch_cost(
            context=context(),
            good_quantity=10,
            carried_material_costs=customer_supplied_material(),
            labor_operations=(
                LaborCostInput(
                    "LABOR-ASSEMBLE",
                    "插卡",
                    1,
                    Decimal("1"),
                    RateSelection(Decimal("20"), Decimal("25")),
                ),
            ),
        )

    result = calculate_production_batch_cost(
        context=context(),
        good_quantity=10,
        carried_material_costs=customer_supplied_material(),
        labor_operations=(
            LaborCostInput(
                "LABOR-ASSEMBLE",
                "插卡",
                1,
                Decimal("1"),
                RateSelection(Decimal("20"), Decimal("25"), "本批人工难度较高"),
            ),
        ),
    )
    detail = result.labor_details[0]
    assert detail.default_rate == Decimal("20")
    assert detail.applied_rate == Decimal("25")
    assert detail.override_reason == "本批人工难度较高"


@pytest.mark.parametrize(
    ("colors", "tier"),
    ((1, "double"), (2, "multi"), (3, "single")),
)
def test_color_count_and_tier_must_be_consistent(colors: int, tier: str) -> None:
    with pytest.raises(ProductionCostError, match="色数档"):
        calculate_production_batch_cost(
            context=context(),
            good_quantity=10,
            carried_material_costs=customer_supplied_material(),
            printing_operations=(
                PrintingCostInput("PRINT-TEST", "BOM-TEST", "测试", 10, colors, tier, 1, 1, rate("0.01"), rate("1")),
            ),
        )


def test_zero_good_quantity_cannot_hide_batch_cost() -> None:
    with pytest.raises(ProductionCostError, match="合格成品数量必须是正数"):
        calculate_production_batch_cost(context=context(), good_quantity=0)


def test_printing_and_diecut_override_reasons_are_preserved() -> None:
    overridden = RateSelection(Decimal("0.02"), Decimal("0.03"), "本批复杂加工")
    result = calculate_production_batch_cost(
        context=context(),
        good_quantity=10,
        carried_material_costs=customer_supplied_material(),
        printing_operations=(
            PrintingCostInput(
                "PRINT-1", "BOM-1", "产品", 10, 2, "double", 1, 1,
                overridden, rate("10"),
            ),
        ),
        diecut_operations=(
            DiecutCostInput(
                "DIECUT-1", "BOM-1", "产品", 10, "oversized", 2, 1,
                overridden, rate("20"),
            ),
        ),
    )

    assert result.printing_details[0].variable_override_reason == "本批复杂加工"
    assert result.diecut_details[0].variable_override_reason == "本批复杂加工"


def test_breakdown_details_are_immutable() -> None:
    result = calculate_production_batch_cost(
        context=context("actual_material_input"),
        good_quantity=10,
        materials=(
            MaterialCostInput(
                "LOT-1", "BOM-1", "产品", Decimal("1000"), Decimal("500"), 10, rate("2")
            ),
        ),
    )

    with pytest.raises(FrozenInstanceError):
        result.material_details[0].amount = Decimal("999")


def test_other_cost_requires_named_audit_detail() -> None:
    with pytest.raises(ProductionCostError, match="编号、类别和原因"):
        calculate_production_batch_cost(
            context=context(),
            good_quantity=10,
            carried_material_costs=customer_supplied_material(),
            other_costs=(OtherCostInput("", "", Decimal("15"), ""),),
        )

    result = calculate_production_batch_cost(
        context=context(),
        good_quantity=10,
        carried_material_costs=customer_supplied_material(),
        other_costs=(
            OtherCostInput("OTHER-1", "打包耗材", Decimal("15"), "本批实际领用", "V-001"),
        ),
    )
    assert result.other_cost == Decimal("15.0000")
    assert result.other_cost_details[0].voucher_reference == "V-001"


def test_rounding_occurs_after_aggregating_detail_values() -> None:
    split = calculate_production_batch_cost(
        context=context(),
        good_quantity=1,
        carried_material_costs=customer_supplied_material(),
        labor_operations=(
            LaborCostInput("LABOR-1", "工序一", 1, Decimal("1"), rate("0.00006")),
            LaborCostInput("LABOR-2", "工序二", 1, Decimal("1"), rate("0.00006")),
        ),
    )
    merged = calculate_production_batch_cost(
        context=context(),
        good_quantity=1,
        carried_material_costs=customer_supplied_material(),
        labor_operations=(
            LaborCostInput("LABOR-ALL", "合并工序", 1, Decimal("2"), rate("0.00006")),
        ),
    )
    assert split.labor_cost == merged.labor_cost == Decimal("0.0001")

    cross_category = calculate_production_batch_cost(
        context=context(),
        good_quantity=1,
        carried_material_costs=customer_supplied_material(),
        labor_operations=(
            LaborCostInput("LABOR-TINY", "微小人工", 1, Decimal("1"), rate("0.00006")),
        ),
        other_costs=(
            OtherCostInput("OTHER-TINY", "微小费用", Decimal("0.00006"), "舍入边界测试"),
        ),
    )
    assert cross_category.total_batch_cost == Decimal("0.0001")
    assert cross_category.rounding_adjustment == Decimal("-0.0001")
    assert (
        cross_category.material_cost
        + cross_category.printing_cost
        + cross_category.diecut_cost
        + cross_category.labor_cost
        + cross_category.other_cost
        + cross_category.rounding_adjustment
    ) == cross_category.total_batch_cost


def test_actual_material_input_cannot_have_zero_material_rows() -> None:
    with pytest.raises(ProductionCostError, match="至少需要一条片料成本明细"):
        calculate_production_batch_cost(
            context=context("actual_material_input"),
            good_quantity=10,
        )


@pytest.mark.parametrize(
    "source_kind",
    ("semi_finished_inventory", "finished_inventory", "customer_supplied"),
)
def test_non_raw_material_sources_require_explicit_cost_basis(source_kind: str) -> None:
    with pytest.raises(ProductionCostError, match="必须提供明确的承接材料成本依据"):
        calculate_production_batch_cost(
            context=context(source_kind),
            good_quantity=10,
        )


def test_inventory_carryover_requires_confirmed_snapshot_but_customer_supplied_can_be_zero() -> None:
    with pytest.raises(ProductionCostError, match="必须关联已确认成本快照"):
        calculate_production_batch_cost(
            context=context("semi_finished_inventory"),
            good_quantity=10,
            carried_material_costs=(
                CarriedMaterialCostInput(
                    "SEMI-LOT-1", "semi_finished_inventory", "BOM-1", None,
                    Decimal("20"), "领用半成品库存",
                ),
            ),
        )

    result = calculate_production_batch_cost(
        context=context(),
        good_quantity=10,
        carried_material_costs=customer_supplied_material(),
    )
    assert result.material_cost == Decimal("0.0000")
    assert result.carried_material_details[0].reason == "客户提供材料，材料成本不计入本批"

    with pytest.raises(ProductionCostError, match="实际投料张数必须是正数"):
        calculate_production_batch_cost(
            context=context("actual_material_input"),
            good_quantity=10,
            materials=(
                MaterialCostInput(
                    "LOT-EMPTY", "BOM-1", "产品", Decimal("1000"), Decimal("500"), 0, rate("2")
                ),
            ),
        )
