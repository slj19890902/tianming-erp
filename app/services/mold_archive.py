from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time_contract import utc_now_naive
from app.models.mold_tool import MoldLocationMovement, MoldTool
from app.models.product import Product
from app.services.mold_location import (
    MOLD_ARCHIVE_AREA_CODE,
    MoldLocationError,
    describe_mold_location,
    normalize_mold_location_code,
)


MOLD_ARCHIVE_REASONS = frozenset({"unbound", "all_products_inactive"})


@dataclass(frozen=True)
class MoldArchiveResult:
    mold: MoldTool
    movement: MoldLocationMovement
    replayed: bool
    action: str


def mold_archive_candidate(row: MoldTool) -> dict:
    products = list(row.products or [])
    active_products = [
        product
        for product in products
        if product.deleted_at is None and product.is_active
    ]
    reason = None
    if row.archive_status == "active" and row.is_active and not active_products:
        reason = "unbound" if not products else "all_products_inactive"
    return {
        "eligible": reason is not None,
        "reason": reason,
        "active_product_count": len(active_products),
        "historical_product_count": len(products),
    }


def _movement_by_key(db: Session, key: str) -> MoldLocationMovement | None:
    return db.scalar(
        select(MoldLocationMovement).where(
            MoldLocationMovement.idempotency_key == key
        )
    )


def _validated_key(value: str) -> str:
    key = (value or "").strip()
    if len(key) < 8:
        raise MoldLocationError("幂等键去除首尾空白后至少需要 8 个字符", status_code=422)
    return key


def _idempotent_result(
    db: Session,
    movement: MoldLocationMovement,
    *,
    mold_id: int,
    action: str,
    target_location: str,
    expected_version: int,
    reason: str,
) -> MoldArchiveResult:
    if (
        movement.mold_tool_id != mold_id
        or movement.source != action
        or movement.to_location != target_location
        or movement.expected_version != expected_version
        or (movement.note or "").strip() != reason
    ):
        raise MoldLocationError("幂等键已用于不同的模具封存业务", status_code=409)
    mold = db.get(MoldTool, mold_id)
    if mold is None:
        raise MoldLocationError("幂等事实对应的模具不存在", status_code=409)
    return MoldArchiveResult(mold=mold, movement=movement, replayed=True, action=action)


def _insert_movement(
    db: Session,
    *,
    mold: MoldTool,
    actor_id: int,
    moved_at,
    from_location: str,
    to_location: str,
    expected_version: int,
    key: str,
    action: str,
    reason: str,
) -> MoldLocationMovement:
    movement = MoldLocationMovement(
        mold_tool_id=mold.id,
        mold_code_snapshot=mold.mold_code,
        from_location=from_location,
        to_location=to_location,
        actor_id=actor_id,
        moved_at=moved_at,
        idempotency_key=key,
        expected_version=expected_version,
        resulting_version=expected_version + 1,
        source=action,
        note=reason,
    )
    db.add(movement)
    db.flush()
    return movement


