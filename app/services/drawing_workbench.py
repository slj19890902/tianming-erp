"""Presentation-neutral contract for the parameterised drawing workbench."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from app.services.drawing_geometry import PARAMETER_KEYS, DrawingGeometryError, build_geometry, plain


WORKBENCH_KEY = "__drawing_workbench_v1"
CATALOG_VERSION = "drawing-workbench-v1"
DIMENSION_BASIS = (
    {"key": "inner", "label": "内尺寸"},
    {"key": "outer", "label": "外尺寸"},
    {"key": "dieline", "label": "刀线尺寸"},
)
TEMPLATE_LABELS = {
    "liner_v1": "衬板",
    "slotted_v1": "单片开槽箱 A1/0201",
    "custom_21301634_v1": "客户定制内衬",
}


def split_editor_state(parameters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Keep editor-only state in a reserved namespace without schema migration."""
    values = dict(parameters)
    raw = values.pop(WORKBENCH_KEY, None)
    if raw is None:
        return values, None
    if not isinstance(raw, dict):
        raise DrawingGeometryError("图纸工作台状态格式无效")
    state = raw.get("editor_state", raw)
    if not isinstance(state, dict):
        raise DrawingGeometryError("图纸工作台状态格式无效")
    return values, deepcopy(state)


def validate_editor_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    if not isinstance(state, dict):
        raise DrawingGeometryError("图纸工作台状态格式无效")
    result = deepcopy(state)
    version = result.setdefault("schema_version", CATALOG_VERSION)
    if version != CATALOG_VERSION:
        raise DrawingGeometryError("图纸工作台状态版本不支持")
    basis = result.setdefault("dimension_basis", "dieline")
    if basis not in {item["key"] for item in DIMENSION_BASIS}:
        raise DrawingGeometryError("尺寸口径必须为内尺寸、外尺寸或刀线尺寸")
    overrides = result.setdefault("local_overrides", {})
    if not isinstance(overrides, dict) or len(overrides) > 50:
        raise DrawingGeometryError("局部参数格式无效或过多")
    assembly = result.get("assembly")
    if assembly is not None and not isinstance(assembly, dict):
        raise DrawingGeometryError("组合预览状态格式无效")
    return result


