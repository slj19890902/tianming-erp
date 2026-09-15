"""Read-only unassembled stock index. Listing never reserves or assembles stock."""
import json
from collections import defaultdict
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomInventoryRelation
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.services.multilevel_bom_master import load_master_structure
from app.services.multilevel_bom_plan import BomPlanError


def pending_stock(db, scope):
    lots_by_product = defaultdict(list)
    lots = db.scalars(select(InventoryLot).where(InventoryLot.status == 'active', InventoryLot.quantity_available > 0))
    for lot in lots:
        detail = lot.finished_detail or lot.semi_finished_detail
        if not detail or (scope is not None and detail.owner_customer_id not in scope):
            continue
        if lot.finished_detail:
            product_ids = [lot.finished_detail.product_id]
        else:
            profile = db.get(WarehouseGoodsProfile, lot.id)
            try:
                data = json.loads(profile.data_json) if profile else {}
            except (ValueError, TypeError):
                continue
            # Only explicitly identified processed parts; generic raw board is not an assembled child.
            product_ids = data.get('product_ids', []) if data.get('output_piece') and data.get('processing') in {'cut', 'die_cut'} else []
        for pid in product_ids:
            lots_by_product[pid].append(lot)
    if not lots_by_product:
        return []
    parents = db.scalars(select(ProductBomComponent.parent_product_id).join(
        ProductBomInventoryRelation, ProductBomInventoryRelation.bom_component_id == ProductBomComponent.id
    ).where(ProductBomComponent.component_product_id.in_(lots_by_product), ProductBomInventoryRelation.relation == 'assembly').distinct())
    result = []
    for parent_id in parents:
        parent = db.get(Product, parent_id)
        if not parent or (scope is not None and parent.customer_id not in scope):
            continue
        try:
            structure = load_master_structure(db, parent_id)
        except BomPlanError as error:
            result.append(dict(key=f'stock:{parent_id}', source_kind='stock', customer_name=parent.customer.chinese_short_name or parent.customer.name,
                order_number='备库 · '+parent.product_code, error=str(error), outputs=[], sources=[]))
            continue
        edges = [e for e in structure['edges'] if e['parent_id'] == parent_id and e['relation'] == 'assembly']
        sources, children, capacities = [], [], []
        for edge in edges:
            child = structure['products'][edge['child_id']]
            candidates = [l for l in lots_by_product[child.id] if (l.finished_detail or l.semi_finished_detail).owner_customer_id == parent.customer_id]
            total = sum(l.quantity_available for l in candidates)
            capacities.append(total // edge['quantity'])
            children.append(dict(product_id=child.id, product_code=child.product_code, product_name=child.product_name,
                per_set=edge['quantity'], quantity=total, unit=child.unit))
            for lot in candidates:
                location = db.get(WarehouseLocation, lot.warehouse_location_id)
                sources.append(dict(lot_id=lot.id, product_id=child.id, product_name=child.product_name, product_code=child.product_code,
                    quantity=lot.quantity_available, unit=child.unit, location=location.location_name if location else '位置待核对'))
        if not sources:
            continue
        capacity = min(capacities, default=0)
        # Show the shortage for the next possible set, never invent a purchase quantity.
        for child in children:
            child['missing_next_set'] = max(0, (capacity+1)*child['per_set']-child['quantity'])
        result.append(dict(key=f'stock:{parent_id}', source_kind='stock', customer_id=parent.customer_id,
            customer_name=parent.customer.chinese_short_name or parent.customer.name,
            order_number='备库 · '+parent.product_code, product_name=parent.product_name,
            available_sets=capacity, children=children, sources=sources, outputs=[],
            notice='未预占子件，配套数仅供参考；组装仍需核实本体、实物身份及实际数量。'))
    return result
