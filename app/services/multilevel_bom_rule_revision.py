"""Hash-bound structural revisions, independent of authorization/persistence.

Every revision consumes the exact previous compiled basis and owns fresh
material-source IDs. Historical rows are never mutated by this projection.
"""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

from app.services.multilevel_bom_execution_boundary import _source_identity, execution_window
from app.services.multilevel_bom_orders import validate_compiled_order_rows
from app.services.multilevel_bom_plan import BomPlanError, _integer
from app.services.multilevel_bom_production_revision import production_basis
from app.services.multilevel_bom_snapshot import dump_graph, load_graph


def _document(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def prepare_rule_revision(previous, proposed, *, order_quantity, delivered_before):
    _integer(order_quantity, "订单数量", 1)
    _integer(delivered_before, "切换前已送数量")
    old_ids = {row.sales_order_item_id for row in previous.snapshots}
    new_ids = {row.sales_order_item_id for row in proposed.snapshots}
    if len(old_ids) != 1 or old_ids != new_ids or any(type(i) is not int or i <= 0 for i in old_ids):
        raise BomPlanError("规则版本来源不属于同一真实订单")
    if previous.graph.root_id != proposed.graph.root_id or previous.graph.customer_id != proposed.graph.customer_id:
        raise BomPlanError("规则版本不能改变订单父产品或客户身份")
    old_quantity = (previous.execution_window.commercial_quantity if previous.execution_window
                    else previous.snapshots[0].order_set_quantity)
    old_baseline = previous.execution_window.delivered_before if previous.execution_window else 0
    if order_quantity != old_quantity or delivered_before < old_baseline:
        raise BomPlanError("规则版本数量与原执行边界不一致")
    window = execution_window(order_quantity=order_quantity, delivered_quantity=delivered_before,
        cutover=SimpleNamespace(order_quantity=order_quantity, delivered_before=delivered_before))
    validate_compiled_order_rows(proposed.graph, proposed.snapshots)
    source_ids = [row.id for row in proposed.snapshots]
    if (any(type(i) is not int or i <= 0 for i in source_ids) or len(set(source_ids)) != len(source_ids)
            or set(source_ids) & {row.id for row in previous.snapshots}
            or any(row.order_set_quantity != window.execution_quantity for row in proposed.snapshots)):
        raise BomPlanError("新规则必须使用独立来源ID且只包含剩余执行数量")
    document = _document(dict(schema=1, order_item_id=next(iter(old_ids)), order_quantity=order_quantity,
        delivered_before=delivered_before, previous_basis=production_basis(previous),
        graph=dump_graph(proposed.graph),
        sources=[_source_identity(row) for row in sorted(proposed.snapshots, key=lambda row: row.id)]))
    return document, hashlib.sha256(document.encode("utf-8")).hexdigest()


def apply_rule_revision(previous, row, sources, products, *, delivered_quantity):
    """Validate stored document, source identities and relational product set."""
    document = row.document_json
    if (type(document) is not str or len(document.encode("utf-8")) > 2_000_000
            or hashlib.sha256(document.encode("utf-8")).hexdigest() != row.content_hash):
        raise BomPlanError("规则版本内容摘要不一致")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        data = json.loads(document, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError) as exc:
        raise BomPlanError("规则版本文档无效") from exc
    if (type(data) is not dict or set(data) != {"schema", "order_item_id", "order_quantity",
            "delivered_before", "previous_basis", "graph", "sources"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or data["order_item_id"] != row.order_item_id
            or data["order_quantity"] != row.order_quantity
            or data["delivered_before"] != row.delivered_before):
        raise BomPlanError("规则版本字段或订单边界不一致")
    graph = load_graph(data["graph"])
    products = tuple(products)
    if (len(products) != len(graph.nodes)
            or any(p.revision_id != row.id or p.order_item_id != row.order_item_id for p in products)
            or {(p.product_id, p.product_version) for p in products} != {(n.product_id, n.version) for n in graph.nodes}):
        raise BomPlanError("规则版本产品身份不完整")
    proposed = validate_compiled_order_rows(graph, tuple(sources))
    expected, checksum = prepare_rule_revision(previous, proposed, order_quantity=row.order_quantity,
        delivered_before=row.delivered_before)
    if document != expected or checksum != row.content_hash:
        raise BomPlanError("规则版本与原配方或新来源不一致")
    window = execution_window(order_quantity=row.order_quantity, delivered_quantity=delivered_quantity,
        cutover=SimpleNamespace(order_quantity=row.order_quantity, delivered_before=row.delivered_before))
    return replace(proposed, execution_window=window)


def validate_rule_revision_chain(rows, *, order_item_id, production_revision_count):
    """Reject missing/reordered rule events and invalid production interleaving."""
    _integer(production_revision_count, "生产资料版本数量")
    rows = tuple(rows)
    previous_id, previous_production, previous_delivery = None, 0, 0
    order_quantity = None
    for number, row in enumerate(rows, 1):
        if (row.order_item_id != order_item_id or row.revision != number
                or row.previous_id != previous_id
                or row.previous_revision != (number - 1 if number > 1 else None)
                or type(row.production_revision_before) is not int
                or not previous_production <= row.production_revision_before <= production_revision_count
                or type(row.delivered_before) is not int or row.delivered_before < previous_delivery
                or (order_quantity is not None and row.order_quantity != order_quantity)):
            raise BomPlanError("BOM规则版本链或生产资料交接顺序不完整")
        execution_window(order_quantity=row.order_quantity, delivered_quantity=row.delivered_before,
            cutover=SimpleNamespace(order_quantity=row.order_quantity, delivered_before=row.delivered_before))
        previous_id, previous_production = row.id, row.production_revision_before
        previous_delivery, order_quantity = row.delivered_before, row.order_quantity
    return rows


def project_rule_and_production_events(base, production_rows, rule_rows, *,
                                       sources_by_revision, products_by_revision, delivered_quantity,
                                       source_snapshot_id=None):
    """Replay amendments and structural changes in their recorded order."""
    from app.services.multilevel_bom_production_revision import apply_production_revision
    item_ids = {source.sales_order_item_id for source in base.snapshots}
    if len(item_ids) != 1:
        raise BomPlanError("规则版本缺少单一订单来源")
    item_id = next(iter(item_ids))
    production_rows = tuple(production_rows)
    rule_rows = validate_rule_revision_chain(rule_rows, order_item_id=item_id,
        production_revision_count=len(production_rows))
    revision_ids = {row.id for row in rule_rows}
    if set(sources_by_revision) != revision_ids or set(products_by_revision) != revision_ids:
        raise BomPlanError("规则版本来源或产品归属不完整")
    if source_snapshot_id is not None:
        _integer(source_snapshot_id, "历史BOM来源ID", 1)
    compiled, production_cursor, previous_production_id = base, 0, None
    used_source_ids = {row.id for row in base.snapshots}
    selected, selected_production, structural_id = None, 0, None

    def capture_source_contract():
        nonlocal selected, selected_production
        if source_snapshot_id is not None and any(s.id == source_snapshot_id for s in compiled.snapshots):
            selected = replace(compiled, rule_revision_id=structural_id)
            selected_production = production_cursor

    def apply_production_until(count):
        nonlocal compiled, production_cursor, previous_production_id
        while production_cursor < count:
            row = production_rows[production_cursor]
            if (row.revision != production_cursor + 1 or row.previous_id != previous_production_id
                    or row.order_item_id != item_id):
                raise BomPlanError("BOM生产资料版本链不完整")
            compiled = apply_production_revision(compiled, row.document_json, expected_hash=row.content_hash)
            production_cursor += 1
            previous_production_id = row.id

    for row in rule_rows:
        apply_production_until(row.production_revision_before)
        # Retain the final production basis belonging to these exact source
        # IDs. Continue validating every later event before returning history.
        capture_source_contract()
        sources = tuple(sources_by_revision[row.id])
        source_ids = {source.id for source in sources}
        if used_source_ids & source_ids:
            raise BomPlanError("规则版本重复使用历史来源")
        compiled = apply_rule_revision(compiled, row, sources, products_by_revision[row.id],
            delivered_quantity=delivered_quantity)
        structural_id = row.id
        used_source_ids.update(source_ids)
    apply_production_until(len(production_rows))
    capture_source_contract()
    if source_snapshot_id is not None:
        if selected is None:
            raise BomPlanError("历史BOM来源不属于已核验的订单规则版本")
        compiled, production_cursor = selected, selected_production
    # Same public revision counter as the existing production editor. It is
    # projection metadata only, never written over an original snapshot row.
    if production_cursor:
        for source in compiled.snapshots:
            source.production_revision = production_cursor
    return compiled
