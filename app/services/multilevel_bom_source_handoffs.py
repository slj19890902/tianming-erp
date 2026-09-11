"""Validate explicit current-version source links; never infer or post a handoff."""
from sqlalchemy import select

from app.models.multilevel_bom import OrderBomSourceHandoff
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_execution_boundary import _source_identity
from app.services.multilevel_bom_orders import read_order_bom_source_contract
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_rule_impact import rule_quantity_impact


def current_source_handoffs(db, compiled):
    """Return proven aliases separately from the original source contracts.

    Only rows belonging to the current structural revision qualify. A later
    revision must explicitly review and carry each remaining obligation again.
    The result does not authorize receiving, procurement or stock allocation.
    """
    if compiled.rule_revision_id is None:
        return ()
    order_ids = {row.sales_order_item_id for row in compiled.snapshots}
    if len(order_ids) != 1:
        raise BomPlanError("BOM来源交接缺少唯一订单身份")
    links = list(db.scalars(select(OrderBomSourceHandoff).where(
        OrderBomSourceHandoff.revision_id == compiled.rule_revision_id)
        .order_by(OrderBomSourceHandoff.source_snapshot_id)))
    for link in links:
        if link.source_snapshot_id not in compiled.history_source_ids:
            raise BomPlanError("来源交接不能引用当前或未来来源")
        validate_source_handoff(db, link, compiled)
    return tuple(links)


def validate_source_handoff(db, link, compiled):
    """Also validate a historical receipt's recorded execution version."""
    from app.models.multilevel_bom import OrderBomRuleRevision, OrderBomRuleSource
    targets = {row.id: row for row in compiled.snapshots}
    order_ids = {row.sales_order_item_id for row in compiled.snapshots}
    if order_ids != {link.order_item_id}:
        raise BomPlanError("来源交接订单不一致")
    order_id = link.order_item_id
    revision = db.get(OrderBomRuleRevision, link.revision_id)
    target_link = db.get(OrderBomRuleSource, link.target_snapshot_id)
    source_link = db.get(OrderBomRuleSource, link.source_snapshot_id)
    source_revision = db.get(OrderBomRuleRevision, source_link.revision_id) if source_link else None
    if (revision is None or target_link is None or target_link.revision_id != revision.id
            or revision.order_item_id != order_id
            or (source_link is not None and (source_revision is None or source_revision.revision >= revision.revision))):
        raise BomPlanError("来源交接版本顺序或目标不一致")
    target = targets.get(link.target_snapshot_id)
    source = db.get(SalesOrderItemBomComponent, link.source_snapshot_id)
    persisted_target = db.get(SalesOrderItemBomComponent, link.target_snapshot_id)
    if (link.order_item_id != order_id or target is None or source is None or persisted_target is None
            or source.sales_order_item_id != order_id
            or source.component_product_id != link.product_id
            or target.component_product_id != link.product_id
            or _source_identity(source)["hash"] != link.source_basis_hash
            or _source_identity(persisted_target)["hash"] != link.target_basis_hash):
        raise BomPlanError("BOM来源交接的订单、产品、原始依据或目标版本不一致")
    original = read_order_bom_source_contract(db, order_id, link.source_snapshot_id)
    quantity = min(source.order_set_quantity, target.order_set_quantity)
    impact = next(row for row in rule_quantity_impact(original, compiled,
        remaining_quantity=quantity)["products"] if row["product_id"] == link.product_id)
    node = next(node for node in original.graph.nodes if node.product_id == link.product_id)
    field = {"manufactured": "material_conversion_compatible", "purchased": "purchase_conversion_compatible"}.get(link.source_kind)
    if node.source != link.source_kind or field is None or not impact[field]:
        raise BomPlanError("BOM来源交接的实物身份、生产方式或单位换算不相容")
