"""Quantity-only plans for receipt-time sub-kit assembly.

No inventory is posted here. The transaction adapter must pass eligible,
customer-scoped stock after excluding other orders' reservations, and persist
the component debits and kit credit atomically against frozen BOM snapshots.
Product IDs (never shared material codes) identify members.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.composite_bom_execution import (
    CompositeBOMExecutionError,
    as_integer,
)


@dataclass(frozen=True)
class SubkitMember:
    product_id: int
    pieces_per_kit: int


@dataclass(frozen=True)
class ComponentBalancePlan:
    product_id: int
    before_pieces: int
    consumed_pieces: int
    retained_pieces: int
    loss_pieces: int
    shortage_pieces: int


@dataclass(frozen=True)
class SubkitAssemblyPlan:
    kit_quantity: int
    remaining_kit_demand: int
    components: tuple[ComponentBalancePlan, ...]


def _quantity(value: Any, label: str, *, positive: bool = False) -> int:
    return as_integer(value, label=label, minimum=1 if positive else 0)


def normalize_members(
    members: Sequence[SubkitMember], *, parent_product_id: int, kit_product_id: int,
    allow_parent_output: bool = False,
) -> tuple[SubkitMember, ...]:
    """Reject ambiguous/self-referencing recipes before computing any quantities."""
    parent = _quantity(parent_product_id, "父产品", positive=True)
    kit = _quantity(kit_product_id, "子套件产品", positive=True)
    if parent == kit and not allow_parent_output:
        raise CompositeBOMExecutionError("父产品与子套件必须分别登记")
    if not members:
        raise CompositeBOMExecutionError("子套件至少需要一个组件")
    normalized = []
    seen = set()
    for member in members:
        product_id = _quantity(member.product_id, "组件产品", positive=True)
        pieces = _quantity(member.pieces_per_kit, "每套用量", positive=True)
        if product_id in (parent, kit) or product_id in seen:
            raise CompositeBOMExecutionError("子套件组件重复或包含父产品/子套件自身")
        seen.add(product_id)
        normalized.append(SubkitMember(product_id, pieces))
    return tuple(sorted(normalized, key=lambda row: row.product_id))


def _balances(
    values: Mapping[int, Any], members: tuple[SubkitMember, ...], label: str
) -> dict[int, int]:
    allowed = {member.product_id for member in members}
    if any(type(key) is not int or key not in allowed for key in values):
        raise CompositeBOMExecutionError(f"{label}包含非本子套件组件")
    return {
        member.product_id: _quantity(values.get(member.product_id, 0), label)
        for member in members
    }


def plan_receipt_assembly(
    *,
    parent_product_id: int,
    kit_product_id: int,
    members: Sequence[SubkitMember],
    remaining_kit_demand: int,
    eligible_pieces: Mapping[int, Any],
    confirmed_loss_pieces: Mapping[int, Any] | None = None,
    loss_confirmed: bool = False,
    allow_parent_output: bool = False,
) -> SubkitAssemblyPlan:
    """Assemble up to outstanding demand; retain every other piece by default.

    Inputs are current balances, NOT cumulative receipts. A replay is handled
    by the transaction's idempotency record, not by this pure function. Loss
    applies only to the remainder after assembly, never to pieces already
    converted or reserved by another order. A caller must separately authorize
    loss confirmation. No parent cartons are created or consumed by this plan.
    """
    recipe = normalize_members(
        members, parent_product_id=parent_product_id, kit_product_id=kit_product_id,
        allow_parent_output=allow_parent_output,
    )
    demand = _quantity(remaining_kit_demand, "尚需子套件数量")
    available = _balances(eligible_pieces, recipe, "可用片数")
    losses = _balances(confirmed_loss_pieces or {}, recipe, "损耗片数")
    if any(losses.values()) and loss_confirmed is not True:
        raise CompositeBOMExecutionError("生产损耗需要确认")
    assembled = min(demand, min(
        available[row.product_id] // row.pieces_per_kit for row in recipe
    ))
    remaining = demand - assembled
    components = []
    for member in recipe:
        before = available[member.product_id]
        consumed = assembled * member.pieces_per_kit
        loss = losses[member.product_id]
        if loss > before - consumed:
            raise CompositeBOMExecutionError("损耗不能超过组套后剩余片数")
        retained = before - consumed - loss
        components.append(ComponentBalancePlan(
            product_id=member.product_id,
            before_pieces=before,
            consumed_pieces=consumed,
            retained_pieces=retained,
            loss_pieces=loss,
            shortage_pieces=max(remaining * member.pieces_per_kit - retained, 0),
        ))
    return SubkitAssemblyPlan(assembled, remaining, tuple(components))


def plan_subkit_requisition(
    *,
    parent_product_id: int,
    kit_product_id: int,
    members: Sequence[SubkitMember],
    required_kits: int,
    allocated_existing_kits: int,
    allocated_existing_pieces: Mapping[int, Any],
) -> dict[int, int]:
    """Net piece demand after explicitly allocated, compatible existing stock.

    Never automatically borrow another customer's or another order's stock.
    The returned values are finished component pieces, not purchase sheets;
    existing cutting/yield rules must convert them to procurement quantities.
    """
    recipe = normalize_members(
        members, parent_product_id=parent_product_id, kit_product_id=kit_product_id
    )
    required = _quantity(required_kits, "子套件需求")
    allocated = _quantity(allocated_existing_kits, "已抵扣套数")
    if allocated > required:
        raise CompositeBOMExecutionError("抵扣套数不能超过子套件需求")
    pieces = _balances(allocated_existing_pieces, recipe, "已抵扣片数")
    demand = {}
    for row in recipe:
        gross = (required - allocated) * row.pieces_per_kit
        if pieces[row.product_id] > gross:
            raise CompositeBOMExecutionError("抵扣片数不能超过组件需求")
        demand[row.product_id] = gross - pieces[row.product_id]
    return demand
