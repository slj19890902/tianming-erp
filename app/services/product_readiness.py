from __future__ import annotations

import json
import re
from typing import Any

from app.services.flute_mapping import normalize_flute_type, validate_flute_for_write
from app.services.report_crease import crease_width_error


def _value(source: object, name: str) -> Any:
    return getattr(source, name, None)


def is_telescoping_lid_box(box_style: str | None) -> bool:
    value = str(box_style or "").strip().upper()
    return "A3" in value or "天地盖" in str(box_style or "")


def _order_save_missing(product: object, missing: list[tuple[str, str]]) -> list[str]:
    if _value(product, "is_virtual_composite_parent") or _value(product, "supply_mode") == "external_purchase":
        return []
    # An assembled parent has no own board; existing BOM validation handles its graph.
    from sqlalchemy import inspect
    from sqlalchemy.orm import object_session
    if inspect(product, raiseerr=False) is not None:
        session = object_session(product)
        if session is not None and _value(product, "id"):
            from app.models.multilevel_bom import ProductBomProfile
            profile = session.get(ProductBomProfile, product.id)
            if profile is not None and profile.source == "assembled":
                return []
    required = {"material", "material_code", "supplier", "report_length_mm", "report_width_mm",
                "base_report_length_mm", "base_report_width_mm"}
    return [label for field, label in missing if field in required]


def product_readiness(product: object) -> dict[str, object]:
    """Return a display-only readiness result for a common-box master record.

    This deliberately ignores ``manual_modified``: edit history is not proof that
    the master data can safely create a requisition.
    """
    missing: list[tuple[str, str]] = []
    if not str(_value(product, "product_code") or "").strip():
        missing.append(("product_code", "存货编码未填写"))
    if not str(_value(product, "product_name") or "").strip():
        missing.append(("product_name", "产品名称未填写"))

    if bool(_value(product, "is_virtual_composite_parent")):
        if not bool(_value(product, "is_composite")):
            missing.append(("bom_components", "虚拟组合套装尚未配置 BOM 组件"))
        fields = [field for field, _label in missing]
        labels = [label for _field, label in missing]
        return {
            "ready": not missing,
            "order_save_missing_labels": [],
            "status": "资料已完善" if not missing else "待完善",
            "missing_fields": fields,
            "missing_labels": labels,
        }

    supply_mode = str(_value(product, "supply_mode") or "corrugated_production").strip()
    if supply_mode == "external_purchase":
        if not str(_value(product, "external_packaging_category_code") or "").strip():
            missing.append(("external_packaging_category_code", "包材类别未填写"))
        if not str(_value(product, "external_packaging_specification_summary") or "").strip():
            missing.append(("external_packaging_specification_summary", "包材规格未填写"))
        if not str(_value(product, "external_packaging_purchase_unit") or "").strip():
            missing.append(("external_packaging_purchase_unit", "采购单位未填写"))
        raw_candidates = _value(product, "external_packaging_candidate_snapshot_json")
        try:
            candidates = json.loads(str(raw_candidates or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            candidates = []
        if not isinstance(candidates, list) or not candidates:
            missing.append(("external_supply", "候选供应商产品未选择"))
        elif sum(1 for row in candidates if isinstance(row, dict) and row.get("is_default") is True) != 1:
            missing.append(("external_supply_default", "默认供应商产品未明确"))
        fields = [field for field, _label in missing]
        labels = [label for _field, label in missing]
        return {
            "ready": not missing,
            "order_save_missing_labels": [],
            "status": "资料已完善" if not missing else "待完善",
            "missing_fields": fields,
            "missing_labels": labels,
        }
    material = _value(product, "material")
    material_code = str(_value(material, "code") or "").strip()
    supplier = str(_value(material, "supplier_name") or "").strip()
    if material is None or not bool(_value(material, "is_active")):
        missing.append(("material", "材质主数据未启用"))
    elif not material_code:
        missing.append(("material_code", "材质代码未填写"))
    elif not supplier:
        missing.append(("supplier", "材质供应商未填写"))

    layer_count = _value(product, "layer_count")
    flute_type = normalize_flute_type(_value(product, "flute_type"))
    if layer_count not in {1, 3, 5, 7}:
        missing.append(("layer_count", "材质层数未填写或不合法"))
    if not flute_type:
        missing.append(("flute_type", "楞型未填写"))
    elif layer_count in {1, 3, 5, 7}:
        flute_error = validate_flute_for_write(flute_type, layer_count)
        if flute_error:
            missing.append(("layer_flute", flute_error))

    def require_dimensions(prefix: str, label: str) -> None:
        length = _value(product, f"{prefix}report_length_mm")
        width = _value(product, f"{prefix}report_width_mm")
        try:
            has_length = length is not None and float(length) > 0
        except (TypeError, ValueError):
            has_length = False
        try:
            has_width = width is not None and float(width) > 0
        except (TypeError, ValueError):
            has_width = False
        if not has_length:
            missing.append((f"{prefix}report_length_mm", f"{label}报料长未填写"))
        if not has_width:
            missing.append((f"{prefix}report_width_mm", f"{label}报料宽未填写"))
        crease_type = str(_value(product, f"{prefix}crease_type") or "").strip()
        if not crease_type:
            missing.append((f"{prefix}crease_type", f"{label}压线类型未填写"))
        else:
            error = crease_width_error(
                label=f"{label}压线",
                crease_type=crease_type,
                report_width_mm=width,
                left_mm=_value(product, f"{prefix}crease_left_mm"),
                middle_mm=_value(product, f"{prefix}crease_middle_mm"),
                right_mm=_value(product, f"{prefix}crease_right_mm"),
            )
            if error:
                missing.append((f"{prefix}crease", error))

    require_dimensions("", "主料")
    if is_telescoping_lid_box(_value(product, "box_style")):
        require_dimensions("base_", "底料")
    fields = [field for field, _label in missing]
    labels = [label for _field, label in missing]
    return {
        "ready": not missing,
        "order_save_missing_labels": _order_save_missing(product, missing),
        "status": "资料已完善" if not missing else "待完善",
        "missing_fields": fields,
        "missing_labels": labels,
    }


def material_code_token(value: str | None) -> str:
    """Normalize only the first material-code token; ignore /AB layer suffixes."""
    token = re.split(r"[\s|｜,，;；]", str(value or "").strip(), maxsplit=1)[0]
    token = token.split("/")[0].split("-")[0].strip().upper()
    return token


def material_comparison(pdf_material: str | None, product_material: str | None) -> str:
    left = material_code_token(pdf_material)
    right = material_code_token(product_material)
    if not left or not right:
        return "unknown"
    return "same" if left == right else "different"
