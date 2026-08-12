from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from threading import RLock

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import OperationLog
from app.services.floor3_locations import (
    Floor3LocationError,
    PalletMergeProfile,
    load_mergeable_pallet,
    lock_pallet_inventory_lots,
    merge_pallet_remaining_goods,
)


PALLET_MERGE_BATCH_LOCK = RLock()
PALLET_MERGE_BATCH_ACTION_CODE = "warehouse.pallet_merge_batch.confirmed"


@dataclass(frozen=True)
class PalletMergeBatchSource:
    client_item_id: str
    pallet_id: int
    expected_version: int


@dataclass(frozen=True)
class PalletMergeBatchPlan:
    target_pallet_id: int
    expected_target_version: int
    profile: PalletMergeProfile
    sources: tuple[PalletMergeBatchSource, ...]


def _pallet_item_snapshot(pallet) -> tuple[tuple, ...]:
    return tuple(
        (
            int(item.id),
            int(item.pallet_id),
            int(item.inventory_lot_id) if item.inventory_lot_id is not None else None,
            int(item.customer_id) if item.customer_id is not None else None,
            int(item.product_id) if item.product_id is not None else None,
            str(item.item_type),
            str(item.unit),
            str(item.match_status),
        )
        for item in pallet.items
    )


class PalletMergeBatchError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def pallet_merge_batch_request_hash(
    *,
    batch_id: str,
    target_pallet_id: int,
    expected_target_version: int,
    sources: list[PalletMergeBatchSource],
) -> str:
    canonical = {
        "batch_id": batch_id,
        "target_pallet_id": int(target_pallet_id),
        "expected_target_version": int(expected_target_version),
        "sources": [
            {
                "client_item_id": source.client_item_id,
                "pallet_id": int(source.pallet_id),
                "expected_version": int(source.expected_version),
            }
            for source in sources
        ],
    }
    return sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def pallet_merge_batch_replay(
    db: Session,
    *,
    batch_id: str,
    request_hash: str,
    actor_user_id: int | None,
) -> dict | None:
    row = db.scalar(
        select(OperationLog)
        .where(
            OperationLog.batch_id == batch_id,
            OperationLog.action_code == PALLET_MERGE_BATCH_ACTION_CODE,
            OperationLog.result == "success",
        )
        .order_by(OperationLog.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    if row.actor_user_id_snapshot != actor_user_id:
        raise PalletMergeBatchError(
            "该合并批次标识已由其他操作员使用，不能查看或重放其结果",
            409,
        )
    try:
        details = json.loads(row.details or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PalletMergeBatchError(
            "已完成合并批次的审计记录损坏，请停止重试并联系管理员",
            409,
        ) from error
    if details.get("request_hash") != request_hash:
        raise PalletMergeBatchError(
            "同一批次标识已用于不同的栈板合并内容",
            409,
        )
    result = details.get("result")
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("sources"), list)
        or not isinstance(result.get("customer_id"), int)
        or int(result["customer_id"]) <= 0
    ):
        raise PalletMergeBatchError(
            "已完成合并批次缺少可回放结果，请停止重试并联系管理员",
            409,
        )
    return result


def preflight_pallet_merge_batch(
    db: Session,
    *,
    target_pallet_id: int,
    expected_target_version: int,
    sources: list[PalletMergeBatchSource],
) -> PalletMergeBatchPlan:
    if not 1 <= len(sources) <= 19:
        raise PalletMergeBatchError("每次合并须选择至少两块、最多二十块系统栈板", 422)

    client_ids = [source.client_item_id for source in sources]
    source_ids = [int(source.pallet_id) for source in sources]
    if len(client_ids) != len(set(client_ids)):
        raise PalletMergeBatchError("合并草稿中的来源标识不能重复", 409)
    if len(source_ids) != len(set(source_ids)):
        raise PalletMergeBatchError("同一来源栈板不能在一次合并中重复选择", 409)
    if int(target_pallet_id) in set(source_ids):
        raise PalletMergeBatchError("目标栈板不能同时作为来源栈板", 409)

    selected_ids = [int(target_pallet_id), *source_ids]
    try:
        initial_pallets = {
            pallet_id: load_mergeable_pallet(
                db,
                pallet_id,
                require_published_location=True,
            )
            for pallet_id in selected_ids
        }
        expected_pallets = {
            int(pallet.id): (int(pallet.version), int(pallet.location_id))
            for pallet, _profile in initial_pallets.values()
            if pallet.location_id is not None
        }
        expected_item_snapshots = {
            int(pallet.id): _pallet_item_snapshot(pallet)
            for pallet, _profile in initial_pallets.values()
        }
        expected_lots = {
            int(item.inventory_lot_id): (
                int(item.inventory_lot.version),
                int(item.inventory_lot.warehouse_location_id),
                str(item.inventory_lot.inventory_type),
                str(item.inventory_lot.status),
                str(item.inventory_lot.unit),
                int(item.inventory_lot.quantity_available or 0),
                int(item.inventory_lot.quantity_reserved or 0),
                int(item.inventory_lot.quantity_consumed or 0),
                int(item.inventory_lot.quantity_damaged or 0),
                int(item.inventory_lot.quantity_scrapped or 0),
                item.inventory_lot.last_movement_at,
            )
            for pallet, _profile in initial_pallets.values()
            for item in pallet.items
            if item.inventory_lot_id is not None and item.inventory_lot is not None
        }
        locked_lots = lock_pallet_inventory_lots(
            db,
            selected_ids,
            expected_pallets=expected_pallets,
            expected_lots=expected_lots,
        )
        locked_lot_ids = {int(lot.id) for lot in locked_lots}
        if locked_lot_ids != set(expected_lots):
            raise PalletMergeBatchError(
                "栈板关联批次或版本在合并预检期间发生变化，请刷新后重试",
                409,
            )
        # Reload every relationship after the row locks so status, balance and
        # reservation changes cannot slip between preflight and mutation.
        target, target_profile = load_mergeable_pallet(
            db,
            int(target_pallet_id),
            require_published_location=True,
        )
        if int(target.version) != int(expected_target_version):
            raise PalletMergeBatchError(
                "目标栈板已被其他操作更新，请刷新后重试",
                409,
            )
        if _pallet_item_snapshot(target) != expected_item_snapshots[int(target.id)]:
            raise PalletMergeBatchError(
                "目标栈板明细在合并预检期间发生变化，请刷新后重试",
                409,
            )

        ordered_sources = tuple(sources)
        for source_spec in ordered_sources:
            source, source_profile = load_mergeable_pallet(
                db,
                int(source_spec.pallet_id),
                require_published_location=True,
            )
            if int(source.version) != int(source_spec.expected_version):
                raise PalletMergeBatchError(
                    f"来源栈板 {source.pallet_code} 已被其他操作更新，请刷新后重试",
                    409,
                )
            if _pallet_item_snapshot(source) != expected_item_snapshots[int(source.id)]:
                raise PalletMergeBatchError(
                    f"来源栈板 {source.pallet_code} 明细在合并预检期间发生变化，请刷新后重试",
                    409,
                )
            if source_profile != target_profile:
                raise PalletMergeBatchError(
                    "只允许合并同一客户、库存类型、原生单位和质量状态的系统栈板",
                    409,
                )
    except Floor3LocationError as error:
        raise PalletMergeBatchError(str(error), error.status_code) from error

    return PalletMergeBatchPlan(
        target_pallet_id=int(target_pallet_id),
        expected_target_version=int(expected_target_version),
        profile=target_profile,
        sources=ordered_sources,
    )


def execute_pallet_merge_batch(
    db: Session,
    *,
    batch_id: str,
    target_pallet_id: int,
    expected_target_version: int,
    sources: list[PalletMergeBatchSource],
    operator_id: int | None,
) -> dict:
    plan = preflight_pallet_merge_batch(
        db,
        target_pallet_id=target_pallet_id,
        expected_target_version=expected_target_version,
        sources=sources,
    )
    target_version = plan.expected_target_version
    results: list[dict] = []
    for source in plan.sources:
        subkey = "p149c:" + sha256(
            (
                f"{batch_id}:{source.client_item_id}:"
                f"{source.pallet_id}:{plan.target_pallet_id}"
            ).encode("utf-8")
        ).hexdigest()
        try:
            merged = merge_pallet_remaining_goods(
                db,
                source_pallet_id=source.pallet_id,
                target_pallet_id=plan.target_pallet_id,
                expected_source_version=source.expected_version,
                expected_target_version=target_version,
                operator_id=operator_id,
                idempotency_key=subkey,
                expected_profile=plan.profile,
                require_published_locations=True,
            )
        except Floor3LocationError as error:
            raise PalletMergeBatchError(str(error), error.status_code) from error
        target_version += 1
        results.append(
            {
                "client_item_id": source.client_item_id,
                "source_pallet_id": source.pallet_id,
                "source_version_after": merged.source_pallet.version,
                "target_pallet_id": plan.target_pallet_id,
                "target_version_after": merged.target_pallet.version,
                "moved_item_count": merged.moved_item_count,
                "source_movement_id": merged.source_movement.id,
                "target_movement_id": merged.target_movement.id,
            }
        )
    return {
        "batch_id": batch_id,
        "customer_id": plan.profile.customer_id,
        "target_pallet_id": plan.target_pallet_id,
        "target_version_after": target_version,
        "sources": results,
    }
