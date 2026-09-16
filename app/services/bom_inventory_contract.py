"""Explicit physical stage inside the existing immutable stock identity.

Product IDs and balances remain relational. No duplicate inventory or name-based
identity: a body is explicitly captured at inbound, never inferred from a name.
"""
import json


def body_basis(document):
    value = json.loads(document)
    value['assembly'] = []
    value['inventory_stage'] = 'body'
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def is_body_lot(lot):
    if lot is None:
        return False
    if lot.inventory_type == 'assembly_body':
        return True
    detail = lot.finished_detail
    if detail is None:
        return False
    try:
        return json.loads(detail.physical_basis_json or '{}').get('inventory_stage') == 'body'
    except (ValueError, TypeError, AttributeError):
        return False


def body_product_ids(graph):
    return {n.product_id for n in graph.nodes if n.source in ('manufactured', 'purchased')
            and any(e.parent_id == n.product_id and e.relation == 'assembly' for e in graph.edges)}


def display_name(lot, name):
    return (name or '产品') + '（未组装本体）' if is_body_lot(lot) else name


def complete_stock_condition():
    """SQL counterpart for aggregate readers; null legacy identity stays legacy."""
    from sqlalchemy import case, func
    from app.models.warehouse_inventory import FinishedGoodsInventoryDetail
    value = FinishedGoodsInventoryDetail.physical_basis_json
    return case((func.json_valid(value) == 1,
                 func.json_extract(value, '$.inventory_stage')), else_=None).is_distinct_from('body')


def product_has_assembly(db, product_id):
    from sqlalchemy import select
    from app.models.product_bom import ProductBomComponent
    from app.models.multilevel_bom import ProductBomInventoryRelation
    return db.scalar(select(ProductBomComponent.id).join(ProductBomInventoryRelation,
        ProductBomInventoryRelation.bom_component_id == ProductBomComponent.id).where(
        ProductBomComponent.parent_product_id == product_id,
        ProductBomInventoryRelation.relation == 'assembly').limit(1)) is not None


def entry_basis(product, document, stage, *, frozen_body=False):
    from sqlalchemy.orm import object_session
    from app.models.multilevel_bom import ProductBomProfile
    if stage not in ('body', 'complete'):
        raise ValueError('请选择未组装本体或完整成品')
    if stage == 'complete':
        return document
    if frozen_body:
        return body_basis(document)
    db = object_session(product)
    profile = db.get(ProductBomProfile, product.id)
    if not profile or profile.source not in ('manufactured', 'purchased') or not product_has_assembly(db, product.id):
        raise ValueError('该产品没有本体组装关系，请按独立子件或完整成品入库')
    return body_basis(document)
