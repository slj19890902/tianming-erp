from __future__ import annotations

import json
import re
from typing import Any

from app.services.flute_mapping import normalize_flute_type, validate_flute_for_write
from app.services.report_crease import crease_width_error


def _issue(field: str, label: str, reason: str, source: str, *,
           repair_page: str | None = None, repairable: bool = True,
           product: object | None = None) -> dict[str, object]:
    return {
        "field": field,
        "label": label,
        "reason": reason,
        "source": source,
        "repair_page": repair_page,
        "repair_field": field,
        "repairable": repairable,
        "product_id": _value(product, "id") if product is not None else None,
        "product_code": _value(product, "product_code") if product is not None else None,
        "product_name": _value(product, "product_name") if product is not None else None,
    }


def inventory_entry_issues(
    product: object | None,
    *,
    inventory_type: str,
    basis: dict | None = None,
    has_legal_frozen_spec: bool = False,
    semi_length: object | None = None,
    semi_width: object | None = None,
    semi_material: object | None = None,
) -> list[dict[str, object]]:
    """Return operation-specific inventory-entry gaps.

    This is deliberately separate from requisition readiness.  In particular,
    report sheet dimensions never stand in for a finished product's dimensions.
    The transaction guard and entry preflight use this same representation.
    """
    basis = basis if isinstance(basis, dict) else {}
    if inventory_type == "semi_finished":
        result = []
        for field, label, value in (
            ("board_length_mm", "片料长度", semi_length),
            ("board_width_mm", "片料宽度", semi_width),
            ("material_code_snapshot", "片料材质", semi_material),
        ):
            if not str(value or "").strip() or (field != "material_code_snapshot" and not _positive(value)):
                result.append(_issue(field, label, "半成品入库必须保留实际片料规格",
                                     "entry", repairable=False, product=product))
        return result
    if product is None:
        return [_issue("product_id", "真实产品", "本次入库没有可核验的真实产品身份",
                       "entry", repairable=False)]
    if inventory_type != "finished":
        return []

    profile = _bom_profile(product, None)
    pure_assembly = bool(profile is not None and profile.source == "assembled"
                         and profile.delivery_mode == "parent")
    if pure_assembly:
        if has_legal_frozen_spec:
            return []
        return [_issue("assembly_evidence", "真实子件组套依据",
                       "纯组套父件必须由本次真实消耗的子件和有效冻结配方形成",
                       "assembly", repairable=False, product=product)]

    supply_mode = str(_value(product, "supply_mode") or "corrugated_production").strip()
    if supply_mode == "external_purchase":
        if has_legal_frozen_spec or str(basis.get("spec") or "").strip():
            return []
        return [_issue("external_packaging_specification_summary", "冻结外购规格",
                       "外购品入库需要本次采购/收料冻结的适用规格",
                       "frozen_source", repair_page="/?page=products", product=product)]

    if has_legal_frozen_spec or str(basis.get("spec") or "").strip():
        return []
    result = []
    if not _positive(_value(product, "length_mm")):
        result.append(_issue("length_mm", "成品长度", "普通成品入库需要结构化成品规格；报料长度不能代替",
                             "product_master", repair_page="/?page=products", product=product))
    if not _positive(_value(product, "width_mm")):
        result.append(_issue("width_mm", "成品宽度", "普通成品入库需要结构化成品规格；报料宽度不能代替",
                             "product_master", repair_page="/?page=products", product=product))
    if not result:
        result.append(_issue("physical_basis_json", "实物规格依据",
                             "产品主档规格完整，但本次入库未正确生成实物规格依据",
                             "entry_snapshot", repairable=False, product=product))
    return result


def _positive(value: object) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _value(source: object, name: str) -> Any:
    return getattr(source, name, None)


def is_telescoping_lid_box(box_style: str | None) -> bool:
    from app.services.box_type_rules import get_box_type_rule
    rule = get_box_type_rule(box_style)
    if rule is not None:
        return rule.code == "a3_set"
    value = str(box_style or "").strip().upper()
    return "A3" in value or "天地盖" in str(box_style or "")


def _bom_profile(product: object, profiles: dict | None):
    if profiles is not None:
        return profiles.get(_value(product, "id"))
    from sqlalchemy import inspect
    from sqlalchemy.orm import object_session
    from app.models.multilevel_bom import ProductBomProfile
    session = object_session(product) if inspect(product, raiseerr=False) is not None else None
    return session.get(ProductBomProfile, product.id) if session is not None and _value(product, "id") else None


def _order_save_missing(product: object, missing: list[tuple[str, str]], profiles: dict | None) -> list[str]:
    if _value(product, "is_virtual_composite_parent") or _value(product, "supply_mode") == "external_purchase":
        return []
    # An assembled parent has no own board; existing BOM validation handles its graph.
    profile = _bom_profile(product, profiles)
    if profile is not None and profile.source == "assembled":
        return []
    required = {"material", "material_code", "supplier", "report_length_mm", "report_width_mm",
                "base_report_length_mm", "base_report_width_mm"}
    return [label for field, label in missing if field in required]


