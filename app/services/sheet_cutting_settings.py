"""Explicit v2 defaults; NULL continues the legacy frozen yield contract.

Product report dimensions remain theoretical dimensions. Legacy order fields
(`special_process`, and BOM default cutting mode) remain sheet-to-piece yield
projections for existing stock/cost/production readers. Never use that projection
to calculate v2 supplier dimensions or display it as the supplier cutting mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.services.requisition_quantities import normalize_cutting_mode
from app.services.sheet_cutting_contract import SheetCuttingContract, SheetCuttingContractError, _integer


@dataclass(frozen=True)
class SheetCuttingSettings:
    length_parts: int = 1
    width_parts: int = 1
    mold_count: int = 1
    is_die_cut: bool = False

    def __post_init__(self):
        _integer(self.length_parts, "长向份数")
        _integer(self.width_parts, "宽向份数")
        _integer(self.mold_count, "模数")
        _integer(self.output_per_sheet, "每张供应商纸产出")
        if type(self.is_die_cut) is not bool or (not self.is_die_cut and self.mold_count != 1):
            raise SheetCuttingContractError("非模切产品的模数必须为一")

    @property
    def cutting_mode(self):
        return normalize_cutting_mode(self.length_parts * self.width_parts, strict=True)

    @property
    def output_per_sheet(self):
        return self.length_parts * self.width_parts * self.mold_count

    @property
    def legacy_yield_mode(self):
        return normalize_cutting_mode(self.output_per_sheet, strict=True)

    def contract(self, length, width):
        return SheetCuttingContract(length, width, self.length_parts, self.width_parts,
                                    self.is_die_cut, self.mold_count)

    def to_dict(self):
        return {"length_parts": self.length_parts, "width_parts": self.width_parts,
                "mold_count": self.mold_count, "is_die_cut": self.is_die_cut}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or set(value) != {"length_parts", "width_parts", "mold_count", "is_die_cut"}:
            raise SheetCuttingContractError("开料设置须包含长向份数、宽向份数、模数与模切标记")
        return cls(**value)


def normalize_settings(value):
    """Validate without filling missing components or reinterpreting old labels."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int or value["schema_version"] != 2:
        raise SheetCuttingContractError("开料设置版本无效")
    if set(value) not in ({"schema_version", "whole"}, {"schema_version", "cover", "base"}):
        raise SheetCuttingContractError("天地盖须分别设置盖与底，其他产品须设置整片")
    return {key: 2 if key == "schema_version" else SheetCuttingSettings.from_dict(part).to_dict()
            for key, part in value.items()}


def component_settings(value, component="whole"):
    settings = normalize_settings(value)
    if settings is None:
        return None
    # Existing parent/whole projections of an A3 set use the cover as before;
    # physical base requisitions always request base explicitly.
    key = "cover" if component == "whole" and "cover" in settings else component
    if key not in settings:
        raise SheetCuttingContractError("开料设置缺少当前实物组件")
    return SheetCuttingSettings.from_dict(settings[key])


def product_yield_mode(product, component="whole"):
    settings = component_settings(getattr(product, "sheet_cutting_settings", None), component)
    return settings.legacy_yield_mode if settings else normalize_cutting_mode(product.default_cutting_mode)


def theoretical_product_yield(product, component="whole"):
    from app.services.requisition_quantities import cutting_factor
    setting = component_settings(getattr(product, "sheet_cutting_settings", None), component)
    return setting.mold_count if setting else cutting_factor(product.default_cutting_mode)


def theoretical_order_yield(item, component="whole", legacy_mode=None):
    from app.services.requisition_quantities import cutting_factor
    setting = component_settings(getattr(item, "sheet_cutting_settings_snapshot", None), component)
    return setting.mold_count if setting else cutting_factor(legacy_mode or item.special_process)


def order_yield_mode(item, component="whole"):
    setting = component_settings(getattr(item, "sheet_cutting_settings_snapshot", None), component)
    return setting.legacy_yield_mode if setting else normalize_cutting_mode(item.special_process)


def order_sheet_contract(item, component="whole"):
    settings = component_settings(getattr(item, "sheet_cutting_settings_snapshot", None), component)
    if settings is None:
        return None
    prefix = "snapshot_base_report_" if component == "base" else "snapshot_report_"
    return settings.contract(getattr(item, prefix + "length_mm"), getattr(item, prefix + "width_mm"))


def resolve_order_sheet_contract(item, component="whole", proposal=None):
    original = order_sheet_contract(item, component)
    if proposal is None:
        return original
    if original is None:
        raise SheetCuttingContractError("旧订单尚未确认独立开料资料，不能按新口径报料")
    selected = SheetCuttingContract.from_snapshot(proposal)
    if (selected.theoretical_length_mm, selected.theoretical_width_mm,
        selected.is_die_cut, selected.mold_count) != (
        original.theoretical_length_mm, original.theoretical_width_mm,
        original.is_die_cut, original.mold_count):
        raise SheetCuttingContractError("理论尺寸或模数与订单冻结资料不一致，请刷新后重试")
    return selected


def resolve_bom_sheet_contract(snapshot, component="whole", proposal=None, actual_mold_count=None):
    from dataclasses import replace
    setting = component_settings(getattr(snapshot, "sheet_cutting_settings_snapshot", None), component)
    if setting is None:
        if proposal is not None:
            raise SheetCuttingContractError("旧组件没有独立开料快照，请先核对组件资料")
        return None
    if actual_mold_count is not None:
        setting = replace(setting, mold_count=actual_mold_count)
    prefix = "snapshot_component_base_report_" if component == "base" else "snapshot_component_report_"
    length, width = getattr(snapshot, prefix + "length_mm"), getattr(snapshot, prefix + "width_mm")
    if proposal is None and (length is None or width is None):
        # An incomplete master must remain visible in the pending list. The
        # formal-save dimension gate still rejects it until it is completed.
        return None
    original = setting.contract(length, width)
    if proposal is None:
        return original
    selected = SheetCuttingContract.from_snapshot(proposal)
    if (selected.theoretical_length_mm, selected.theoretical_width_mm, selected.mold_count, selected.is_die_cut) != (original.theoretical_length_mm, original.theoretical_width_mm, original.mold_count, original.is_die_cut):
        raise SheetCuttingContractError("组件理论尺寸或模数与冻结资料不一致，请刷新草稿")
    return selected
