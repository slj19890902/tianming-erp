"""Read-only, bounded cost details; whole-stock totals retain frozen-cost semantics."""
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.warehouse_inventory import (
    InventoryLot as Lot, FinishedGoodsInventoryDetail as Finished,
    SemiFinishedInventoryDetail as Semi, WarehouseLocation as Location,
)
from app.services.inventory_valuation import cost_payload
from app.services.inventory_revaluation_readiness import batch_revaluation_readiness

BASIS = "按批次冻结的人民币成本，含可用、预占及损坏实物；材料成本、外购价与售价参考分别标明"
SUMMARY_BATCH_SIZE = 200


def read_inventory_cost_page(db, scoped_ids, *, page=1, page_size=50, keyword="", summary_only=False):
    if page < 1 or not 1 <= page_size <= 200 or len(keyword) > 150:
        raise ValueError("Invalid inventory cost page")
    # Only scalar snapshot fields are streamed for the whole-stock amount. Do not
    # hydrate pallet, allowed-product, alias or revaluation graphs for every lot.
    snapshot_fields = ("id", "estimated_unit_cost_snapshot", "cost_snapshot_source",
        "cost_snapshot_detail_json", "cost_snapshot_at", "quantity_available",
        "quantity_reserved", "quantity_damaged", "unit")
    query = select(*(getattr(Lot, name) for name in snapshot_fields),
        Finished.inventory_lot_id.label("finished_id"), Finished.length_mm, Finished.width_mm, Finished.height_mm,
        Finished.owner_customer_name_snapshot.label("finished_customer"), Finished.inventory_code_snapshot,
        Finished.product_name_snapshot, Semi.owner_customer_name_snapshot.label("semi_customer"),
        Semi.internal_name, Location.location_name,
    ).select_from(Lot).outerjoin(Finished, Finished.inventory_lot_id == Lot.id).outerjoin(
        Semi, Semi.inventory_lot_id == Lot.id).outerjoin(Location, Location.id == Lot.warehouse_location_id
    ).where(Lot.id.in_(scoped_ids)).order_by(Lot.id).execution_options(yield_per=SUMMARY_BATCH_SIZE)
    total_value, total_lots, missing_lots, matched_count = Decimal(0), 0, 0, 0
    page_ids = []
    keyword = keyword.strip().lower()
    offset = (page - 1) * page_size
    with db.execute(query) as result:
        for row in result.mappings():
            physical = (SimpleNamespace(length_mm=row.length_mm, width_mm=row.width_mm, height_mm=row.height_mm)
                if row.finished_id is not None else None)
            snapshot = SimpleNamespace(**{name:row[name] for name in snapshot_fields}, finished_detail=physical)
            value = cost_payload(snapshot, db)
            total_lots += 1
            if value["inventory_value"] is None:
                missing_lots += 1
            else:
                # Sum individually rounded lots, never round a sum of raw prices.
                total_value += Decimal(value["inventory_value"])
            text = " ".join(str(v or "") for v in (
                row.finished_customer if physical else row.semi_customer,
                row.inventory_code_snapshot if physical else None,
                row.product_name_snapshot if physical else row.internal_name,
                row.location_name if row.location_name is not None else "未归位"))
            if keyword and keyword not in text.lower():
                continue
            if not summary_only and offset <= matched_count < offset + page_size:
                page_ids.append(row.id)
            matched_count += 1
    rows = []
    if page_ids:
        details = select(Lot).where(Lot.id.in_(page_ids)).order_by(Lot.id).limit(page_size).options(
            selectinload(Lot.location), selectinload(Lot.finished_detail), selectinload(Lot.semi_finished_detail))
        page_lots = list(db.scalars(details))
        revaluation_readiness = batch_revaluation_readiness(db, page_lots)
        for lot in page_lots:
            value = cost_payload(lot, db)
            detail = lot.finished_detail or lot.semi_finished_detail
            value.update(lot_number=lot.lot_number, inventory_type=lot.inventory_type,
                stock_date=lot.stock_date, location_id=lot.warehouse_location_id,
                location_name=lot.location.location_name if lot.location else "未归位",
                product_code=getattr(detail, "inventory_code_snapshot", None),
                product_id=getattr(detail, "product_id", None),
                can_revalue=bool(lot.finished_detail and revaluation_readiness.get(int(lot.id), False)),
                product_name=getattr(detail, "product_name_snapshot", None) or getattr(detail, "internal_name", None),
                customer_name=getattr(detail, "owner_customer_name_snapshot", None))
            rows.append(value)
    return dict(currency="CNY", inventory_value=str(total_value), total_lots=total_lots,
        missing_lots=missing_lots, rows=rows, basis=BASIS, total=matched_count,
        page=page, page_size=page_size, has_more=page * page_size < matched_count,
        summary_scope="all_visible_stock_in_selected_location", summary_only=summary_only)
