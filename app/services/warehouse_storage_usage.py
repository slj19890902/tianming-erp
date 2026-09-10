"""Physical storage accepts mixed goods; lot types and units remain separate."""
GOODS_USAGES = ("finished", "semi_finished", "raw_material")
GOODS_LOCATION_TYPES = {"finished", "semi_finished", "shared"}

def compatible_warehouse_types(values):
    values = set(values or ())
    return values | GOODS_LOCATION_TYPES if values & GOODS_LOCATION_TYPES else values

def effective_inventory_usages(values, storage_layout=None):
    result = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if storage_layout in {"pallet_ground", "rack", "mixed"} or (storage_layout is None and bool(result)):
        return list(dict.fromkeys([*GOODS_USAGES, *result]))
    return result
