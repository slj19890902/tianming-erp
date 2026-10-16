"""Read-only choices for existing component-delivery contracts.

No profile is manufactured and no inventory is converted. Product IDs remain
the selected physical object; a virtual parent only supplies search context.
"""
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile
from app.services.product_specification import product_dimension_specification


def route_candidates(db, candidates):
    routed = {}
    for row in candidates:
        product = db.get(Product, row['product_id'])
        profile = db.get(ProductBomProfile, product.id)
        component_delivery = (
            profile.source == 'separate' if profile else
            product.is_composite and product.composite_fulfillment_mode == 'component_delivery'
        )
        if not component_delivery:
            routed.setdefault(product.id, row)
            continue
        # Legacy physical parents retain their pre-existing own-product entry.
        # They do not acquire complete-kit outbound eligibility through routing.
        own_entry = not product.is_virtual_composite_parent and profile is None
        if own_entry:
            routed.setdefault(product.id, {**row, 'entry_hint': '本品；配套子件分别登记'})
        edges = list(db.scalars(select(ProductBomComponent).where(
            ProductBomComponent.parent_product_id == product.id).order_by(
                ProductBomComponent.display_order, ProductBomComponent.id)))
        problems = []
        if not edges or len(edges) > 100:
            problems.append('BOM子件关系缺失或超过100项，请维护配方')
        for edge in edges[:100]:
            child = db.get(Product, edge.component_product_id)
            if child is None or child.customer_id != product.customer_id:
                problems.append('BOM含不存在或跨客户的关系，请维护配方')
                continue
            if not child.is_active or child.deleted_at or child.purged_at:
                problems.append(f'子件 {child.product_code} 已停用或删除，请维护配方')
                continue
            routed.setdefault(child.id, {
                **row, 'product_id': child.id, 'product_code': child.product_code,
                'customer_material_code': child.customer_material_code,
                'product_name': child.product_name, 'specification': product_dimension_specification(child),
                'match_score': None, 'is_exact': False, 'match_type': 'bom_component',
                'matched_order_numbers': [],
                'entry_hint': f'{product.product_code} 的子件 · 每套 {edge.quantity_per_set}',
            })
        if problems or (not own_entry and not edges):
            routed.setdefault(product.id, {**row, 'entry_block_reason': '；'.join(dict.fromkeys(problems)),
                'entry_hint': '组合关系待完善'})
    return list(routed.values())
