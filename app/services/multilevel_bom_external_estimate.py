"""Current external material quotes using frozen real-node purchase ratios.

No purchases, receipts or actual-cost facts are created. Extra freight/tooling
is disclosed but not silently included in the material-only estimate.
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.models.supplier import ExternalPackagingProduct
from app.services.external_packaging_purchase import (
    ExternalPurchaseContractError, _amounts, _candidate_preview, _resolved_purchase_pricing,
)
from app.services.multilevel_bom_external_identity import read_external_node, current_external_links, current_external_component_predicate
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from app.services.multilevel_bom_purchase_units import purchase_quantity_for_stock


def estimate_graph_external_materials(db, item, *, as_of):
    compiled = read_compiled_order_bom(db, item.id)
    if compiled is None:
        raise BomPlanError("外购成本缺少冻结BOM")
    nodes = {node.product_id: node for node in compiled.graph.nodes if node.source == "purchased"}
    result, missing = [], []
    if not nodes:
        return {"components": result, "missing_items": missing}
    links = {link.product_id: link for link in current_external_links(db, compiled)}
    components = {row.id: row for row in db.scalars(select(SalesOrderItemExternalComponent).where(
        SalesOrderItemExternalComponent.sales_order_item_id == item.id,
        current_external_component_predicate()).options(
        selectinload(SalesOrderItemExternalComponent.candidates)))}
    if set(links) - set(nodes) or set(components) - {link.external_component_id for link in links.values()}:
        raise BomPlanError("外购成本含未关联的BOM来源")
    demands = {row.product_id: row.required_units for row in plan_bom(compiled.graph, item.quantity).products}
    for pid, node in nodes.items():
        label = f"{node.name}外购节点成本待核定"
        link = links.get(pid)
        if link is None:
            missing.append(f"{label}：缺少冻结采购候选")
            continue
        read_external_node(db, link.external_component_id)
        component = components.get(link.external_component_id)
        if component is None:
            raise BomPlanError("外购成本组件订单身份不完整")
        defaults = [candidate for candidate in component.candidates if candidate.is_default]
        if len(defaults) != 1:
            missing.append(f"{label}：须有一个冻结默认供应商")
            continue
        candidate = defaults[0]
        if candidate.customer_scope_id_snapshot not in (None, compiled.graph.customer_id):
            raise BomPlanError("外购成本候选不属于当前客户")
        external_product = db.get(ExternalPackagingProduct, candidate.external_product_id_snapshot)
        if external_product is not None and external_product.customer_scope_id not in (None, compiled.graph.customer_id):
            raise BomPlanError("外购产品当前客户范围已变化")
        if candidate.purchase_unit_snapshot != node.purchase_units.purchase_unit:
            raise BomPlanError("外购成本采购单位与冻结节点不一致")
        quantity = purchase_quantity_for_stock(node, demands[pid])
        try:
            preview = _candidate_preview(db, candidate, component=component, quantity=quantity, as_of=as_of)
            reason = preview["blocked_reason"] or preview["suggested_quantity_warning"]
            if reason or preview["price"] is None:
                missing.append(f"{label}：{reason or '缺少有效报价'}")
                continue
            price = db.get(ExternalPackagingPriceVersion, preview["price"]["id"])
            if price is None:
                missing.append(f"{label}：报价已失效")
                continue
            # There is no authorized exchange-rate contract in this estimator.
            if price.currency != "CNY":
                missing.append(f"{label}：{price.currency}报价缺少人民币汇率合同")
                continue
            if price.tax_mode not in ("tax_inclusive", "tax_exclusive") or not 0 <= price.tax_rate <= 1:
                missing.append(f"{label}：报价税费口径无效")
                continue
            pricing = _resolved_purchase_pricing(component, price, purchase_quantity=quantity)
            line, tax, total = _amounts(price, quantity=quantity,
                unit_price=Decimal(pricing["purchase_unit_price"]),
                tax_amount_per_purchase_unit=pricing["tax_amount_per_purchase_unit"])
        except ExternalPurchaseContractError as error:
            missing.append(f"{label}：{error}")
            continue
        result.append({
            "source_type": "bom_graph_external", "label": f"外购：{node.name}",
            "product_id": pid, "bom_snapshot_id": link.bom_snapshot_id,
            "external_component_id": component.id, "external_candidate_id": candidate.id,
            "external_product_id": candidate.external_product_id_snapshot,
            "external_product_version": candidate.external_product_version_snapshot,
            "supplier_id": candidate.supplier_id_snapshot, "supplier_name": candidate.supplier_name_snapshot,
            "category_code": component.category_code, "specification_summary": component.specification_summary,
            "required_stock_quantity": demands[pid], "stock_unit": node.unit,
            "purchase_quantity": format(quantity, "f"), "purchase_unit": node.purchase_units.purchase_unit,
            "stock_basis": node.purchase_units.stock_basis, "purchase_basis": node.purchase_units.purchase_basis,
            "price_version_id": price.id, "price_version_number": price.version_number,
            "quote_snapshot": preview["price"], "pricing_quantity": str(pricing["pricing_quantity"]),
            "purchase_unit_price": str(pricing["purchase_unit_price"]),
            "quoted_line_amount": str(line), "quoted_tax_amount": str(tax),
            "currency": "CNY", "tax_included": True,
            "estimated_material_cost": str(total),
            "cost_scope_label": "当前含税材料报价估算，不含另计运费、样品、版费和模具费，非实际成本",
        })
    return {"components": result, "missing_items": missing}
