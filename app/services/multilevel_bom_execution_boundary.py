"""Selection/quantity contracts for an explicitly recorded execution cutover.

Not an authorization to create or infer a cutover from delivered quantities.
Historical document lookups still use their original direct snapshot IDs.
"""
from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import Numeric, exists, or_, and_, select

from app.models.multilevel_bom import OrderBomExecutionCutover, OrderBomCutoverSource
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_plan import BomPlanError


def current_snapshot_predicate(snapshot=SalesOrderItemBomComponent):
    from sqlalchemy.orm import aliased
    from app.models.multilevel_bom import OrderBomRuleRevision, OrderBomRuleSource
    cutover = exists().where(OrderBomExecutionCutover.order_item_id == snapshot.sales_order_item_id)
    current = exists().where(OrderBomCutoverSource.snapshot_id == snapshot.id,
        OrderBomCutoverSource.order_item_id == snapshot.sales_order_item_id,
        OrderBomCutoverSource.role == "current")
    rule = OrderBomRuleRevision
    newer = aliased(OrderBomRuleRevision)
    has_rule = exists().where(rule.order_item_id == snapshot.sales_order_item_id)
    has_newer = exists().where(newer.order_item_id == rule.order_item_id, newer.revision > rule.revision)
    current_rule = exists(select(1).select_from(OrderBomRuleSource).join(rule,
        rule.id == OrderBomRuleSource.revision_id).where(
            OrderBomRuleSource.snapshot_id == snapshot.id,
            OrderBomRuleSource.order_item_id == snapshot.sales_order_item_id,
            rule.order_item_id == snapshot.sales_order_item_id, ~has_newer))
    return or_(and_(has_rule, current_rule), and_(~has_rule, or_(~cutover, current)))


def cutover_roles_by_order(db, order_item_ids):
    """One explicit role query for all converted orders; none for ordinary ones."""
    from collections import defaultdict
    result = defaultdict(dict)
    if order_item_ids:
        for row in db.scalars(select(OrderBomCutoverSource).where(
                OrderBomCutoverSource.order_item_id.in_(order_item_ids))):
            result[row.order_item_id][row.snapshot_id] = row.role
    return result


def handoff_assembly_ids(db, compiled):
    """Stock assembled from old execution inputs, not new receipt capacity."""
    if compiled.execution_window is None:
        return set()
    from app.models.multilevel_bom import BomAssembly
    from app.services.multilevel_bom_inventory import _node_keys
    item_id = compiled.snapshots[0].sales_order_item_id
    if compiled.rule_revision_id is not None:
        from app.models.multilevel_bom import OrderBomRuleRevision
        cutover = db.get(OrderBomRuleRevision, compiled.rule_revision_id)
    else:
        cutover = db.get(OrderBomExecutionCutover, item_id)
    if cutover is None:
        raise BomPlanError("订单执行边界缺少原始版本事实")
    keys = _node_keys(cutover.idempotency_key + ":assemble",
        [node.product_id for node in compiled.graph.nodes if node.source == "assembled"])
    return set(db.scalars(select(BomAssembly.id).where(BomAssembly.order_item_id == item_id,
        BomAssembly.status == "posted", BomAssembly.idempotency_key.in_(keys.values()))))


@dataclass(frozen=True)
class ExecutionWindow:
    commercial_quantity: int
    delivered_before: int
    execution_quantity: int
    delivered_since: int
    remaining_quantity: int


def execution_window(*, order_quantity, delivered_quantity, cutover=None):
    if (type(order_quantity) is not int or order_quantity <= 0
            or type(delivered_quantity) is not int or delivered_quantity < 0):
        raise BomPlanError("订单或已送数量无效")
    baseline = 0
    if cutover is not None:
        if (type(cutover.order_quantity) is not int or cutover.order_quantity != order_quantity
                or type(cutover.delivered_before) is not int
                or not 0 <= cutover.delivered_before < order_quantity):
            raise BomPlanError("订单数量与执行转换边界不一致")
        baseline = cutover.delivered_before
        if delivered_quantity < baseline:
            raise BomPlanError("不能跨越BOM转换边界撤销旧送货，请先核对历史转换")
    # Authorized excess delivery is possible; do not make a negative demand.
    return ExecutionWindow(order_quantity, baseline, order_quantity - baseline,
        delivered_quantity - baseline, max(order_quantity - delivered_quantity, 0))


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as error:
        raise BomPlanError("BOM转换来源内容无效") from error


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_identity(row):
    # Match the immutable production-basis contract. The optional master edge
    # legally becomes NULL on later template deletion; frozen snapshot/product
    # IDs and the graph remain authoritative, not that disposable template link.
    fields = {}
    for column in SalesOrderItemBomComponent.__table__.columns:
        if column.key in {"created_at", "product_bom_component_id"}:
            continue
        value = getattr(row, column.key)
        if value is not None and isinstance(column.type, Numeric):
            value = str(Decimal(str(value)).normalize())
        fields[column.key] = value
    return {"snapshot_id": row.id, "product_id": row.component_product_id,
            "hash": _digest(_canonical(fields))}


