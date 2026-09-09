"""Immutable, identity-based BOM plans. No database writes or stock postings.

The adapter must freeze this graph with the order and supply customer-scoped,
eligible stock. A material route is a physical piece group, not a BOM edge:
pieces per unit and cutting yield must each be applied exactly once.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping


class BomPlanError(ValueError):
    pass


def _integer(value: int, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise BomPlanError(f"{label}必须是大于等于{minimum}的整数")
    return value


@dataclass(frozen=True)
class MaterialRoute:
    # Unique frozen route identity: different piece types/specs never net off.
    key: str
    pieces_per_unit: int = 1
    pieces_per_sheet: int = 1


@dataclass(frozen=True)
class PurchaseUnits:
    purchase_unit: str
    stock_basis: str
    purchase_basis: str

    def validated(self):
        from decimal import Decimal, InvalidOperation
        if type(self.purchase_unit) is not str or not self.purchase_unit.strip() or len(self.purchase_unit) > 30:
            raise BomPlanError("外购采购单位无效")
        for value in (self.stock_basis, self.purchase_basis):
            try:
                if type(value) is not str or len(value) > 30:
                    raise ValueError()
                number = Decimal(value)
                if not number.is_finite() or number <= 0 or number >= Decimal('1e12') or number.quantize(Decimal('0.000001')) != number:
                    raise ValueError()
            except (ValueError, InvalidOperation):
                raise BomPlanError("外购库存与采购比例必须为正数，最多6位小数") from None
        return self


@dataclass(frozen=True)
class ProductNode:
    product_id: int
    customer_id: int
    version: int
    name: str
    unit: str
    source: str  # manufactured / purchased / assembled
    routes: tuple[MaterialRoute, ...] = ()
    purchase_units: PurchaseUnits | None = None


@dataclass(frozen=True)
class BomEdge:
    parent_id: int
    child_id: int
    quantity: int
    relation: str  # assembly consumes child; accompany keeps separate stock


@dataclass(frozen=True)
class MaterialDemand:
    product_id: int
    route_key: str
    required_pieces: int
    credited_pieces: int
    purchase_sheets: int
    excess_pieces: int


@dataclass(frozen=True)
class ProductDemand:
    product_id: int
    required_units: int
    credited_units: int
    make_units: int


@dataclass(frozen=True)
class DemandPath:
    path: tuple[int, ...]
    quantity: int


@dataclass(frozen=True)
class BomPlan:
    products: tuple[ProductDemand, ...]
    materials: tuple[MaterialDemand, ...]
    paths: tuple[DemandPath, ...]
    # Product IDs + physical units; the adapter resolves actual lots/locations.
    picking: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class FrozenBom:
    root_id: int
    customer_id: int
    nodes: tuple[ProductNode, ...]
    edges: tuple[BomEdge, ...]

    def validated(self):
        _integer(self.root_id, "父产品", 1)
        _integer(self.customer_id, "客户", 1)
        nodes = {}
        for node in self.nodes:
            _integer(node.product_id, "产品ID", 1)
            _integer(node.customer_id, "产品客户ID", 1)
            _integer(node.version, "产品版本", 1)
            if node.product_id in nodes or node.customer_id != self.customer_id:
                raise BomPlanError("重复产品或跨客户BOM")
            if not node.name.strip() or not node.unit.strip():
                raise BomPlanError("产品名称、库存单位不能为空")
            if node.source not in {"manufactured", "purchased", "assembled"}:
                raise BomPlanError("未知产品来源")
            if node.purchase_units is not None:
                if node.source != "purchased" or not isinstance(node.purchase_units, PurchaseUnits):
                    raise BomPlanError("只有外购节点可以配置采购比例")
                node.purchase_units.validated()
            route_keys = set()
            for route in node.routes:
                if not route.key.strip() or route.key in route_keys:
                    raise BomPlanError("物理片组身份缺失或重复")
                route_keys.add(route.key)
                _integer(route.pieces_per_unit, "每件物理片数", 1)
                _integer(route.pieces_per_sheet, "每张开料产出", 1)
            if bool(node.routes) != (node.source == "manufactured"):
                raise BomPlanError("自制产品必须有物理片组；外购/组套产品不能虚构自身纸板")
            nodes[node.product_id] = node
        if self.root_id not in nodes:
            raise BomPlanError("父产品不存在")
        children = {pid: [] for pid in nodes}
        indegree = {pid: 0 for pid in nodes}
        seen = set()
        for edge in self.edges:
            _integer(edge.parent_id, "关系父产品ID", 1)
            _integer(edge.child_id, "关系子产品ID", 1)
            _integer(edge.quantity, "每父件子件用量", 1)
            if edge.parent_id not in nodes or edge.child_id not in nodes:
                raise BomPlanError("BOM包含失效产品引用")
            key = (edge.parent_id, edge.child_id)
            if key in seen or edge.parent_id == edge.child_id:
                raise BomPlanError("重复子件或自引用BOM")
            if edge.relation not in {"assembly", "accompany"}:
                raise BomPlanError("必须明确组装或配套关系")
            seen.add(key)
            children[edge.parent_id].append(edge)
            indegree[edge.child_id] += 1
        for pid, node in nodes.items():
            assembly = [edge for edge in children[pid] if edge.relation == "assembly"]
            if node.source == "assembled" and not assembly:
                raise BomPlanError("组套产品缺少组装子件")
            if node.source == "purchased" and assembly:
                raise BomPlanError("外购成品不可同时重复消耗组装子件")
        # Deterministic Kahn order handles shared children without recursion limits.
        ready = sorted(pid for pid, count in indegree.items() if count == 0)
        order = []
        while ready:
            pid = ready.pop(0)
            order.append(pid)
            for edge in children[pid]:
                indegree[edge.child_id] -= 1
                if indegree[edge.child_id] == 0:
                    ready.append(edge.child_id)
                    ready.sort()
        if len(order) != len(nodes):
            raise BomPlanError("BOM存在循环引用")
        reachable = {self.root_id}
        for pid in order:
            if pid in reachable:
                reachable.update(edge.child_id for edge in children[pid])
        if reachable != set(nodes):
            raise BomPlanError("冻结BOM含不属于当前父产品的孤立节点")
        return nodes, children, order


def plan_bom(
    graph: FrozenBom,
    quantity: int,
    *,
    eligible_stock: Mapping[int, int] | None = None,
    eligible_pieces: Mapping[tuple[int, str], int] | None = None,
) -> BomPlan:
    """Net eligible product stock once, then physical-piece stock once.

    Accompanying goods remain necessary even if the parent comes from stock.
    Assembly children are needed only for NEW parent production. The two
    meanings cannot be inferred from the commercial delivery display mode.
    """
    quantity = _integer(quantity, "订单数量")
    nodes, children, order = graph.validated()
    stock = dict(eligible_stock or {})
    pieces = dict(eligible_pieces or {})
    for pid, count in stock.items():
        if type(pid) is not int or pid not in nodes:
            raise BomPlanError("库存抵扣包含无关产品")
        _integer(count, "可抵扣库存")
    route_keys = {(n.product_id, r.key) for n in graph.nodes for r in n.routes}
    for key, count in pieces.items():
        if key not in route_keys:
            raise BomPlanError("片料抵扣包含无关物理片组")
        _integer(count, "可抵扣片数")

    # Delivery context survives stock credits even through assembled branches:
    # an accessory is not magically included in an existing assembly's stock.
    context = defaultdict(int, {graph.root_id: quantity})
    for pid in order:
        for edge in children[pid]:
            context[edge.child_id] += context[pid] * edge.quantity
    demand = defaultdict(int, {graph.root_id: quantity})
    paths = [DemandPath((graph.root_id,), quantity)]
    products, materials = [], []
    for pid in order:
        required = demand[pid]
        credited = min(stock.get(pid, 0), required)
        make = required - credited
        products.append(ProductDemand(pid, required, credited, make))
        for route in nodes[pid].routes:
            required_pieces = make * route.pieces_per_unit
            piece_credit = min(pieces.get((pid, route.key), 0), required_pieces)
            net = required_pieces - piece_credit
            sheets = (net + route.pieces_per_sheet - 1) // route.pieces_per_sheet
            materials.append(MaterialDemand(
                pid, route.key, required_pieces, piece_credit, sheets,
                sheets * route.pieces_per_sheet - net,
            ))
        for edge in children[pid]:
            child_qty = (make if edge.relation == "assembly" else context[pid]) * edge.quantity
            demand[edge.child_id] += child_qty
            # Preserve source-edge allocation without exponentially duplicating
            # every full root path in a shared DAG. Frozen edges retain ancestry.
            paths.append(DemandPath((pid, edge.child_id), child_qty))

    # Picking follows physical stock nodes. Parts already consumed in assembly
    # do not appear again as independently pickable finished inventory.
    pick = defaultdict(int, {graph.root_id: quantity})
    for pid in order:
        for edge in children[pid]:
            if edge.relation == "accompany":
                pick[edge.child_id] += context[pid] * edge.quantity
    return BomPlan(
        tuple(products), tuple(materials), tuple(paths),
        tuple((pid, pick[pid]) for pid in order if pick[pid]),
    )


@dataclass(frozen=True)
class AssemblyStep:
    product_id: int
    produced_units: int
    consumed: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ReceiptAssemblyPlan:
    steps: tuple[AssemblyStep, ...]
    remaining_stock: tuple[tuple[int, int], ...]


def plan_assembly(graph: FrozenBom, quantity: int, *, eligible_stock: Mapping[int, int],
                  fulfilled_stock: Mapping[int, int] | None = None) -> ReceiptAssemblyPlan:
    """Incremental, bottom-up short-board plan; retains every excess unit.

    Callers must supply currently eligible balances, never cumulative receipts.
    Manufactured nodes need their own material-completion evidence and are NOT
    synthesized here; this planner only credits explicit assembled products.
    Database adapters still must enforce reservations, idempotency, cost,
    versions, audit and atomic debits/credits before applying any step.
    """
    nodes, children, order = graph.validated()
    credits = dict(eligible_stock)
    for pid, count in (fulfilled_stock or {}).items():
        if type(pid) is not int or pid not in nodes or nodes[pid].source != "assembled":
            raise BomPlanError("历史组装抵扣包含无关产品")
        _integer(count, "历史组装抵扣")
        credits[pid] = credits.get(pid, 0) + count
    # Fulfilled credits are NOT available inputs. In particular, an inner
    # assembly consumed into an outer assembly must not be credited twice.
    demand = plan_bom(graph, quantity, eligible_stock=credits)
    needs = {p.product_id: p.make_units for p in demand.products}
    if any(nodes[pid].source == "manufactured" and any(e.relation == "assembly" for e in children[pid]) for pid in order):
        raise BomPlanError("自制本体加子件组装需要本体完工来源，不能仅凭子件余额入库")
    balances = {pid: eligible_stock.get(pid, 0) for pid in order}
    steps = []
    for pid in reversed(order):
        if nodes[pid].source != "assembled":
            continue
        inputs = [edge for edge in children[pid] if edge.relation == "assembly"]
        make = min(needs[pid], *(balances[e.child_id] // e.quantity for e in inputs))
        if not make:
            continue
        consumed = tuple((e.child_id, make * e.quantity) for e in inputs)
        for child_id, count in consumed:
            balances[child_id] -= count
        balances[pid] += make
        steps.append(AssemblyStep(pid, make, consumed))
    return ReceiptAssemblyPlan(tuple(steps), tuple((pid, balances[pid]) for pid in order))
