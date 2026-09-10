"""Canonical, immutable BOM wire format shared by order and receipt adapters.

Never rebuild a frozen order from current product names or a material code.
This module deliberately performs no stock writes and does not enable nested
orders before the transaction adapters have passed their acceptance gates.
"""

import hashlib
import json
from dataclasses import asdict

from app.services.multilevel_bom_plan import (
    BomEdge, BomModes, BomPlanError, FrozenBom, MaterialRoute, ProductNode, PurchaseUnits,
)

SCHEMA_VERSION = 1
PURCHASE_SCHEMA_VERSION = 2
MODES_SCHEMA_VERSION = 3
MAX_NODES = 1000
MAX_EDGES = 5000
MAX_DOCUMENT_BYTES = 2_000_000


def _object(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise BomPlanError(f"{label}字段不完整或包含未知字段")
    return value


def _text(value, label, limit=250):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise BomPlanError(f"{label}无效")
    return value


def graph_schema_version(graph):
    if graph.modes is not None:
        return MODES_SCHEMA_VERSION
    return PURCHASE_SCHEMA_VERSION if any(n.purchase_units is not None for n in graph.nodes) else SCHEMA_VERSION


def dump_graph(graph: FrozenBom) -> str:
    """Return deterministic JSON; order-independent declarations hash equally."""
    graph.validated()
    schema_version = graph_schema_version(graph)
    document = {
        "schema_version": schema_version,
        "root_id": graph.root_id,
        "customer_id": graph.customer_id,
        "nodes": [
            {**{k: v for k, v in asdict(node).items() if k != "purchase_units" or schema_version >= PURCHASE_SCHEMA_VERSION},
             "routes": [asdict(r) for r in sorted(node.routes, key=lambda r: r.key)]}
            for node in sorted(graph.nodes, key=lambda n: n.product_id)
        ],
        "edges": [asdict(edge) for edge in sorted(graph.edges, key=lambda e: (e.parent_id, e.child_id))],
    }
    if graph.modes is not None:
        document["modes"] = asdict(graph.modes)
    result = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # Apply the same field/size checks on both sides of the storage boundary.
    load_graph(result)
    return result


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise BomPlanError("BOM快照包含重复字段")
        obj[key] = value
    return obj


def load_graph(document: str, *, expected_hash: str | None = None) -> FrozenBom:
    if type(document) is not str or len(document.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise BomPlanError("BOM快照格式或大小无效")
    if expected_hash is not None and (
        type(expected_hash) is not str or graph_hash(document) != expected_hash
    ):
        raise BomPlanError("BOM快照校验失败，不得继续库存操作")
    try:
        data = json.loads(document, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError, RecursionError) as error:
        raise BomPlanError("BOM快照不是有效JSON") from error
    if type(data) is not dict:
        raise BomPlanError("BOM快照必须为对象")
    version = data.get("schema_version")
    _object(data, ("schema_version", "root_id", "customer_id", "nodes", "edges")
            + (("modes",) if version == MODES_SCHEMA_VERSION else ()), "BOM快照")
    if type(version) is not int or version not in (SCHEMA_VERSION, PURCHASE_SCHEMA_VERSION, MODES_SCHEMA_VERSION):
        raise BomPlanError("不支持的BOM快照版本")
    modes = None
    if version == MODES_SCHEMA_VERSION:
        _object(data["modes"], ("material", "inventory", "delivery"), "BOM执行规则")
        for value in data["modes"].values():
            _text(value, "BOM执行规则", 30)
        modes = BomModes(**data["modes"])
    if type(data["nodes"]) is not list or not 1 <= len(data["nodes"]) <= MAX_NODES:
        raise BomPlanError("BOM产品数量无效")
    if type(data["edges"]) is not list or len(data["edges"]) > MAX_EDGES:
        raise BomPlanError("BOM关系数量无效")
    nodes = []
    for node in data["nodes"]:
        fields = ("product_id", "customer_id", "version", "name", "unit", "source", "routes")
        _object(node, fields + (("purchase_units",) if version >= PURCHASE_SCHEMA_VERSION else ()), "产品")
        units = node.get("purchase_units")
        if units is not None:
            _object(units, ("purchase_unit", "stock_basis", "purchase_basis"), "外购单位比例")
            units = PurchaseUnits(**units).validated()
        _text(node["name"], "产品名称")
        _text(node["unit"], "库存单位", 30)
        _text(node["source"], "产品来源", 30)
        if type(node["routes"]) is not list or len(node["routes"]) > 99:
            raise BomPlanError("物理片组数量无效")
        routes = []
        for route in node["routes"]:
            _object(route, ("key", "pieces_per_unit", "pieces_per_sheet"), "物理片组")
            _text(route["key"], "物理片组标识", 100)
            routes.append(MaterialRoute(**route))
        nodes.append(ProductNode(**{**node, "routes": tuple(routes), "purchase_units": units}))
    edges = []
    for edge in data["edges"]:
        _object(edge, ("parent_id", "child_id", "quantity", "relation"), "BOM关系")
        _text(edge["relation"], "BOM关系类型", 30)
        edges.append(BomEdge(**edge))
    graph = FrozenBom(data["root_id"], data["customer_id"], tuple(nodes), tuple(edges), modes)
    graph.validated()
    if graph_schema_version(graph) != data["schema_version"]:
        raise BomPlanError("BOM采购快照版本与内容不一致")
    return graph


def graph_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def verify_current_identities(graph: FrozenBom, products) -> None:
    """Before freezing ONLY: reject stale/cross-customer/inactive master data.

    After freezing, use the frozen graph for calculations, not current master
    versions. Ordinary later master edits must not rewrite historical orders.
    """
    graph.validated()
    by_id = {product.id: product for product in products}
    for node in graph.nodes:
        product = by_id.get(node.product_id)
        if (product is None or product.customer_id != graph.customer_id
                or not product.is_active or product.deleted_at is not None
                or product.purged_at is not None):
            raise BomPlanError("BOM产品已失效或不属于本客户")
        if product.version != node.version:
            raise BomPlanError("BOM产品版本已变化，请刷新后重试")
        if product.product_name != node.name or product.unit != node.unit:
            raise BomPlanError("BOM产品名称或单位与正式产品不一致")
