"""Read-only physical sheet discovery; selection never allocates inventory."""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import exists, or_, select

from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, SemiFinishedLotAllowedProduct
from app.services.warehouse_inventory import SEMI_FINISHED_FLUTES_BY_LAYER


def flute_layers(flute):
    return next((layer for layer, values in SEMI_FINISHED_FLUTES_BY_LAYER.items()
                 if flute in values), None)


SHORTFALL_MM = Decimal('10')
NEAR_SIZE_FACTOR = Decimal('1.10')


def _axis_fit(actual, required, shortfall_mm):
    """Best original-axis grid; tolerance is per whole sheet, never per cut."""
    whole = int(actual // required)
    candidates = []
    for count in (whole, whole + 1):
        if count < 1:
            continue
        planned = required * count
        if planned - actual > shortfall_mm:
            continue
        candidates.append((min(actual, planned) / max(actual, planned), -count))
    score, negative_count = max(candidates)
    return score, -negative_count


def sheet_fit(actual_length, actual_width, length, width, *, shortfall_mm=SHORTFALL_MM):
    """Read-only size ranking, not an execution yield or inventory conversion."""
    actual_length, actual_width, length, width = map(
        lambda value: Decimal(str(value)), (actual_length, actual_width, length, width))
    if min(actual_length, actual_width, length, width) <= 0:
        return None
    if actual_length < length - shortfall_mm or actual_width < width - shortfall_mm:
        return None
    length_score, length_cuts = _axis_fit(actual_length, length, shortfall_mm)
    width_score, width_cuts = _axis_fit(actual_width, width, shortfall_mm)
    raw_score = length_score * width_score * 100
    score = raw_score.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if raw_score < 100:
        score = min(score, Decimal('99.99'))
    return {'match_score': score, 'cuts_per_sheet': length_cuts * width_cuts,
            'length_cuts': length_cuts, 'width_cuts': width_cuts}


def find_sheets(db, scope, *, length, width, flute_type, page, page_size, min_score=0, expand_scope=False):
    from app.api.mobile_erp import _inventory_group, _lot_load_options
    from app.services.location_candidates import load_warehouse_location_projection_contexts
    from app.services.warehouse_goods import goods_profile

    length, width = Decimal(str(length)), Decimal(str(width))
    query = select(InventoryLot).join(SemiFinishedInventoryDetail).where(
        InventoryLot.inventory_type == "semi_finished", InventoryLot.status == "active",
        or_(InventoryLot.quantity_available > 0, InventoryLot.quantity_reserved > 0),
        SemiFinishedInventoryDetail.board_length_mm > 0,
        SemiFinishedInventoryDetail.board_width_mm > 0,
        SemiFinishedInventoryDetail.board_length_mm >= length - SHORTFALL_MM,
        SemiFinishedInventoryDetail.board_width_mm >= width - SHORTFALL_MM)
    if not expand_scope:
        query = query.where(
            SemiFinishedInventoryDetail.board_length_mm <= length * NEAR_SIZE_FACTOR,
            SemiFinishedInventoryDetail.board_width_mm <= width * NEAR_SIZE_FACTOR)
    if flute_type:
        query = query.where(SemiFinishedInventoryDetail.flute_type == flute_type)
    if scope is not None:
        allowed = exists(select(SemiFinishedLotAllowedProduct.product_id).join(
            Product, Product.id == SemiFinishedLotAllowedProduct.product_id).where(
            SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
            Product.customer_id.in_(scope), Product.deleted_at.is_(None),
            Product.purged_at.is_(None), Product.is_active.is_(True)))
        query = query.where(or_(SemiFinishedInventoryDetail.owner_customer_id.in_(scope), allowed))
    # Rank the scoped dimensional projection before paging. Load relationships
    # only for the visible page, retaining separate physical batches and units.
    ranked = []
    large_lots = set()
    for lot_id, actual_length, actual_width in db.execute(query.with_only_columns(
            InventoryLot.id, SemiFinishedInventoryDetail.board_length_mm,
            SemiFinishedInventoryDetail.board_width_mm)):
        fit = sheet_fit(actual_length, actual_width, length, width)
        if fit and fit['match_score'] >= min_score:
            ranked.append((lot_id, fit))
            if actual_length > length * NEAR_SIZE_FACTOR or actual_width > width * NEAR_SIZE_FACTOR:
                large_lots.add(lot_id)
    ranked.sort(key=lambda row: (row[0] in large_lots, -row[1]['match_score'], row[1]['cuts_per_sheet'], row[0]))
    total = len(ranked)
    selected = ranked[(page - 1) * page_size:page * page_size]
    fits = dict(selected)
    loaded = {lot.id: lot for lot in db.scalars(query.where(InventoryLot.id.in_(fits))
              .options(*_lot_load_options()))} if selected else {}
    lots = [loaded[lot_id] for lot_id, _ in selected if lot_id in loaded]
    contexts = load_warehouse_location_projection_contexts(db, [lot.location for lot in lots if lot.location])
    positions = {row["lot_id"]: row for row in _inventory_group(
        lots, unit="张", projection_contexts=contexts)["positions"]}
    items = []
    for lot in lots:
        detail, position = lot.semi_finished_detail, positions[lot.id]
        profile = goods_profile(db, lot) or {}
        own = scope is None or detail.owner_customer_id in scope
        items.append({
            "lot_id": lot.id,
            **fits[lot.id],
            "match_score": float(fits[lot.id]['match_score']),
            "name": (detail.internal_name or "库存片料") if own else "已授权共用片料",
            "length": detail.board_length_mm, "width": detail.board_width_mm,
            "flute_type": detail.flute_type, "layer_count": detail.layer_count,
            "material_code": detail.material_code_snapshot,
            "actual": position["quantity_total"], "available": position["quantity_available"],
            "reserved": position["quantity_reserved"],
            "unit": "片" if profile.get("output_piece") is True else "张",
            "location_label": position.get("employee_location_name") or "位置待核对",
            "reverse_source": {"lot_id": lot.id, "length": detail.board_length_mm,
                               "width": detail.board_width_mm, "flute_type": detail.flute_type},
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total, "source": "inventory_sheets"}
