"""Batch, read-only eligibility projection for inventory cost revaluation."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from sqlalchemy import select

from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer

READ_BATCH_SIZE = 500


def _chunks(values: list[int], size: int = READ_BATCH_SIZE):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def batch_revaluation_readiness(db, lots: Iterable[InventoryLot]) -> dict[int, bool]:
    """Return ``lot_id -> is_revaluable`` with bounded graph reads.

    Initial page entities provide their scalar facts. Transfer edges and
    ancestor scalar facts are fetched once per graph depth (and parameter
    chunk), while DFS path state is local to each root so a shared diamond is
    not mistaken for a cycle.
    """

    initial = {int(lot.id): lot for lot in lots}
    if not initial:
        return {}
    facts: dict[int, tuple[str | None, str | None, str | None]] = {
        lot_id: (
            getattr(lot, "source_type", None),
            getattr(lot, "source_ref_type", None),
            getattr(lot, "cost_snapshot_source", None),
        )
        for lot_id, lot in initial.items()
    }
    edges: dict[int, list[int]] = defaultdict(list)
    frontier = [
        lot_id
        for lot_id, fact in facts.items()
        if fact[0] == "transfer"
    ]
    seen_targets: set[int] = set()
    while frontier:
        targets = sorted(set(frontier) - seen_targets)
        if not targets:
            break
        seen_targets.update(targets)
        origins: set[int] = set()
        for chunk in _chunks(targets):
            rows = db.execute(
                select(
                    InventoryLotTransfer.target_lot_id,
                    InventoryLotTransfer.source_lot_id,
                )
                .where(InventoryLotTransfer.target_lot_id.in_(chunk))
                .distinct()
            ).all()
            for target_id, source_id in rows:
                target_id = int(target_id)
                source_id = int(source_id)
                if source_id not in edges[target_id]:
                    edges[target_id].append(source_id)
                origins.add(source_id)
        missing = sorted(origins - facts.keys())
        next_frontier: list[int] = []
        for chunk in _chunks(missing):
            rows = db.execute(
                select(
                    InventoryLot.id,
                    InventoryLot.source_type,
                    InventoryLot.source_ref_type,
                    InventoryLot.cost_snapshot_source,
                ).where(InventoryLot.id.in_(chunk))
            ).all()
            for lot_id, source_type, source_ref_type, cost_snapshot_source in rows:
                facts[int(lot_id)] = (
                    source_type,
                    source_ref_type,
                    cost_snapshot_source,
                )
                if source_type == "transfer":
                    next_frontier.append(int(lot_id))
        frontier = next_frontier

    memo: dict[int, bool] = {}

    def evaluate(lot_id: int, path: frozenset[int]) -> bool:
        if lot_id in path:
            return False
        if lot_id in memo:
            return memo[lot_id]
        fact = facts.get(lot_id)
        if fact is None:
            return False
        source_type, source_ref_type, cost_snapshot_source = fact
        if source_ref_type is not None or cost_snapshot_source == "purchase_receipt_actual":
            memo[lot_id] = False
            return False
        if source_type in {"manual", "stocktake"}:
            memo[lot_id] = True
            return True
        if source_type != "transfer" or not edges.get(lot_id):
            memo[lot_id] = False
            return False
        next_path = path | {lot_id}
        result = all(evaluate(source_id, next_path) for source_id in edges[lot_id])
        memo[lot_id] = result
        return result

    return {lot_id: evaluate(lot_id, frozenset()) for lot_id in initial}


def batch_is_revaluable(db, lots: Iterable[InventoryLot]) -> dict[int, bool]:
    return batch_revaluation_readiness(db, lots)
