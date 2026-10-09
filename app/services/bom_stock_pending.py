"""Read-only unassembled stock index. Listing never reserves or assembles stock."""
from app.services.product_unit_labels import product_unit_label
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
        if not detail:
            continue
        if lot.finished_detail:
            product_ids = [lot.finished_detail.product_id]
        else:
            profile=db.get(WarehouseGoodsProfile,lot.id)
            try:
                data=json.loads(profile.data_json) if profile else {}
            except (ValueError,TypeError):
                continue
            # Reference counts describe identified physical pieces. The strict
            # action preview below separately verifies identity and ownership.
            product_ids=data.get('product_ids',[]) if data.get('output_piece') and data.get('processing') in {'cut','die_cut','printed'} else []
        from app.models.shared_finished_stock import SharedFinishedLot,SharedFinishedMember,SharedBomMember
        from app.services.shared_finished_stock import match
        fact=db.get(SharedFinishedLot,lot.id)
        if fact:
            for member in db.scalars(select(SharedFinishedMember).where(SharedFinishedMember.group_id==fact.group_id)):
                if db.get(SharedBomMember,member.product_id) and match(db,lot,product_id=member.product_id,customer_id=member.customer_id):
                    product_ids=list(set(product_ids)|{member.product_id})
        for pid in product_ids:
            product=db.get(Product,pid)
            if product is None or (scope is not None and product.customer_id not in scope):continue
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
                parent_product_id=parent_id,can_assemble_stock=False,assembly_block=str(error),
                order_number='备库 · '+parent.product_code, error=str(error), outputs=[], sources=[]))
            continue
        edges = [e for e in structure['edges'] if e['parent_id'] == parent_id and e['relation'] == 'assembly']
        sources, children, capacities = [], [], []
        for edge in edges:
            child = structure['products'][edge['child_id']]
            candidates = [l for l in lots_by_product[child.id] if (l.finished_detail or l.semi_finished_detail).owner_customer_id == parent.customer_id
                or match(db,l,product_id=child.id,customer_id=parent.customer_id)]
            total = sum(l.quantity_available for l in candidates)
            capacities.append(total // edge['quantity'])
            children.append(dict(product_id=child.id, product_code=child.product_code, product_name=child.product_name,
                per_set=edge['quantity'], quantity=total, unit=product_unit_label(child)))
            for lot in candidates:
                location = db.get(WarehouseLocation, lot.warehouse_location_id)
                sources.append(dict(lot_id=lot.id, product_id=child.id, product_name=child.product_name, product_code=child.product_code,
                    quantity=lot.quantity_available, unit=product_unit_label(child), location=location.location_name if location else '位置待核对'))
        if not sources:
            continue
        capacity = min(capacities, default=0)
        from app.services.stock_preparation_assembly import preview
        from app.services.warehouse_inventory import WarehouseInventoryError
        try:
            verified = preview(db,parent_id,1)
            can_assemble=verified['available_sets']>0
            verified_capacity=verified['available_sets']
            assembly_block=None if can_assemble else '实物子件不足或身份待核对'
            if not can_assemble and verified.get('excluded_recipes'):
                assembly_block='当前按最新冻结配方核对；其他版本子件保留，需按原配方另行核对'
        except WarehouseInventoryError as error:
            can_assemble=False;assembly_block=str(error);verified_capacity=0;verified=None
        # Show the shortage for the next possible set, never invent a purchase quantity.
        for child in children:
            child['reference_quantity']=child['quantity']
            child['verified_quantity']=(verified or {}).get('component_availability',{}).get(child['product_id'],0)
            child['missing_next_set'] = max(0, (capacity+1)*child['per_set']-child['quantity'])
        result.append(dict(key=f'stock:{parent_id}', source_kind='stock', customer_id=parent.customer_id,
            parent_product_id=parent_id,can_assemble_stock=can_assemble,assembly_block=assembly_block,
            customer_name=parent.customer.chinese_short_name or parent.customer.name,
            order_number='备库 · '+parent.product_code, product_name=parent.product_name,
            available_sets=capacity,reference_available_sets=capacity,verified_available_sets=verified_capacity,
            unit=product_unit_label(parent),verified_output_unit=(verified or {}).get('output_unit'),
            children=children, sources=sources, outputs=[],
            notice='未预占子件，配套数仅供参考；组装仍需核实本体、实物身份及实际数量。'))
    return result
