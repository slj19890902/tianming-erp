"""Shared frozen BOM sheet-yield contract for planning and procurement."""
from dataclasses import dataclass

from app.services.composite_bom_execution import CompositeBOMExecutionError, require_positive_integer
from app.services.requisition_quantities import (
    CUTTING_MODE_BOX_STYLES, DEFAULT_CUTTING_MODE, cutting_factor, normalize_cutting_mode,
)


@dataclass(frozen=True)
class BomSheetYield:
    cutting_mode: str
    cutting_factor: int
    yield_per_sheet: int
    actual_yield_per_sheet: int | None


def resolve_bom_sheet_yield(snapshot, *, cutting_mode=None, actual_yield_per_sheet=None,
                           strict=False, component_type="whole", sheet_cutting_contract=None):
    """One yield, never cutting-factor times mold-yield.

    Preserve existing requisition precedence: explicit actual mold yield,
    supported cutting override/default (>1), mold maximum, then one.
    A3/ordinary styles do not inherit stale board-cutting metadata.
    """
    maximum = snapshot.mold_max_yield_per_sheet
    if snapshot.is_die_cut and maximum is not None:
        maximum = require_positive_integer(maximum, label="模具最大出数")
    if actual_yield_per_sheet is not None:
        actual_yield_per_sheet = require_positive_integer(actual_yield_per_sheet, label="实际模切出数")
        if not snapshot.is_die_cut:
            raise CompositeBOMExecutionError("非模切组件不能填写实际模切出数")
        if maximum is None:
            raise CompositeBOMExecutionError("模切组件缺少最大模切出数")
        if actual_yield_per_sheet > maximum:
            raise CompositeBOMExecutionError("实际模切出数不能超过模具最大出数")
    from app.services.sheet_cutting_settings import component_settings
    settings = component_settings(getattr(snapshot, "sheet_cutting_settings_snapshot", None), component_type)
    if settings is not None:
        if settings.is_die_cut != bool(snapshot.is_die_cut):
            raise CompositeBOMExecutionError("组件模切设置与冻结开料设置不一致")
        mold_count = actual_yield_per_sheet or settings.mold_count
        if maximum is not None and mold_count > maximum:
            raise CompositeBOMExecutionError("组件模数不能超过模具最大出数")
        length_parts = sheet_cutting_contract.length_parts if sheet_cutting_contract else settings.length_parts
        width_parts = sheet_cutting_contract.width_parts if sheet_cutting_contract else settings.width_parts
        output = length_parts * width_parts * mold_count
        mode = normalize_cutting_mode(output, strict=True)
        if cutting_mode is not None and normalize_cutting_mode(cutting_mode, strict=True) != mode:
            raise CompositeBOMExecutionError("独立开料组件须同时核对长宽份数和模数，不能单独覆盖每张产出")
        return BomSheetYield(mode, length_parts * width_parts, output, actual_yield_per_sheet)
    supported = (snapshot.snapshot_component_box_style or "").strip() in CUTTING_MODE_BOX_STYLES
    mode = (cutting_mode or snapshot.snapshot_component_default_cutting_mode or DEFAULT_CUTTING_MODE) if supported else DEFAULT_CUTTING_MODE
    mode = normalize_cutting_mode(mode, strict=strict)
    factor = cutting_factor(mode)
    if actual_yield_per_sheet is not None:
        output = actual_yield_per_sheet
    elif factor > 1:
        if snapshot.is_die_cut and maximum is not None and factor > maximum:
            raise CompositeBOMExecutionError("默认开料每张产出不能超过模具最大出数")
        output = factor
    elif snapshot.is_die_cut and maximum is not None:
        output = maximum
    else:
        output = 1
    return BomSheetYield(mode, factor, output, actual_yield_per_sheet)
