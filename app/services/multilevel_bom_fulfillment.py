"""Physical fulfillment only. Commercial settlement is a separate decision."""
from dataclasses import dataclass
from typing import Mapping

from app.services.multilevel_bom_plan import BomPlanError, FrozenBom, _integer, plan_bom


@dataclass(frozen=True)
class ComponentFulfillment:
    product_id: int
    unit: str
    required: int
    dispatched: int
    remaining: int


@dataclass(frozen=True)
class BomFulfillment:
    components: tuple[ComponentFulfillment, ...]
    complete: bool
    physically_paired_sets: int


def component_fulfillment(graph: FrozenBom, quantity: int,
                          dispatched: Mapping[int, int]) -> BomFulfillment:
    """Derive completion from ALL physical pick identities, never one child.

    Quantities are cumulative active physical allocations, net of reversals.
    physically_paired_sets is warehouse progress and grants no billing rights.
    """
    _integer(quantity, "订单套数", 1)
    requirements = dict(plan_bom(graph, quantity).picking)
    units = {node.product_id: node.unit for node in graph.nodes}
    for pid, amount in dispatched.items():
        if type(pid) is not int or pid not in requirements:
            raise BomPlanError("实发数量包含不属于本单拿货规则的产品")
        _integer(amount, "累计实发数量")
    components = tuple(ComponentFulfillment(pid, units[pid], required,
        dispatched.get(pid, 0), max(required - dispatched.get(pid, 0), 0))
        for pid, required in requirements.items())
    if not components:
        raise BomPlanError("订单缺少真实交付产品")
    paired = min(quantity, *(row.dispatched * quantity // row.required for row in components))
    return BomFulfillment(components, all(row.remaining == 0 for row in components), paired)


def preview_component_dispatch(graph: FrozenBom, quantity: int, *,
                               dispatched: Mapping[int, int],
                               requested: Mapping[int, int]) -> BomFulfillment:
    if graph.modes is None or graph.modes.delivery != "components":
        raise BomPlanError("本订单冻结规则不是子件实发")
    before = component_fulfillment(graph, quantity, dispatched)
    remaining = {row.product_id: row.remaining for row in before.components}
    if not requested:
        raise BomPlanError("请填写本次子件实发数量")
    for pid, amount in requested.items():
        if type(pid) is not int or pid not in remaining:
            raise BomPlanError("本次实发产品不属于订单冻结拿货规则")
        _integer(amount, "本次子件实发数量")
        if amount > remaining[pid]:
            raise BomPlanError(f"产品{pid}本次实发超过剩余{remaining[pid]}，请先处理超送授权")
    if not any(requested.values()):
        raise BomPlanError("本次至少一个子件实发数量必须大于0")
    return component_fulfillment(graph, quantity,
        {pid: dispatched.get(pid, 0) + requested.get(pid, 0) for pid in remaining})