def _master_field_readiness(product: object, *, bom_profiles: dict | None = None) -> dict[str, object]:
    """Return a display-only readiness result for a common-box master record.

    This deliberately ignores ``manual_modified``: edit history is not proof that
    the master data can safely create a requisition.
    """
    missing: list[tuple[str, str]] = []
    from app.services.legacy_product_classification import classification
    category = classification(product)
    if category["needs_supply_completion"] or category["kind"] == "expense":
        label = (category["label"] + "：请完善真实规格、采购单位和供应商产品"
                 if category["needs_supply_completion"] else "模具费属于费用项目，请通过费用入口登记")
        return {"ready": False, "order_save_missing_labels": [label],
                "status": "外购资料待完善" if category["needs_supply_completion"] else "费用项目",
                "readiness_scope": "requisition",
                "missing_fields": ["external_supply" if category["needs_supply_completion"] else "expense_item"],
                "missing_labels": [label]}
    if not str(_value(product, "product_code") or "").strip():
        missing.append(("product_code", "存货编码未填写"))
    if not str(_value(product, "product_name") or "").strip():
        missing.append(("product_name", "产品名称未填写"))

    profile = _bom_profile(product, bom_profiles)
    if profile is not None and profile.source == "assembled":
        if not bool(_value(product, "is_composite")) or profile.delivery_mode == "components":
            missing.append(("bom_components", "纯组套父件的有效 BOM 配方尚未保存"))
        if not str(_value(product, "unit") or "").strip():
            missing.append(("unit", "成品单位未填写"))
        return {
            "ready": not missing,
            "order_save_missing_labels": [label for _, label in missing],
            "status": "报料资料完整" if not missing else "报料资料待完善",
            "readiness_scope": "requisition",
            "missing_fields": [field for field, _ in missing],
            "missing_labels": [label for _, label in missing],
        }

    if _value(product, "box_style") == "BOM组合":
        if not profile or profile.source != "assembled" or profile.delivery_mode != "parent" or not _value(product, "is_composite"):
            missing.append(("bom_components", "组套配方尚未保存"))
        return {
            "ready": not missing,
            "order_save_missing_labels": [label for _, label in missing],
            "status": "报料资料完整" if not missing else "报料资料待完善",
            "readiness_scope": "requisition",
            "missing_fields": [field for field, _ in missing],
            "missing_labels": [label for _, label in missing],
        }

    if bool(_value(product, "is_virtual_composite_parent")):
        if not bool(_value(product, "is_composite")):
            missing.append(("bom_components", "虚拟组合套装尚未配置 BOM 组件"))
        fields = [field for field, _label in missing]
        labels = [label for _field, label in missing]
        return {
            "ready": not missing,
            "order_save_missing_labels": [],
            "status": "报料资料完整" if not missing else "报料资料待完善",
            "readiness_scope": "requisition",
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
            "status": "报料资料完整" if not missing else "报料资料待完善",
            "readiness_scope": "requisition",
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
        "order_save_missing_labels": _order_save_missing(product, missing, bom_profiles),
        "status": "报料资料完整" if not missing else "报料资料待完善",
            "readiness_scope": "requisition",
        "missing_fields": fields,
        "missing_labels": labels,
    }


def product_readiness(product: object, *, bom_profiles: dict | None = None) -> dict[str, object]:
    """BOM readiness includes its real editable graph and entry reference data.

    Availability/reservations and historical lot identity remain transaction
    facts; they must not masquerade as missing product master fields.
    """
    result = _master_field_readiness(product, bom_profiles=bom_profiles)
    profile = _bom_profile(product, bom_profiles)
    if profile is None or not _value(product, 'is_composite'):
        return result
    from sqlalchemy.orm import object_session
    from sqlalchemy import inspect
    db = object_session(product) if inspect(product, raiseerr=False) is not None else None
    if db is None:
        return result
    issues = [_issue(field, label, label, 'product_master',
        repair_page='/?page=products', product=product)
        for field, label in zip(result['missing_fields'], result['missing_labels'])]
    from app.services.multilevel_bom_master import load_master_structure
    try:
        structure = load_master_structure(db, product.id)
    except ValueError as error:
        issues.append(_issue('bom_components', 'BOM关系', str(error), 'bom_master',
            repair_page='/?page=products', product=product))
    else:
        for pid, child in structure['products'].items():
            if pid == product.id:
                continue
            child_result = _master_field_readiness(child)
            for field, label in zip(child_result['missing_fields'],child_result['missing_labels']):
                issues.append(_issue(field, f'子件 {child.product_code}：{label}',
                    label, 'product_master', repair_page='/?page=products', product=child))
        if profile.source == 'assembled':
            from app.services.bom_entry_cost import assembled_entry_cost
            cost = assembled_entry_cost(db, product, for_entry=True)
            for reason in cost.missing:
                target = next((p for p in structure['products'].values()
                    if str(reason).startswith(str(p.product_code)+' · ')), product)
                field = ('material_id' if any(word in reason for word in ('材质','报价','价格','计价','税率')) else 'unit' if '单位' in reason
                         else 'sheet_cutting_settings' if '开料' in reason or '出数' in reason else 'bom_components')
                issues.append(_issue(field, str(reason), str(reason), 'bom_entry_reference',
                    repair_page='/?page=products', product=target))
    unique = {(issue['product_id'],issue['field'],issue['reason']):issue for issue in issues}
    issues = list(unique.values())
    return {**result, 'ready': not issues,
        'status': '资料完整' if not issues else '资料待完善',
        'readiness_scope': 'bom_product_operations', 'issues':issues,
        'missing_fields':[issue['field'] for issue in issues],
        'missing_labels':[issue['label']+('：'+str(issue['reason']) if issue['label']!=issue['reason'] else '') for issue in issues]}


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
