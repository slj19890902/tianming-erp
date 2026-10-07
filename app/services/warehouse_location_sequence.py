"""Read-only employee numbering; stable location IDs and stock never change."""
from collections import defaultdict
from types import SimpleNamespace

from sqlalchemy import select

from app.models.warehouse_inventory import Floor3LocationLayout, WarehouseLocation, WarehouseArea, WarehouseFloor


def applied_ground_geometry(location_id, layout, feature):
    if layout is None:
        return None
    geometry = {key: float(getattr(layout, key)) for key in ("left_pct", "top_pct", "width_pct", "height_pct")}
    saved = next((slot for slot in ((feature or {}).get("ground_location_draft") or {}).get("slots", [])
                  if slot["location_id"] == location_id), None)
    points = (feature or {}).get("points") or []
    if saved and points:
        left, right = min(p[0] for p in points), max(p[0] for p in points)
        bottom, top = min(p[1] for p in points), max(p[1] for p in points)
        if right > left and top > bottom:
            geometry.update(left_pct=(saved["x_mm"] - left) / (right - left) * 100,
                top_pct=(top - saved["y_mm"] - saved["depth_mm"]) / (top - bottom) * 100,
                width_pct=saved["width_mm"] / (right - left) * 100,
                height_pct=saved["depth_mm"] / (top - bottom) * 100)
    return geometry


def spatial_sequences(rows):
    groups = defaultdict(list)
    for location, layout in rows:
        if not location.is_active or location.address_kind == "rack_slot":
            continue
        groups[(location.warehouse_floor, str(location.area_code or "").strip().upper())].append((location, layout))
    result = {}
    for items in groups.values():
        positioned = [(loc, geo) for loc, geo in items if geo is not None]
        positioned.sort(key=lambda item: (float(item[1].top_pct), float(item[1].left_pct), item[0].id))
        bands = []
        for loc, geo in positioned:
            center = float(geo.top_pct) + float(geo.height_pct) / 2
            band = next((b for b in bands if abs(center - b[0]) <= min(b[1], float(geo.height_pct)) * .4), None)
            if band is None:
                band = [center, float(geo.height_pct), []]
                bands.append(band)
            band[2].append((loc, geo))
        ordered = [loc for band in sorted(bands, key=lambda b: b[0])
                   for loc, geo in sorted(band[2], key=lambda item: (float(item[1].left_pct), item[0].id))]
        ordered.extend(sorted((loc for loc, geo in items if geo is None), key=lambda loc: (loc.sort_order or 0, loc.id)))
        result.update({loc.id: index for index, loc in enumerate(ordered, 1)})
    return result


def load_spatial_sequences(db, floor_numbers, area_codes, *, rows=None):
    if not floor_numbers or not area_codes:
        return {}
    from app.services.warehouse_location_address import employee_area_name, normalize_location_alias
    # Several physical areas may share one employee-facing name. Number their
    # locations together, including siblings omitted by a stock/filter query.
    area_keys = {}
    for area, floor in db.execute(select(WarehouseArea, WarehouseFloor)
            .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
            .where(WarehouseFloor.floor_number.in_(floor_numbers))).all():
        area_keys[(floor.floor_number, area.area_code)] = (floor.floor_number,
            normalize_location_alias(employee_area_name(area, floor_number=floor.floor_number)))
    requested = {key for (floor, code), key in area_keys.items() if code in area_codes}
    expanded_codes = set(area_codes) | {code for (floor, code), key in area_keys.items() if key in requested}
    if rows is None:
        rows = db.execute(select(WarehouseLocation, Floor3LocationLayout)
            .outerjoin(Floor3LocationLayout, Floor3LocationLayout.location_id == WarehouseLocation.id)
            .where(WarehouseLocation.warehouse_floor.in_(floor_numbers),
                   WarehouseLocation.area_code.in_(expanded_codes), WarehouseLocation.is_active.is_(True))).all()
    else:
        rows = list(rows)
        missing_codes = expanded_codes - set(area_codes)
        if missing_codes:
            rows += db.execute(select(WarehouseLocation, Floor3LocationLayout)
                .outerjoin(Floor3LocationLayout, Floor3LocationLayout.location_id == WarehouseLocation.id)
                .where(WarehouseLocation.warehouse_floor.in_(floor_numbers),
                       WarehouseLocation.area_code.in_(missing_codes), WarehouseLocation.is_active.is_(True))).all()
    from app.services.warehouse_twin_layout import load_warehouse_twin_floor, WarehouseTwinLayoutNotFoundError
    features = {}
    for floor_number in floor_numbers:
        try:
            for feature in load_warehouse_twin_floor(f"{floor_number}F").get("features", []):
                features[(floor_number, feature.get("erp_area_code"))] = feature
        except (OSError, ValueError, WarehouseTwinLayoutNotFoundError):
            pass
    numbers = spatial_sequences([(loc, SimpleNamespace(**applied_ground_geometry(loc.id, layout,
        features.get((loc.warehouse_floor, loc.area_code)))) if layout is not None else None) for loc, layout in rows])
    shared = defaultdict(lambda: defaultdict(list))
    for loc, _layout in rows:
        if loc.id in numbers:
            key = (loc.warehouse_floor, loc.area_code)
            shared[area_keys.get(key, key)][key].append(loc.id)
    def area_order(key):
        points = features.get(key, {}).get("points") or []
        return (0, -max(p[1] for p in points), min(p[0] for p in points), key) if points else (1, 0, 0, key)
    for areas in shared.values():
        offset = 0
        for key in sorted(areas, key=area_order):
            ids = areas[key]
            for location_id in ids:
                numbers[location_id] += offset
            offset += len(ids)
    return numbers
