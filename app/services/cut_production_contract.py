"""Physical input and inherited cost for frozen sheet-cut reservations."""
import json
from decimal import Decimal, ROUND_HALF_UP
from math import ceil
from sqlalchemy import select
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement
from app.services.warehouse_inventory import WarehouseInventoryError, inventory_fifo_order_columns


def completion_contract(db, task, item, planned_output, *, require_cost=True):
    requirements = list(db.scalars(select(OrderItemSemiRequirement).where(
        OrderItemSemiRequirement.order_item_id == item.id,
        OrderItemSemiRequirement.sales_order_item_bom_component_id == task.sales_order_item_bom_component_id,
    ).order_by(OrderItemSemiRequirement.id)))
    allocations = []
    has_cut = False
    missing = 0
    for requirement in requirements:
        remaining = planned_output * max(requirement.pieces_per_box, 1)
        rows = db.scalars(select(InventoryReservation).join(InventoryLot).where(
            InventoryReservation.semi_requirement_id == requirement.id,
            InventoryReservation.reservation_type == 'semi_order',
            InventoryReservation.status != 'cancelled',
        ).order_by(*inventory_fifo_order_columns(), InventoryReservation.id)).all()
        for reservation in rows:
            credit = reservation.credited_requirement_quantity - reservation.consumed_requirement_quantity - reservation.released_requirement_quantity
            stock = reservation.reserved_stock_quantity - reservation.consumed_stock_quantity - reservation.released_stock_quantity
            if remaining <= 0 or credit <= 0 or stock <= 0:
                continue
            take_credit = min(remaining, credit)
            take = min(stock, ceil(take_credit / reservation.yield_factor))
            if take * reservation.yield_factor < take_credit:
                raise WarehouseInventoryError('预占片料数量与裁切换算不一致，请刷新核对', 409)
            has_cut = has_cut or bool(reservation.cut_plan_json)
            allocations.append(dict(reservation_id=reservation.id, lot_id=reservation.inventory_lot_id,
                quantity=take, credit=take_credit, cut_plan=json.loads(reservation.cut_plan_json) if reservation.cut_plan_json else None))
            remaining -= take_credit
        missing += remaining
    if not has_cut:
        return None
    # A mixed supplier/stock batch must keep its independent purchasing cost facts;
    # it cannot be silently valued entirely as reserved stock.
    if missing:
        if not require_cost:
            return None
        raise WarehouseInventoryError('本批裁切预占尚未齐套，请将缺少的来料入库并补齐本批预占后确认完工', 409)
    if not require_cost:
        return dict(input_quantity=sum(row['quantity'] for row in allocations), planned_output=planned_output)
    from app.services.inventory_valuation import require_inherited_entry_cost
    total = Decimal(0)
    for row in allocations:
        origin = db.get(InventoryLot, row['lot_id'])
        unit = require_inherited_entry_cost(db, origin)
        row['unit_cost'] = str(unit)
        total += unit * row['quantity']
    return dict(inputs=allocations, input_quantity=sum(row['quantity'] for row in allocations),
        planned_output=planned_output, total_cost=str(total), currency='CNY',
        basis='inherited_entry_cost_not_new_purchase')


def freeze_completion_cost(lot, completion, contract, captured_at):
    total = Decimal(contract['total_cost'])
    quantity = int(completion.actual_output_quantity)
    lot.estimated_unit_cost_snapshot = (total / quantity).quantize(Decimal('.0001'), rounding=ROUND_HALF_UP)
    lot.estimated_square_price_snapshot = None
    lot.estimated_cost_area_m2_snapshot = None
    lot.cost_snapshot_source = 'sheet_cut_production'
    lot.cost_snapshot_at = captured_at
    lot.cost_snapshot_detail_json = json.dumps(dict(contract, completion_id=completion.id,
        output_quantity=quantity, cost_label='裁切继承材料成本'), ensure_ascii=False, sort_keys=True)
