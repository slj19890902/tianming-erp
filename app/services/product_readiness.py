from __future__ import annotations

import re
from typing import Any

from app.services.flute_mapping import normalize_flute_type, validate_flute_for_write
from app.services.report_crease import crease_width_error


def _value(source: object, name: str) -> Any:
    return getattr(source, name, None)


def is_telescoping_lid_box(box_style: str | None) -> bool:
    value = str(box_style or "").strip().upper()
    return "A3" in value or "天地盖" in str(box_style or "")


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
    if layer_count not in {3, 5, 7}:
        missing.append(("layer_count", "材质层数未填写或不合法"))
    if not flute_type:
        missing.append(("flute_type", "楞型未填写"))
    elif layer_count in {3, 5, 7}:
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
