"""Presentation-neutral contract for the parameterised drawing workbench."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import re
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
    "partition_v1": "开槽隔板（长片 / 短片）",
    "assembly_v1": "组合内衬 / 组套",
}
PARAMETER_LABELS = {
    "length_mm": "衬板长", "width_mm": "衬板宽", "height_mm": "隔板高",
    "panel_width_mm": "中间板面宽", "panel_height_mm": "中间板面高",
    "top_cover_mm": "上盖", "bottom_cover_mm": "下盖", "top_fold_mm": "上折边",
    "bottom_fold_mm": "下折边", "left_fold_mm": "左折边", "right_fold_mm": "右折边",
    "left_wing_mm": "左侧翼", "right_wing_mm": "右侧翼", "body_height_mm": "箱身高",
    "top_flap_mm": "上盖片", "bottom_flap_mm": "下盖片", "glue_flap_mm": "接头",
    "slot_width_mm": "槽宽", "slot_depth_mm": "槽深", "slot_pitch_mm": "槽中心间距",
    "slot_offset_mm": "首槽中心距", "slot_count": "插槽数量", "slot_edge": "开槽边",
    **{f"panel_{i}_mm":f"第{i}面宽" for i in range(1,5)},
}
LINKS = {
    "slotted_v1": {"panel_3_mm":("panel_1_mm",1),"panel_4_mm":("panel_2_mm",1),
                    "top_flap_mm":("panel_2_mm",.5),"bottom_flap_mm":("panel_2_mm",.5)},
    "custom_21301634_v1": {"right_fold_mm":("left_fold_mm",1),
        "bottom_cover_mm":("top_cover_mm",1),"bottom_fold_mm":("top_fold_mm",1),"right_wing_mm":("left_wing_mm",1)},
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
    if len(json.dumps(state, default=str)) > 2_000_000:
        raise DrawingGeometryError("图纸工作台状态过大")
    if set(state) - {"schema_version", "dimension_basis", "local_overrides", "assembly"}:
        raise DrawingGeometryError("图纸工作台包含未知状态字段")
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
    for key, value in overrides.items():
        if key not in PARAMETER_LABELS and not re.fullmatch(r"slot_([1-9]|[12][0-9]|3[0-2])_position_mm", key):
            raise DrawingGeometryError("局部参数名称无效")
        if isinstance(value, bool):
            continue
        if not isinstance(value, dict) or set(value) - {"value_mm", "updated_at"}:
            raise DrawingGeometryError("局部参数标记无效")
        if len(str(value.get("updated_at", ""))) > 40 or len(str(value.get("value_mm", ""))) > 30:
            raise DrawingGeometryError("局部参数标记过长")
    assembly = result.get("assembly")
    if assembly is not None and not isinstance(assembly, dict):
        raise DrawingGeometryError("组合预览状态格式无效")
    if assembly is not None:
        if set(assembly) - {"basis_hash", "placements"} or not isinstance(assembly.get("placements", []), list):
            raise DrawingGeometryError("组合预览字段无效")
        if len(assembly.get("placements", [])) > 500 or len(str(assembly.get("basis_hash", ""))) > 64:
            raise DrawingGeometryError("组合预览实例过多或版本无效")
    return result


def with_editor_state(parameters: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(parameters)
    if state is not None:
        result[WORKBENCH_KEY] = {"editor_state": validate_editor_state(state)}
    return result


def _parameter_label(key: str) -> str:
    match = re.fullmatch(r"slot_(\d+)_position_mm", key)
    return f"第{match[1]}槽中心距" if match else PARAMETER_LABELS.get(key, key)


def parameter_field(template, key):
    link = LINKS.get(template, {}).get(key)
    main = {"length_mm", "width_mm", "height_mm", "panel_width_mm", "panel_height_mm",
            "panel_1_mm", "panel_2_mm", "body_height_mm", "left_fold_mm"}
    field = {"key":key,"label":_parameter_label(key),"unit":"mm","min_mm":"0.01","max_mm":"10000",
             "required":True,"derived":False,"group":"main" if key in main else "detail",
             "depends_on":[link[0]] if link else []}
    if template == "partition_v1" and key == "length_mm":
        field["label"] = "隔板长"
    if link:
        field["formula"] = {"source":link[0],"multiplier":link[1]}
    if key == "slot_count":
        field.update(unit="个",min_mm="1",max_mm="32",step="1")
    if key == "slot_edge":
        field.update(unit="",min_mm="0",max_mm="1",step="1",options=[{"value":0,"label":"上边"},{"value":1,"label":"下边"}])
    return field


def template_catalog() -> dict[str, Any]:
    templates = []
    for key, label in TEMPLATE_LABELS.items():
        fields = [parameter_field(key,name) for name in PARAMETER_KEYS[key]]
        templates.append({"key": key, "label": label,
                          "description": "",
                          "parameter_schema": fields, "editable_dimensions": fields,
                          "capabilities": {"preview": True, "fold_3d": True,
                                           "export_svg": key != "assembly_v1", "export_pdf_1to1": key != "assembly_v1",
                                           "export_dxf": key != "assembly_v1"}})
    return {"catalog_version": CATALOG_VERSION, "unit": "mm",
            "dimension_basis_options": list(DIMENSION_BASIS), "templates": templates}


def fold_model(template_key: str, geometry: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable parent hinge axes used by the browser's same-geometry folding view."""
    if template_key in {"liner_v1", "partition_v1", "assembly_v1"}:
        return []
    geometry = with_frozen_fold_panels(template_key, geometry)
    panels = {panel["id"]: panel for panel in geometry["fold_panels"]}
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
                                (tx, y, tx+tw, y), 10+index, 1))
            result.append(hinge(f"hinge-wall-{index}-bottom", wall["id"], bottom_flap["id"],
                                (bx, y+h, bx+bw, y+h), 20+index, -1))
        return result
    center = panels["center"]
    x, y, w, h = (Decimal(center[key]) for key in ("x", "y", "width", "height"))
    return [
        hinge("hinge-center-top-fold", "center", "top_fold", (x, y, x+w, y), 1, -1),
        hinge("hinge-top-fold-cover", "top_fold", "top_cover", (x, Decimal(panels["top_fold"]["y"]), x+w, Decimal(panels["top_fold"]["y"])), 2, -1),
        hinge("hinge-center-bottom-fold", "center", "bottom_fold", (x, y+h, x+w, y+h), 3, 1),
        hinge("hinge-bottom-fold-cover", "bottom_fold", "bottom_cover", (x, Decimal(panels["bottom_cover"]["y"]), x+w, Decimal(panels["bottom_cover"]["y"])), 4, 1),
        hinge("hinge-center-left-fold", "center", "left_fold", (x, y, x, y+h), 5, 1),
        hinge("hinge-left-fold-wing", "left_fold", "left_wing", (Decimal(panels["left_fold"]["x"]), y, Decimal(panels["left_fold"]["x"]), y+h), 6, 1),
        hinge("hinge-center-right-fold", "center", "right_fold", (x+w, y, x+w, y+h), 7, -1),
        hinge("hinge-right-fold-wing", "right_fold", "right_wing", (Decimal(panels["right_wing"]["x"]), y, Decimal(panels["right_wing"]["x"]), y+h), 8, -1),
    ]