def archive_mold_tool(
    db: Session,
    *,
    mold_id: int,
    expected_version: int,
    idempotency_key: str,
    actor_id: int,
    reason: str,
) -> MoldArchiveResult:
    key = _validated_key(idempotency_key)
    clean_reason = (reason or "").strip()
    if clean_reason not in MOLD_ARCHIVE_REASONS:
        raise MoldLocationError("未知的模具封存原因", status_code=422)
    existing = _movement_by_key(db, key)
    if existing is not None:
        return _idempotent_result(
            db,
            existing,
            mold_id=mold_id,
            action="archive",
            target_location=MOLD_ARCHIVE_AREA_CODE,
            expected_version=expected_version,
            reason=clean_reason,
        )
    mold = db.get(MoldTool, mold_id)
    if mold is None:
        raise MoldLocationError("模具不存在", status_code=404)
    if mold.archive_status != "active" or not mold.is_active:
        raise MoldLocationError("当前模具不是可封存的启用状态", status_code=409)
    candidate = mold_archive_candidate(mold)
    if not candidate["eligible"]:
        raise MoldLocationError("该模具仍绑定启用中的常用箱，不能封存", status_code=409)
    if candidate["reason"] != clean_reason:
        raise MoldLocationError("封存原因与当前绑定事实不一致，请刷新后重试", status_code=409)
    if mold.location_version != expected_version:
        raise MoldLocationError(
            f"模具位置版本已变化（当前版本 {mold.location_version}），请刷新后重试",
            status_code=409,
        )
    archived_at = utc_now_naive()
    from_location = mold.rack_location
    active_product_exists = select(Product.id).where(
        Product.mold_tool_id == mold.id,
        Product.deleted_at.is_(None),
        Product.is_active.is_(True),
    ).exists()
    any_product_exists = select(Product.id).where(
        Product.mold_tool_id == mold.id
    ).exists()
    candidate_state_predicate = (
        ~any_product_exists if clean_reason == "unbound" else any_product_exists
    )
    try:
        with db.begin_nested():
            claimed = db.execute(
                update(MoldTool)
                .where(
                    MoldTool.id == mold.id,
                    MoldTool.archive_status == "active",
                    MoldTool.is_active.is_(True),
                    MoldTool.location_version == expected_version,
                    ~active_product_exists,
                    candidate_state_predicate,
                )
                .values(
                    rack_location=MOLD_ARCHIVE_AREA_CODE,
                    location_version=expected_version + 1,
                    last_location_confirmed_at=archived_at,
                    last_location_confirmed_by=actor_id,
                    is_active=False,
                    archive_status="archived",
                    archived_at=archived_at,
                    archived_by=actor_id,
                    archive_reason=clean_reason,
                    pre_archive_location=from_location,
                    restored_at=None,
                    restored_by=None,
                    updated_by=actor_id,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise MoldLocationError("模具状态或位置版本已变化，请刷新后重试", status_code=409)
            movement = _insert_movement(
                db,
                mold=mold,
                actor_id=actor_id,
                moved_at=archived_at,
                from_location=from_location,
                to_location=MOLD_ARCHIVE_AREA_CODE,
                expected_version=expected_version,
                key=key,
                action="archive",
                reason=clean_reason,
            )
    except IntegrityError:
        existing = _movement_by_key(db, key)
        if existing is not None:
            return _idempotent_result(
                db,
                existing,
                mold_id=mold_id,
                action="archive",
                target_location=MOLD_ARCHIVE_AREA_CODE,
                expected_version=expected_version,
                reason=clean_reason,
            )
        raise
    db.expire(mold)
    db.refresh(mold)
    return MoldArchiveResult(mold=mold, movement=movement, replayed=False, action="archive")


def restore_mold_tool(
    db: Session,
    *,
    mold_id: int,
    target_location: str,
    expected_version: int,
    idempotency_key: str,
    actor_id: int,
) -> MoldArchiveResult:
    key = _validated_key(idempotency_key)
    target = normalize_mold_location_code(target_location)
    guide = describe_mold_location(target)
    if guide.get("floor") != "1F" and guide.get("kind") != "storage_cell":
        raise MoldLocationError("封存模具只能恢复到已发布的正式模具格或原一楼位置", status_code=422)
    existing = _movement_by_key(db, key)
    if existing is not None:
        return _idempotent_result(
            db,
            existing,
            mold_id=mold_id,
            action="restore",
            target_location=target,
            expected_version=expected_version,
            reason="restore_to_production",
        )
    mold = db.get(MoldTool, mold_id)
    if mold is None:
        raise MoldLocationError("模具不存在", status_code=404)
    if mold.archive_status != "archived" or mold.is_active:
        raise MoldLocationError("当前模具不在封存待复用状态", status_code=409)
    if mold.location_version != expected_version:
        raise MoldLocationError(
            f"模具位置版本已变化（当前版本 {mold.location_version}），请刷新后重试",
            status_code=409,
        )
    restored_at = utc_now_naive()
    from_location = mold.rack_location
    try:
        with db.begin_nested():
            claimed = db.execute(
                update(MoldTool)
                .where(
                    MoldTool.id == mold.id,
                    MoldTool.archive_status == "archived",
                    MoldTool.is_active.is_(False),
                    MoldTool.location_version == expected_version,
                )
                .values(
                    rack_location=target,
                    location_version=expected_version + 1,
                    last_location_confirmed_at=restored_at,
                    last_location_confirmed_by=actor_id,
                    is_active=True,
                    archive_status="active",
                    archived_at=None,
                    archived_by=None,
                    archive_reason=None,
                    pre_archive_location=None,
                    restored_at=restored_at,
                    restored_by=actor_id,
                    updated_by=actor_id,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise MoldLocationError("模具状态或位置版本已变化，请刷新后重试", status_code=409)
            movement = _insert_movement(
                db,
                mold=mold,
                actor_id=actor_id,
                moved_at=restored_at,
                from_location=from_location,
                to_location=target,
                expected_version=expected_version,
                key=key,
                action="restore",
                reason="restore_to_production",
            )
    except IntegrityError:
        existing = _movement_by_key(db, key)
        if existing is not None:
            return _idempotent_result(
                db,
                existing,
                mold_id=mold_id,
                action="restore",
                target_location=target,
                expected_version=expected_version,
                reason="restore_to_production",
            )
        raise
    db.expire(mold)
    db.refresh(mold)
    return MoldArchiveResult(mold=mold, movement=movement, replayed=False, action="restore")
