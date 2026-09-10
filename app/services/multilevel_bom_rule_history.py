"""Batched structural history loading and one current-source projection."""
from collections import defaultdict
from dataclasses import dataclass, field, replace

from sqlalchemy import select

from app.models.multilevel_bom import OrderBomRuleRevision, OrderBomRuleProduct, OrderBomRuleSource
from app.services.multilevel_bom_plan import BomPlanError


@dataclass
class RuleHistory:
    revisions: list = field(default_factory=list)
    products: dict = field(default_factory=lambda: defaultdict(list))
    sources: dict = field(default_factory=lambda: defaultdict(list))


def rule_histories_by_order(db, order_item_ids):
    result = defaultdict(RuleHistory)
    if not order_item_ids:
        return result
    revisions = list(db.scalars(select(OrderBomRuleRevision).where(
        OrderBomRuleRevision.order_item_id.in_(order_item_ids)).order_by(
            OrderBomRuleRevision.order_item_id, OrderBomRuleRevision.revision)))
    owners = {}
    for row in revisions:
        result[row.order_item_id].revisions.append(row)
        owners[row.id] = row.order_item_id
    if not owners:
        return result
    for model, attribute in ((OrderBomRuleProduct, "products"), (OrderBomRuleSource, "sources")):
        for row in db.scalars(select(model).where(model.revision_id.in_(owners))):
            owner = owners[row.revision_id]
            if row.order_item_id != owner:
                raise BomPlanError("规则版本产品或来源跨订单")
            getattr(result[owner], attribute)[row.revision_id].append(row)
    return result


def base_graph_with_rule_registry(header, item, order, identities, history):
    """Registry retains each product's first identity for historical stock FKs.

    The original graph still requires exactly its original identities. Every
    additional registry identity must be introduced by a validated revision;
    a changed version of an existing product never overwrites its first row.
    """
    from app.services.multilevel_bom_snapshot import load_graph
    from app.services.multilevel_bom_orders import validate_order_graph_rows
    graph = load_graph(header.document_json, expected_hash=header.content_hash)
    initial = {(node.product_id, node.version) for node in graph.nodes}
    expected = dict(initial)
    for revision in history.revisions:
        for product in history.products[revision.id]:
            expected.setdefault(product.product_id, product.product_version)
    if identities != set(expected.items()):
        raise BomPlanError("订单BOM快照身份校验失败：原始及版本产品登记不一致")
    return validate_order_graph_rows(header, item, order, initial)


def project_order_rule_history(*, header, item, order, identities, cutover,
                               rows_with_roles, production_rows, history, source_snapshot_id=None):
    from app.services.multilevel_bom_execution_boundary import select_execution_sources
    from app.services.multilevel_bom_rule_revision import project_rule_and_production_events
    graph = base_graph_with_rule_registry(header, item, order, identities, history)
    pairs = tuple(rows_with_roles)
    row_map = {row.id: row for row, _ in pairs}
    if len(row_map) != len(pairs):
        raise BomPlanError("订单BOM来源重复")
    roles = {row.id: role for row, role in pairs}
    revision_sources, all_revision_ids = {}, set()
    for revision in history.revisions:
        sources = []
        for link in history.sources[revision.id]:
            source = row_map.get(link.snapshot_id)
            if (source is None or source.sales_order_item_id != item.id
                    or source.component_product_id != link.product_id
                    or roles[link.snapshot_id] is not None
                    or link.snapshot_id in all_revision_ids):
                raise BomPlanError("规则版本来源缺失、错绑或重复归属")
            sources.append(source)
            all_revision_ids.add(source.id)
        revision_sources[revision.id] = sources
    base = select_execution_sources(graph=graph, item=item, cutover=cutover,
        rows_with_roles=((row, role) for row, role in pairs if row.id not in all_revision_ids))
    compiled = project_rule_and_production_events(base, production_rows, history.revisions,
        sources_by_revision=revision_sources, products_by_revision=dict(history.products),
        delivered_quantity=item.delivered_quantity or 0, source_snapshot_id=source_snapshot_id)
    if history.revisions and source_snapshot_id is None:
        current_ids = {row.id for row in compiled.snapshots}
        compiled = replace(compiled, rule_revision_id=history.revisions[-1].id,
            history_source_ids=frozenset(row_map.keys() - current_ids))
    return compiled
