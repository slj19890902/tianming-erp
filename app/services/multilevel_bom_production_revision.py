"""Pure, hash-bound production amendments; never mutate frozen ORM rows.

Persistence and qualification belong to the order transaction adapter. This
module alone does not authorize a revision or activate it for procurement.
"""
import hashlib
import json
from dataclasses import replace
from decimal import Decimal

from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_compile import CompiledMasterBom, physical_routes
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_snapshot import dump_graph


POSITIVE_FIELDS = frozenset(("material_id", "layer_count", "report_length_mm", "report_width_mm",
    "base_report_length_mm", "base_report_width_mm", "pieces_per_box"))
NONNEGATIVE_FIELDS = frozenset(("crease_left_mm", "crease_middle_mm", "crease_right_mm",
    "base_crease_left_mm", "base_crease_middle_mm", "base_crease_right_mm", "flap_mm"))
TEXT_FIELDS = frozenset(("material", "supplier_name", "flute_type", "production_notes", "crease_type",
    "report_notes", "base_crease_type", "base_report_notes", "splice_mode", "default_cutting_mode"))
FIELDS = POSITIVE_FIELDS | NONNEGATIVE_FIELDS | TEXT_FIELDS
PREFIX = "snapshot_component_"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def production_basis(compiled):
    """Bind an amendment to material values as well as the physical graph."""
    from app.services.multilevel_bom_orders import validate_compiled_order_rows
    validate_compiled_order_rows(compiled.graph, compiled.snapshots)
    def scalar(value):
        return str(value.normalize()) if isinstance(value, Decimal) else value
    return _hash({"graph": dump_graph(compiled.graph), "materials": [
        {"snapshot_id": row.id, "order_item_id": row.sales_order_item_id,
         "product_id": row.component_product_id,
         "fields": {column.key: scalar(getattr(row, column.key))
                    for column in SalesOrderItemBomComponent.__table__.columns
                    if column.key not in {"created_at", "product_bom_component_id"}
                    and not (column.key == "sheet_cutting_settings_snapshot" and getattr(row, column.key) is None)}}
        for row in sorted(compiled.snapshots, key=lambda row: row.component_product_id)]})


def _validate_changes(changes, compiled):
    if type(changes) is not dict or not changes:
        raise BomPlanError("生产资料修订不能为空")
    nodes = {str(node.product_id): node for node in compiled.graph.nodes}
    for pid, fields in changes.items():
        if type(pid) is not str or pid not in nodes or nodes[pid].source != "manufactured":
            raise BomPlanError("只能修订当前BOM中的自制产品资料")
        if type(fields) is not dict or not fields or set(fields) - FIELDS:
            raise BomPlanError("生产资料修订包含未知或禁止字段")
        for field, value in fields.items():
            if value is None:
                continue
            if field in TEXT_FIELDS:
                column = SalesOrderItemBomComponent.__table__.c[PREFIX + field]
                limit = getattr(column.type, "length", None) or 4000
                valid = type(value) is str and len(value) <= limit
            else:
                minimum = 1 if field in POSITIVE_FIELDS else 0
                valid = type(value) is int and minimum <= value <= 2_147_483_647
            if not valid:
                raise BomPlanError(f"生产资料修订字段{field}无效")
            if field == "default_cutting_mode":
                snapshot = next(row for row in compiled.snapshots if str(row.component_product_id) == pid)
                if snapshot.sheet_cutting_settings_snapshot is not None and value != snapshot.snapshot_component_default_cutting_mode:
                    raise BomPlanError("该组件已使用独立模数与开料，请在报料草稿中点击修改开料")
                from app.services.requisition_quantities import normalize_cutting_mode
                try:
                    normalize_cutting_mode(value, strict=True)
                except ValueError as error:
                    raise BomPlanError(str(error)) from error
            if field == "splice_mode":
                from app.services.box_type_rules import SPLICE_MODES
                if value not in SPLICE_MODES:
                    raise BomPlanError("生产资料修订拼版方式无效")


def prepare_production_revision(compiled, changes):
    """Return canonical document/hash for storage by an authorized adapter."""
    _validate_changes(changes, compiled)
    document = _canonical({"schema": 1, "basis": production_basis(compiled), "changes": changes})
    checksum = hashlib.sha256(document.encode("utf-8")).hexdigest()
    # Validate resulting routes before allowing an amendment to be recorded.
    apply_production_revision(compiled, document, expected_hash=checksum)
    return document, checksum


def apply_production_revision(compiled, document, *, expected_hash):
    if (type(document) is not str or len(document.encode("utf-8")) > 2_000_000
            or type(expected_hash) is not str
            or hashlib.sha256(document.encode("utf-8")).hexdigest() != expected_hash):
        raise BomPlanError("生产资料修订校验失败")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BomPlanError("生产资料修订包含重复字段")
            result[key] = value
        return result
    try:
        data = json.loads(document, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError) as error:
        raise BomPlanError("生产资料修订格式无效") from error
    if (type(data) is not dict or set(data) != {"schema", "basis", "changes"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or data["basis"] != production_basis(compiled)):
        raise BomPlanError("生产资料修订与当前冻结版本不一致")
    changes = data["changes"]
    _validate_changes(changes, compiled)
    snapshots = []
    routes = {}
    for original in compiled.snapshots:
        # Do not attach these projections to a Session. Historical source IDs
        # remain stable; every original material column remains untouched.
        row = SalesOrderItemBomComponent(**{column.key: getattr(original, column.key)
            for column in SalesOrderItemBomComponent.__table__.columns})
        fields = changes.get(str(row.component_product_id))
        if fields:
            for field, value in fields.items():
                setattr(row, PREFIX + field, value)
            routes[row.component_product_id] = physical_routes(row)
        snapshots.append(row)
    graph = replace(compiled.graph, nodes=tuple(replace(node, routes=routes[node.product_id])
        if node.product_id in routes else node for node in compiled.graph.nodes))
    from app.services.multilevel_bom_orders import validate_compiled_order_rows
    validate_compiled_order_rows(graph, tuple(snapshots))
    return replace(compiled, graph=graph, snapshots=tuple(snapshots))
