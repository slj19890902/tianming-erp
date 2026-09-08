"""Read-only employee numbering; stable location IDs and stock never change."""
from collections import defaultdict
from types import SimpleNamespace

from sqlalchemy import select

from app.models.warehouse_inventory import Floor3LocationLayout, WarehouseLocation


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


def load_spatial_sequences(db, floor_numbers, area_codes):
    if not floor_numbers or not area_codes:
        return {}
    rows = db.execute(select(WarehouseLocation, Floor3LocationLayout)
        .outerjoin(Floor3LocationLayout, Floor3LocationLayout.location_id == WarehouseLocation.id)
        .where(WarehouseLocation.warehouse_floor.in_(floor_numbers),
               WarehouseLocation.area_code.in_(area_codes), WarehouseLocation.is_active.is_(True))).all()
    from app.services.warehouse_twin_layout import load_warehouse_twin_floor, WarehouseTwinLayoutNotFoundError
    features = {}
    for floor_number in floor_numbers:
        try:
            for feature in load_warehouse_twin_floor(f"{floor_number}F").get("features", []):
                features[(floor_number, feature.get("erp_area_code"))] = feature
        except (OSError, ValueError, WarehouseTwinLayoutNotFoundError):
            pass
    return spatial_sequences([(loc, SimpleNamespace(**applied_ground_geometry(loc.id, layout,
        features.get((loc.warehouse_floor, loc.area_code)))) if layout is not None else None) for loc, layout in rows])