def with_frozen_fold_panels(template_key, geometry):
    """Enrich a legacy release using its frozen dimensions, never today's product."""
    if "fold_panels" in geometry or template_key not in PARAMETER_KEYS:
        return geometry
    values = {key:geometry.get("dimensions", {}).get(_parameter_label(key)) for key in PARAMETER_KEYS[template_key]}
    try:
        regenerated = build_geometry(template_key, values)
    except DrawingGeometryError:
        return {**geometry,"fold_panels":geometry.get("panels",[])}
    return {**geometry,"fold_panels":regenerated["fold_panels"]}


def same_manufacturing_geometry(left, right):
    # Visual folding metadata must not invalidate already frozen releases.
    if not isinstance(left, dict) or not isinstance(right, dict):
        return left == right
    return {k:v for k,v in left.items() if k != "fold_panels"} == {k:v for k,v in right.items() if k != "fold_panels"}


def validate_parameters(template_key, parameters):
    allowed = set(PARAMETER_KEYS[template_key])
    if template_key == "partition_v1":
        allowed.update(f"slot_{i}_position_mm" for i in range(1,33))
    if set(parameters) - allowed:
        raise DrawingGeometryError("存在不属于当前结构的尺寸参数")
    for key, raw in parameters.items():
        if raw is None:
            continue
        if key in {"slot_count","slot_edge"}:
            value = Decimal(str(raw))
            if not value.is_finite() or value != value.to_integral_value() or not (1 <= value <= 32 if key == "slot_count" else value in (0,1)):
                raise DrawingGeometryError("槽数须为1至32的整数，开槽边须为0或1")
        else:
            from app.services.drawing_geometry import number
            number(raw, _parameter_label(key))
    if template_key == "partition_v1" and parameters.get("slot_count") is not None:
        if any(int(m[1]) > int(parameters["slot_count"]) for key in parameters if (m:=re.fullmatch(r"slot_(\d+)_position_mm",key))):
            raise DrawingGeometryError("独立槽位置超过当前槽数，请清除多余槽位置")