def make_cutover_basis(*, graph, order_item_id, order_quantity, delivered_before,
                       history_rows, current_rows):
    """Build an exact source manifest, after IDs exist, without changing any row.

    This is not a conversion writer. Permission, CAS, inventory/reservation
    cutover and transaction ownership remain the dedicated adapter's job.
    """
    from app.services.multilevel_bom_orders import validate_compiled_order_rows
    from app.services.multilevel_bom_snapshot import dump_graph
    history_rows, current_rows = tuple(history_rows), tuple(current_rows)
    window = execution_window(order_quantity=order_quantity, delivered_quantity=delivered_before,
        cutover=SimpleNamespace(order_quantity=order_quantity, delivered_before=delivered_before))
    rows = history_rows + current_rows
    ids = [row.id for row in rows]
    if (type(order_item_id) is not int or order_item_id <= 0 or not history_rows or not current_rows
            or len(rows) > 2000 or any(type(i) is not int or i <= 0 for i in ids)
            or len(set(ids)) != len(ids)
            or any(row.sales_order_item_id != order_item_id for row in rows)):
        raise BomPlanError("BOM转换来源缺失、重复或不属于当前订单")
    for row in history_rows:
        if (type(row.snapshot_schema_version) is not int or not 1 <= row.snapshot_schema_version <= 4
                or row.order_set_quantity != order_quantity or row.quantity_per_set <= 0
                or row.required_piece_quantity != order_quantity * row.quantity_per_set):
            raise BomPlanError("旧BOM冻结数量或版本不一致，不能自动转换")
    validate_compiled_order_rows(graph, tuple(current_rows))
    if any(row.order_set_quantity != window.execution_quantity for row in current_rows):
        raise BomPlanError("新BOM必须仅包含转换后的待执行数量")
    document = _canonical({"schema": 1, "order_item_id": order_item_id,
        "order_quantity": order_quantity, "delivered_before": delivered_before,
        "graph_hash": _digest(dump_graph(graph)),
        "history": [_source_identity(row) for row in sorted(history_rows, key=lambda row: row.id)],
        "current": [_source_identity(row) for row in sorted(current_rows, key=lambda row: row.id)]})
    return document, _digest(document)


def select_execution_sources(*, graph, item, cutover, rows_with_roles):
    """Validate both epochs, then return current sources plus quantity window."""
    from app.services.multilevel_bom_orders import validate_compiled_order_rows
    pairs = tuple(rows_with_roles)
    if cutover is None:
        if any(role is not None for _, role in pairs):
            raise BomPlanError("BOM来源存在无执行边界的归属")
        return validate_compiled_order_rows(graph, tuple(row for row, _ in pairs))
    if cutover.order_item_id != item.id or any(role not in {"history", "current"} for _, role in pairs):
        raise BomPlanError("BOM转换来源归属不完整")
    history = tuple(row for row, role in pairs if role == "history")
    current = tuple(row for row, role in pairs if role == "current")
    window = execution_window(order_quantity=item.quantity, delivered_quantity=item.delivered_quantity or 0,
                              cutover=cutover)
    document, checksum = make_cutover_basis(graph=graph, order_item_id=item.id,
        order_quantity=cutover.order_quantity, delivered_before=cutover.delivered_before,
        history_rows=history, current_rows=current)
    if (cutover.basis_json != document or cutover.basis_hash != checksum):
        raise BomPlanError("BOM转换原始来源摘要不一致，禁止继续执行")
    return replace(validate_compiled_order_rows(graph, current), execution_window=window)
