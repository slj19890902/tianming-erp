from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Iterable

import jwt
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import load_settings
from app.core.time_contract import utc_now_naive
from app.models.customer import Customer
from app.models.mold_tool import MoldRepairEvent, MoldTool
from app.models.product import Product
from app.models.user import User

REPAIR_NORMAL = "normal"
REPAIR_NEEDED = "needs_repair"
REPAIR_STATUSES = {REPAIR_NORMAL, REPAIR_NEEDED}
CONFIRMATION_TYPE = "order_mold_repair_confirmation"
CONFIRMATION_TTL_MINUTES = 5


class MoldRepairError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class MoldRepairChange:
    mold: MoldTool
    event: MoldRepairEvent
    replayed: bool


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def change_mold_repair_status(
    db: Session,
    *,
    mold_id: int,
    target_status: str,
    expected_version: int,
    idempotency_key: str,
    actor: User,
) -> MoldRepairChange:
    target = (target_status or "").strip()
    key = (idempotency_key or "").strip()
    if target not in REPAIR_STATUSES:
        raise MoldRepairError("模具维修状态无效", status_code=400)
    if expected_version < 1 or len(key) < 8 or len(key) > 120:
        raise MoldRepairError("模具维修请求参数无效", status_code=400)
    request_hash = _sha256(
        {"mold_id": int(mold_id), "target_status": target, "expected_version": int(expected_version)}
    )
    replay = db.scalar(
        select(MoldRepairEvent).where(MoldRepairEvent.idempotency_key == key)
    )
    if replay is not None:
        if replay.actor_id != actor.id or replay.request_hash != request_hash:
            raise MoldRepairError("该幂等凭证已用于其他模具维修操作")
        mold = db.get(MoldTool, replay.mold_tool_id)
        if mold is None:
            raise MoldRepairError("模具维修流水对应档案不存在")
        return MoldRepairChange(mold=mold, event=replay, replayed=True)

    mold = db.get(MoldTool, mold_id)
    if mold is None:
        raise MoldRepairError("模具不存在", status_code=404)
    if mold.repair_status == target:
        raise MoldRepairError("模具已经是该维修状态，请刷新后核对")
    if mold.repair_version != expected_version:
        raise MoldRepairError("模具维修状态已变化，请刷新后重试")
    before = mold.repair_status
    result = db.execute(
        update(MoldTool)
        .where(
            MoldTool.id == mold_id,
            MoldTool.repair_status == before,
            MoldTool.repair_version == expected_version,
        )
        .values(
            repair_status=target,
            repair_version=expected_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise MoldRepairError("模具维修状态已变化，请刷新后重试")
    event = MoldRepairEvent(
        mold_tool_id=mold.id,
        mold_code_snapshot=mold.mold_code,
        before_status=before,
        after_status=target,
        actor_id=actor.id,
        actor_username_snapshot=actor.username,
        idempotency_key=key,
        request_hash=request_hash,
        expected_version=expected_version,
        resulting_version=expected_version + 1,
    )
    db.add(event)
    db.flush()
    db.expire(mold)
    return MoldRepairChange(mold=mold, event=event, replayed=False)


def repair_warnings_for_products(db: Session, product_ids: Iterable[int]) -> list[dict]:
    ids = sorted({int(value) for value in product_ids if int(value) > 0})
    if not ids:
        return []
    products = db.scalars(
        select(Product)
        .options(selectinload(Product.customer), selectinload(Product.mold_tool))
        .where(Product.id.in_(ids))
    ).unique().all()
    grouped: dict[int, dict] = {}
    for product in products:
        mold = product.mold_tool
        if mold is None or mold.repair_status != REPAIR_NEEDED:
            continue
        row = grouped.setdefault(
            mold.id,
            {
                "mold_id": mold.id,
                "mold_code": mold.mold_code,
                "mold_name": mold.mold_name,
                "repair_version": mold.repair_version,
                "products": [],
            },
        )
        row["products"].append(
            {
                "product_id": product.id,
                "customer_name": product.customer.name if product.customer else "",
                "product_code": product.product_code,
                "product_name": product.product_name,
            }
        )
    return [grouped[key] for key in sorted(grouped)]


def _claims(*, warnings: list[dict], user: User) -> dict:
    now = utc_now_naive().replace(tzinfo=timezone.utc)
    repairs = [
        {"mold_id": row["mold_id"], "repair_version": row["repair_version"]}
        for row in warnings
    ]
    return {
        "sub": str(user.id),
        "type": CONFIRMATION_TYPE,
        "repairs": repairs,
        "repairs_sha256": _sha256(repairs),
        "iat": now,
        "exp": now + timedelta(minutes=CONFIRMATION_TTL_MINUTES),
    }


def issue_order_mold_repair_confirmation(*, warnings: list[dict], user: User) -> str | None:
    if not warnings:
        return None
    return jwt.encode(_claims(warnings=warnings, user=user), load_settings().secret_key, algorithm="HS256")


def _confirmation_covers(token: str | None, *, warnings: list[dict], user: User) -> bool:
    if not token:
        return False
    try:
        claims = jwt.decode(
            token,
            load_settings().secret_key,
            algorithms=["HS256"],
            options={"require": ["sub", "type", "repairs", "repairs_sha256", "iat", "exp"]},
        )
    except jwt.PyJWTError:
        return False
    if claims.get("sub") != str(user.id) or claims.get("type") != CONFIRMATION_TYPE:
        return False
    repairs = claims.get("repairs")
    if not isinstance(repairs, list) or claims.get("repairs_sha256") != _sha256(repairs):
        return False
    acknowledged = {
        (int(row.get("mold_id", 0)), int(row.get("repair_version", 0)))
        for row in repairs
        if isinstance(row, dict)
    }
    current = {(int(row["mold_id"]), int(row["repair_version"])) for row in warnings}
    return current.issubset(acknowledged)


def require_order_mold_repair_confirmation(
    db: Session,
    *,
    product_ids: Iterable[int],
    confirmation_token: str | None,
    user: User,
) -> list[dict]:
    warnings = repair_warnings_for_products(db, product_ids)
    if not warnings or _confirmation_covers(confirmation_token, warnings=warnings, user=user):
        return warnings
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "MOLD_REPAIR_CONFIRMATION_REQUIRED",
            "message": "订单关联模具处于待维修状态，请确认后再保存",
            "warnings": warnings,
            "confirmation_token": issue_order_mold_repair_confirmation(warnings=warnings, user=user),
        },
    )