def preview_payload(template_key: str, parameters: dict[str, Any], editor_state: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_parameters(template_key, parameters)
    geometry = build_geometry(template_key, parameters)
    name_to_key = {_parameter_label(key):key for key in PARAMETER_KEYS[template_key]}
    if template_key == "partition_v1":
        name_to_key["隔板长"] = "length_mm"
    for dim in geometry.get("dimension_index", []):
        dim["parameter_key"] = dim.get("parameter_key") or name_to_key.get(dim["name"],"")
    dimension_ids = {entry["parameter_key"]: entry["id"] for entry in geometry.get("dimension_index", [])}
    # Stable hit targets use the exact dimensions of their physical segments.
    for kind in ("cut","score"):
        for i, segment in enumerate(geometry[kind]):
            segment.setdefault("id",f"{kind}-{i+1}")
            length = abs(Decimal(segment["x2"])-Decimal(segment["x1"])) + abs(Decimal(segment["y2"])-Decimal(segment["y1"]))
            matches = [key for key in PARAMETER_KEYS[template_key] if key.endswith("_mm") and parameters.get(key) is not None and Decimal(str(parameters[key])) == length]
            if not matches:
                axis = "x" if segment["y1"] == segment["y2"] else "y"
                start,end=sorted((Decimal(segment[axis+"1"]),Decimal(segment[axis+"2"])))
                matches = [d["parameter_key"] for d in geometry.get("dimension_index",[]) if d["axis"]==axis
                           and min(end,Decimal(d["end_mm"])) > max(start,Decimal(d["start_mm"])) and d.get("parameter_key")]
            segment["parameter_keys"] = matches
            if not segment.get("parameter_key") and matches:
                segment["parameter_key"] = matches[0]
    # Printing panels retain their existing identity; folding panels add hit bindings.
    for panel in geometry["panels"]:
        twin = next((p for p in geometry["fold_panels"] if p["id"] == panel["id"]),None)
        if twin:
            panel["parameter_keys"] = twin.get("parameter_keys",[])
    editable = []
    keys = list(PARAMETER_KEYS[template_key])
    if template_key == "partition_v1":
        keys += [f"slot_{i+1}_position_mm" for i in range(int(parameters["slot_count"]))]
    for key in keys:
        value = parameters.get(key)
        if value is None and (m:=re.fullmatch(r"slot_(\d+)_position_mm", key)):
            value = Decimal(str(parameters["slot_offset_mm"]))+(int(m[1])-1)*Decimal(str(parameters["slot_pitch_mm"]))
        field = parameter_field(template_key,key)
        editable.append({**field,
                         "value_mm": None if value is None else str(value), "source": "parameter",
                         "editable": True, "derived": False, "derived_from": field["depends_on"],
                         "bounds": {"min_mm": "0.01", "max_mm": "10000"},
                         "geometry_refs": {"dimension_ids": [dimension_ids[key]] if key in dimension_ids else [],
                                           "panel_ids": [p["id"] for p in geometry["fold_panels"] if key in p.get("parameter_keys",[])],
                                           "segment_ids": [s["id"] for k in ("cut","score") for s in geometry[k] if key == s.get("parameter_key")]}})
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
