"""Atomically produce procurement snapshots from real versioned BOM nodes.

Supplier prices are still resolved/frozen by the existing purchase confirmation.
This is not yet the public order-entry adapter: receipt acceptance remains gated.
"""
import json
from fractions import Fraction
from types import SimpleNamespace

from sqlalchemy import select

from app.models.multilevel_bom import OrderBomExternalComponent
from app.models.order import OrderItem
from app.models.product import Product
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from app.services.multilevel_bom_external_identity import bind_external_component, read_external_node, legacy_multiplier
from app.services.order_external_packaging import _freeze_direct_product_component, OrderExternalPackagingSnapshotError


def freeze_order_procurement(db, *, order_item_id, actor):
    with atomic_bom(db):
        compiled = freeze_master_order_bom(db, order_item_id=order_item_id, actor=actor)
        nodes = [n for n in compiled.graph.nodes if n.source == 'purchased']
        existing = list(db.scalars(select(OrderBomExternalComponent).where(OrderBomExternalComponent.order_item_id == order_item_id)))
        if existing:
            if {e.product_id for e in existing} != {n.product_id for n in nodes}:
                raise BomPlanError('订单外购节点快照不完整，禁止用当前主档补写')
            for link in existing:
                read_external_node(db, link.external_component_id)
            return compiled
        if not nodes:
            return compiled
        if db.scalar(select(SalesOrderItemExternalComponent.id).where(SalesOrderItemExternalComponent.sales_order_item_id == order_item_id).limit(1)):
            raise BomPlanError('已有未关联的外购快照，必须先审计转换')
        item = db.get(OrderItem, order_item_id)
        gross = {p.product_id:p.required_units for p in plan_bom(compiled.graph, 1).products}
        for position, node in enumerate(sorted(nodes, key=lambda n:n.product_id), 1):
            product = db.get(Product, node.product_id)
            units = node.purchase_units
            if (units is None or product is None or product.version != node.version
                    or product.customer_id != compiled.graph.customer_id or not product.is_active
                    or product.deleted_at is not None or product.purged_at is not None
                    or product.supply_mode != 'external_purchase'):
                raise BomPlanError('外购真实产品已变化，不能补写订单采购快照')
            # Reuse the existing strict candidate/default/unit snapshot writer.
            try:
                candidates = json.loads(product.external_packaging_candidate_snapshot_json or '[]')
            except (ValueError, TypeError) as error:
                raise BomPlanError('外购候选格式无效') from error
            if not isinstance(candidates, list) or not candidates or len(candidates) > 99:
                raise BomPlanError('外购节点必须有有效供应商候选')
            for candidate in candidates:
                if (not isinstance(candidate, dict)
                        or any(type(candidate.get(key)) is not int or candidate[key] <= 0
                               for key in ('external_product_id','supplier_id','external_product_version'))
                        or type(candidate.get('is_default')) is not bool
                        or (candidate.get('customer_scope_id') is not None and
                            (type(candidate['customer_scope_id']) is not int or candidate['customer_scope_id'] != compiled.graph.customer_id))):
                    raise BomPlanError('外购候选身份无效或不属于当前客户')
            ratio = Fraction(gross[node.product_id]) * Fraction(units.purchase_basis) / Fraction(units.stock_basis)
            source = SimpleNamespace(id=item.id, quantity=item.quantity, snapshot_product_name=node.name,
                external_packaging_category_code_snapshot=product.external_packaging_category_code,
                external_packaging_specification_json_snapshot=product.external_packaging_specification_json,
                external_packaging_specification_summary_snapshot=product.external_packaging_specification_summary,
                external_packaging_purchase_unit_snapshot=units.purchase_unit,
                external_packaging_order_quantity_basis_snapshot=units.stock_basis,
                external_packaging_purchase_quantity_basis_snapshot=units.purchase_basis,
                external_packaging_quantity_per_finished_unit_snapshot=legacy_multiplier(ratio),
                external_packaging_product_version_snapshot=node.version,
                external_packaging_candidate_snapshot_json=product.external_packaging_candidate_snapshot_json)
            try:
                _freeze_direct_product_component(db, order_item=source, display_order=position)
            except (OrderExternalPackagingSnapshotError, AttributeError, TypeError) as error:
                raise BomPlanError(str(error)) from error
            component = db.scalar(select(SalesOrderItemExternalComponent).where(
                SalesOrderItemExternalComponent.sales_order_item_id == item.id,
                SalesOrderItemExternalComponent.display_order == position))
            component.conversion_basis = (f'库存 {units.stock_basis} {node.unit} → 采购 {units.purchase_basis} {units.purchase_unit}；'
                                          f'每父件 {gross[node.product_id]} {node.unit}')
            component.remarks = '真实多级BOM外购节点；采购数量按冻结图精确换算'
            if any(c.customer_scope_id_snapshot not in (None, compiled.graph.customer_id)
                   or c.external_product_id_snapshot <= 0 or c.supplier_id_snapshot <= 0
                   or c.external_product_version_snapshot <= 0 for c in component.candidates):
                raise BomPlanError('外购候选身份无效或不属于当前客户')
            bind_external_component(db, external_component_id=component.id,
                order_item_id=item.id, product_id=node.product_id, actor=actor)
        return compiled