def with_editor_state(parameters: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(parameters)
    if state is not None:
        result[WORKBENCH_KEY] = {"editor_state": validate_editor_state(state)}
    return result


def _parameter_label(key: str) -> str:
    return key.replace("_mm", "").replace("_", " ")


def template_catalog() -> dict[str, Any]:
    templates = []
    for key, label in TEMPLATE_LABELS.items():
        fields = [{"key": name, "label": _parameter_label(name), "unit": "mm",
                   "min_mm": "0.01", "max_mm": "10000", "required": True,
                   "derived": False, "depends_on": []}
                  for name in PARAMETER_KEYS[key]]
        templates.append({"key": key, "label": label,
                          "description": "尺寸为图纸参数，未确认加工补偿；不改产品或采购尺寸。",
                          "parameter_schema": fields, "editable_dimensions": fields,
                          "capabilities": {"preview": True, "fold_3d": True,
                                           "export_svg": True, "export_pdf_1to1": True,
                                           "export_dxf": True}})
    return {"catalog_version": CATALOG_VERSION, "unit": "mm",
            "dimension_basis_options": list(DIMENSION_BASIS), "templates": templates}


def fold_model(template_key: str, geometry: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable parent hinge axes used by the browser's same-geometry folding view."""
    panels = {panel["id"]: panel for panel in geometry.get("fold_panels", geometry["panels"])}
    def hinge(identifier: str, parent: str, child: str, axis: tuple[Decimal, Decimal, Decimal, Decimal],
              order: int, direction: int) -> dict[str, Any]:
        return {"id": identifier, "parent_panel_id": parent, "child_panel_id": child,
                "parent_hinge_axis": {"x1_mm": plain(axis[0]), "y1_mm": plain(axis[1]),
                                      "x2_mm": plain(axis[2]), "y2_mm": plain(axis[3])},
                "max_angle_deg": 90, "order": order, "direction": direction}
    if template_key == "liner_v1":
        return []
    if template_key == "slotted_v1":
        result = []
        for index in range(1, 4):
            left = panels[f"wall_{index}"]
            x = Decimal(left["x"]) + Decimal(left["width"])
            y, h = Decimal(left["y"]), Decimal(left["height"])
            result.append(hinge(f"hinge-wall-{index}-{index + 1}", left["id"], f"wall_{index + 1}",
                                (x, y, x, y+h), index, 1))
        wall4 = panels["wall_4"]
        x, y, h = Decimal(wall4["x"]) + Decimal(wall4["width"]), Decimal(wall4["y"]), Decimal(wall4["height"])
        result.append(hinge("hinge-wall-4-glue", "wall_4", "glue_tab", (x, y, x, y+h), 4, 1))
        for index in range(1, 5):
            wall = panels[f"wall_{index}"]
            x, y, w, h = (Decimal(wall[key]) for key in ("x", "y", "width", "height"))
            top_flap, bottom_flap = panels[f"top_flap_{index}"], panels[f"bottom_flap_{index}"]
            tx, tw = Decimal(top_flap["x"]), Decimal(top_flap["width"])
            bx, bw = Decimal(bottom_flap["x"]), Decimal(bottom_flap["width"])
            result.append(hinge(f"hinge-wall-{index}-top", wall["id"], top_flap["id"],
                                (tx, y, tx+tw, y), 10+index, -1))
            result.append(hinge(f"hinge-wall-{index}-bottom", wall["id"], bottom_flap["id"],
                                (bx, y+h, bx+bw, y+h), 20+index, 1))
        return result
    center = panels["center"]
    x, y, w, h = (Decimal(center[key]) for key in ("x", "y", "width", "height"))
    return [
        hinge("hinge-center-top-fold", "center", "top_fold", (x, y, x+w, y), 1, -1),
        hinge("hinge-top-fold-cover", "top_fold", "top_cover", (x, Decimal(panels["top_fold"]["y"]), x+w, Decimal(panels["top_fold"]["y"])), 2, -1),
        hinge("hinge-center-bottom-fold", "center", "bottom_fold", (x, y+h, x+w, y+h), 3, 1),
        hinge("hinge-bottom-fold-cover", "bottom_fold", "bottom_cover", (x, Decimal(panels["bottom_cover"]["y"]), x+w, Decimal(panels["bottom_cover"]["y"])), 4, 1),
        hinge("hinge-center-left-fold", "center", "left_fold", (x, y, x, y+h), 5, -1),
        hinge("hinge-left-fold-wing", "left_fold", "left_wing", (Decimal(panels["left_fold"]["x"]), y, Decimal(panels["left_fold"]["x"]), y+h), 6, -1),
        hinge("hinge-center-right-fold", "center", "right_fold", (x+w, y, x+w, y+h), 7, 1),
        hinge("hinge-right-fold-wing", "right_fold", "right_wing", (Decimal(panels["right_wing"]["x"]), y, Decimal(panels["right_wing"]["x"]), y+h), 8, 1),
    ]


def preview_payload(template_key: str, parameters: dict[str, Any], editor_state: dict[str, Any] | None = None) -> dict[str, Any]:
    geometry = build_geometry(template_key, parameters)
    dimension_ids = {entry["name"]: entry["id"] for entry in geometry.get("dimension_index", [])}
    editable = []
    for key in PARAMETER_KEYS[template_key]:
        value = parameters.get(key)
        editable.append({"key": key, "label": _parameter_label(key), "unit": "mm",
                         "value_mm": None if value is None else str(value), "source": "parameter",
                         "editable": True, "derived": False, "derived_from": [],
                         "bounds": {"min_mm": "0.01", "max_mm": "10000"},
                         "geometry_refs": {"dimension_ids": [dimension_ids[key]] if key in dimension_ids else [],
                                           "panel_ids": [], "segment_ids": []}})
    basis = (editor_state or {}).get("dimension_basis", "dieline")
    compensation_required = basis != "dieline"
    return {"unit": "mm", "template_key": template_key, "geometry": geometry,
            "editable_dimensions": editable, "panel_hinges": fold_model(template_key, geometry),
            "validation": {"valid": True, "errors": [],
                           "warnings": (["内/外尺寸尚未有已确认加工补偿；几何仍按输入的刀线/压线尺寸显示，不能发布。"]
                                        if compensation_required else [])},
            "dimension_basis": {"key": basis,
                                "label": next(item["label"] for item in DIMENSION_BASIS if item["key"] == basis),
                                "compensation_required": compensation_required,
                                "compensation_status": "未确认加工补偿；当前值未宣称已折算"}}
