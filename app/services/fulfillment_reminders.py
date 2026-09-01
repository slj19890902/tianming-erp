from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any, Iterator

from fastapi.encoders import jsonable_encoder
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_now_naive, beijing_today
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import ReturnReceipt
from app.models.fulfillment_reminder import (
    FulfillmentReminder,
    FulfillmentReminderMutation,
)
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.audit_log import append_audit_event


REMINDER_SCOPES = frozenset({"receipt", "customer", "product"})
REMINDER_TYPES = frozenset(
    {"replenishment", "delivery_attention", "production_attention", "other"}
)
REMINDER_CADENCES = frozenset({"one_time", "continuous"})
REMINDER_STATUSES = frozenset({"active", "resolved", "cancelled"})
MAX_REMINDER_CONTENT_CHARS = 1_000
MAX_INITIAL_REMINDERS = 20

_WRITE_LOCK = Lock()


def fulfillment_reminder_write_guard() -> Iterator[None]:
    """Serialize reminder facts and their durable replay receipts.

    The deployed ERP uses one application worker.  SQLite has no row-level
    locks, so this process lock ensures two same-key requests cannot both pass
    the replay check before either transaction commits.
    """

    _WRITE_LOCK.acquire()
    try:
        yield
    finally:
        _WRITE_LOCK.release()


@dataclass(frozen=True)
class FulfillmentReminderError(Exception):
    status_code: int
    detail: str


def actor_display_name(user: User) -> str:
    return str(user.display_name or user.real_name or user.username).strip()[:100]


def request_hash(value: Any) -> str:
    encoded = json.dumps(
        jsonable_encoder(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mutation_batch_id(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def replay_mutation(
    db: Session,
    *,
    idempotency_key: str,
    request_hash_value: str,
    action: str,
    actor: User,
) -> dict | None:
    mutation = db.scalar(
        select(FulfillmentReminderMutation).where(
            FulfillmentReminderMutation.idempotency_key == idempotency_key
        )
    )
    if mutation is None:
        return None
    if (
        mutation.action != action
        or mutation.request_hash != request_hash_value
        or mutation.actor_id != actor.id
    ):
        raise FulfillmentReminderError(
            409,
            "幂等键已被不同操作、不同内容或不同账号使用",
        )
    return json.loads(mutation.response_json)


def record_mutation(
    db: Session,
    *,
    reminder_id: int | None,
    return_receipt_id: int,
    idempotency_key: str,
    request_hash_value: str,
    action: str,
    actor: User,
    response: dict,
) -> FulfillmentReminderMutation:
    mutation = FulfillmentReminderMutation(
        reminder_id=reminder_id,
        return_receipt_id_snapshot=return_receipt_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash_value,
        action=action,
        actor_id=actor.id,
        actor_name_snapshot=actor_display_name(actor),
        response_json=json.dumps(
            jsonable_encoder(response),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    db.add(mutation)
    db.flush()
    return mutation


def _normalized_quantity(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FulfillmentReminderError(422, "建议数量必须是大于0的数字")
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise FulfillmentReminderError(422, "建议数量必须是大于0的数字") from error
    if not quantity.is_finite() or quantity <= 0:
        raise FulfillmentReminderError(422, "建议数量必须是大于0的数字")
    if quantity > Decimal("99999999999.999"):
        raise FulfillmentReminderError(422, "建议数量过大")
    return quantity.quantize(Decimal("0.001"))


def normalize_reminder_input(value: dict) -> dict:
    scope_type = str(value.get("scope_type") or "").strip()
    reminder_type = str(value.get("reminder_type") or "").strip()
    cadence = str(value.get("cadence") or "").strip()
    content = str(value.get("content") or "").strip()
    if scope_type not in REMINDER_SCOPES:
        raise FulfillmentReminderError(422, "请选择备忘范围")
    if reminder_type not in REMINDER_TYPES:
        raise FulfillmentReminderError(422, "请选择备忘类型")
    if cadence not in REMINDER_CADENCES:
        raise FulfillmentReminderError(422, "请选择一次性或持续提醒")
    if not content:
        raise FulfillmentReminderError(422, "请填写内部备忘内容")
    if len(content) > MAX_REMINDER_CONTENT_CHARS:
        raise FulfillmentReminderError(
            422,
            f"内部备忘不能超过{MAX_REMINDER_CONTENT_CHARS}个字符",
        )
    product_id = value.get("product_id")
    if scope_type == "product":
        try:
            product_id = int(product_id)
        except (TypeError, ValueError) as error:
            raise FulfillmentReminderError(422, "产品级备忘必须选择正式常用箱") from error
        if product_id <= 0:
            raise FulfillmentReminderError(422, "产品级备忘必须选择正式常用箱")
    else:
        product_id = None
    remind_on = value.get("remind_on")
    if remind_on in (None, ""):
        remind_on = None
    elif isinstance(remind_on, date):
        pass
    else:
        try:
            remind_on = date.fromisoformat(str(remind_on))
        except ValueError as error:
            raise FulfillmentReminderError(422, "提醒日期格式无效") from error
    return {
        "scope_type": scope_type,
        "reminder_type": reminder_type,
        "content": content,
        "suggested_quantity": _normalized_quantity(
            value.get("suggested_quantity")
        ),
        "cadence": cadence,
        "remind_on": remind_on,
        "product_id": product_id,
    }


def _source_context(
    db: Session,
    receipt: ReturnReceipt,
) -> tuple[Delivery, Customer]:
    delivery = db.get(Delivery, receipt.delivery_id)
    if delivery is None:
        raise FulfillmentReminderError(409, "回单关联送货单不存在")
    customer = db.get(Customer, delivery.customer_id)
    if customer is None:
        raise FulfillmentReminderError(409, "回单关联客户不存在")
    return delivery, customer


def _validated_product(
    db: Session,
    *,
    customer_id: int,
    product_id: int | None,
) -> Product | None:
    if product_id is None:
        return None
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.customer_id == customer_id,
        )
    )
    if product is None:
        # Do not reveal a cross-customer product through a direct identifier.
        raise FulfillmentReminderError(404, "常用箱不存在")
    if (
        not product.is_active
        or product.deleted_at is not None
        or product.purged_at is not None
    ):
        raise FulfillmentReminderError(409, "该常用箱已停用或进入垃圾站")
    return product


def reminder_response(reminder: FulfillmentReminder) -> dict:
    return {
        "id": reminder.id,
        "source_return_receipt_id": reminder.source_return_receipt_id_snapshot,
        "source_delivery_id": reminder.source_delivery_id_snapshot,
        "source_delivery_number": reminder.source_delivery_number_snapshot,
        "source_received_date": reminder.source_received_date_snapshot,
        "source_valid": bool(reminder.source_valid),
        "customer_id": reminder.customer_id,
        "customer_name": reminder.customer_name_snapshot,
        "scope_type": reminder.scope_type,
        "reminder_type": reminder.reminder_type,
        "content": reminder.content,
        "suggested_quantity": (
            format(reminder.suggested_quantity, ".3f")
            if reminder.suggested_quantity is not None
            else None
        ),
        "cadence": reminder.cadence,
        "remind_on": reminder.remind_on,
        "status": reminder.status,
        "version": reminder.version,
        "product_id": reminder.product_id_snapshot,
        "product_code": reminder.product_code_snapshot,
        "product_name": reminder.product_name_snapshot,
        "created_by": reminder.created_by,
        "created_by_name": reminder.created_by_name_snapshot,
        "created_at": reminder.created_at,
        "updated_at": reminder.updated_at,
        "resolved_at": reminder.resolved_at,
        "cancelled_at": reminder.cancelled_at,
    }


def _audit_reminder(
    db: Session,
    *,
    reminder: FulfillmentReminder,
    actor: User,
    action_code: str,
    idempotency_key: str,
    before_version: int | None = None,
    content_changed: bool = False,
) -> None:
    # Free text is intentionally excluded from both legacy and structured
    # audit details.  The reminder table is the only authorized text source.
    append_audit_event(
        db,
        event_category="business",
        result="success",
        source="api",
        module_code="finance",
        action_code=action_code,
        resource="FulfillmentReminder",
        legacy_action=action_code[:30],
        actor=actor,
        entity_type="fulfillment_reminder",
        entity_id=reminder.id,
        object_ref=f"fulfillment-reminder:{reminder.id}",
        customer_id=reminder.customer_id,
        customer_name=reminder.customer_name_snapshot,
        batch_id=_mutation_batch_id(idempotency_key),
        description={
            "FULFILLMENT_REMINDER_CREATED": "新增内部履约备忘",
            "FULFILLMENT_REMINDER_UPDATED": "修改内部履约备忘",
            "FULFILLMENT_REMINDER_RESOLVED": "处理内部履约备忘",
            "FULFILLMENT_REMINDER_CANCELLED": "取消内部履约备忘",
        }[action_code],
        details={
            "source_return_receipt_id": reminder.source_return_receipt_id_snapshot,
            "source_delivery_id": reminder.source_delivery_id_snapshot,
            "scope_type": reminder.scope_type,
            "reminder_type": reminder.reminder_type,
            "cadence": reminder.cadence,
            "product_id": reminder.product_id_snapshot,
            "remind_on": reminder.remind_on,
            "suggested_quantity": reminder.suggested_quantity,
            "status": reminder.status,
            "before_version": before_version,
            "after_version": reminder.version,
            "content_changed": content_changed,
        },
    )


def create_reminder(
    db: Session,
    *,
    receipt: ReturnReceipt,
    value: dict,
    actor: User,
    idempotency_key: str,
    audit: bool = True,
) -> FulfillmentReminder:
    if receipt.status != "confirmed":
        raise FulfillmentReminderError(409, "来源回单已取消，不能新增备忘")
    normalized = normalize_reminder_input(value)
    delivery, customer = _source_context(db, receipt)
    product = _validated_product(
        db,
        customer_id=customer.id,
        product_id=normalized["product_id"],
    )
    reminder = FulfillmentReminder(
        source_return_receipt_id=receipt.id,
        source_return_receipt_id_snapshot=receipt.id,
        source_delivery_id_snapshot=delivery.id,
        source_delivery_number_snapshot=delivery.delivery_number,
        source_received_date_snapshot=receipt.actual_received_date,
        source_valid=True,
        customer_id=customer.id,
        customer_name_snapshot=customer.name,
        product_id=product.id if product else None,
        product_id_snapshot=product.id if product else None,
        product_code_snapshot=product.product_code if product else None,
        product_name_snapshot=product.product_name if product else None,
        scope_type=normalized["scope_type"],
        reminder_type=normalized["reminder_type"],
        content=normalized["content"],
        suggested_quantity=normalized["suggested_quantity"],
        cadence=normalized["cadence"],
        remind_on=normalized["remind_on"],
        status="active",
        version=1,
        created_by=actor.id,
        created_by_name_snapshot=actor_display_name(actor),
    )
    db.add(reminder)
    db.flush()
    if audit:
        _audit_reminder(
            db,
            reminder=reminder,
            actor=actor,
            action_code="FULFILLMENT_REMINDER_CREATED",
            idempotency_key=idempotency_key,
        )
    return reminder


def create_reminder_with_replay(
    db: Session,
    *,
    receipt: ReturnReceipt,
    value: dict,
    actor: User,
    idempotency_key: str,
) -> tuple[dict, bool]:
    normalized_for_hash = {
        key: value.get(key)
        for key in (
            "scope_type",
            "reminder_type",
            "content",
            "suggested_quantity",
            "cadence",
            "remind_on",
            "product_id",
        )
    }
    hash_value = request_hash(
        {
            "return_receipt_id": receipt.id,
            "reminder": normalized_for_hash,
        }
    )
    replay = replay_mutation(
        db,
        idempotency_key=idempotency_key,
        request_hash_value=hash_value,
        action="create",
        actor=actor,
    )
    if replay is not None:
        return replay, True
    reminder = create_reminder(
        db,
        receipt=receipt,
        value=value,
        actor=actor,
        idempotency_key=idempotency_key,
    )
    response = reminder_response(reminder)
    record_mutation(
        db,
        reminder_id=reminder.id,
        return_receipt_id=receipt.id,
        idempotency_key=idempotency_key,
        request_hash_value=hash_value,
        action="create",
        actor=actor,
        response=response,
    )
    return response, False


def _reminder_for_customer(
    db: Session,
    *,
    reminder_id: int,
    visible_customer_ids: set[int] | None,
) -> FulfillmentReminder:
    query = select(FulfillmentReminder).where(FulfillmentReminder.id == reminder_id)
    if visible_customer_ids is not None:
        if not visible_customer_ids:
            raise FulfillmentReminderError(404, "备忘不存在")
        query = query.where(FulfillmentReminder.customer_id.in_(visible_customer_ids))
    reminder = db.scalar(query)
    if reminder is None:
        raise FulfillmentReminderError(404, "备忘不存在")
    return reminder


def update_reminder_with_replay(
    db: Session,
    *,
    reminder_id: int,
    value: dict,
    expected_version: int,
    actor: User,
    visible_customer_ids: set[int] | None,
    idempotency_key: str,
) -> tuple[dict, bool]:
    hash_value = request_hash(
        {
            "reminder_id": reminder_id,
            "expected_version": expected_version,
            "reminder": {
                key: value.get(key)
                for key in (
                    "scope_type",
                    "reminder_type",
                    "content",
                    "suggested_quantity",
                    "cadence",
                    "remind_on",
                    "product_id",
                )
            },
        }
    )
    replay = replay_mutation(
        db,
        idempotency_key=idempotency_key,
        request_hash_value=hash_value,
        action="update",
        actor=actor,
    )
    if replay is not None:
        return replay, True
    reminder = _reminder_for_customer(
        db,
        reminder_id=reminder_id,
        visible_customer_ids=visible_customer_ids,
    )
    if reminder.status != "active":
        raise FulfillmentReminderError(409, "只有有效备忘可以修改")
    normalized = normalize_reminder_input(value)
    product = _validated_product(
        db,
        customer_id=reminder.customer_id,
        product_id=normalized["product_id"],
    )
    before_version = int(reminder.version)
    content_changed = reminder.content != normalized["content"]
    now = beijing_now_naive()
    updated = db.execute(
        update(FulfillmentReminder)
        .where(
            FulfillmentReminder.id == reminder.id,
            FulfillmentReminder.version == expected_version,
            FulfillmentReminder.status == "active",
        )
        .values(
            scope_type=normalized["scope_type"],
            reminder_type=normalized["reminder_type"],
            content=normalized["content"],
            suggested_quantity=normalized["suggested_quantity"],
            cadence=normalized["cadence"],
            remind_on=normalized["remind_on"],
            product_id=product.id if product else None,
            product_id_snapshot=product.id if product else None,
            product_code_snapshot=product.product_code if product else None,
            product_name_snapshot=product.product_name if product else None,
            version=FulfillmentReminder.version + 1,
            updated_by=actor.id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        raise FulfillmentReminderError(409, "备忘版本已变化，请刷新后重试")
    db.expire(reminder)
    db.refresh(reminder)
    response = reminder_response(reminder)
    _audit_reminder(
        db,
        reminder=reminder,
        actor=actor,
        action_code="FULFILLMENT_REMINDER_UPDATED",
        idempotency_key=idempotency_key,
        before_version=before_version,
        content_changed=content_changed,
    )
    record_mutation(
        db,
        reminder_id=reminder.id,
        return_receipt_id=reminder.source_return_receipt_id_snapshot,
        idempotency_key=idempotency_key,
        request_hash_value=hash_value,
        action="update",
        actor=actor,
        response=response,
    )
    return response, False


def transition_reminder_with_replay(
    db: Session,
    *,
    reminder_id: int,
    expected_version: int,
    action: str,
    actor: User,
    visible_customer_ids: set[int] | None,
    idempotency_key: str,
) -> tuple[dict, bool]:
    if action not in {"resolve", "cancel"}:
        raise ValueError("unsupported reminder transition")
    hash_value = request_hash(
        {
            "reminder_id": reminder_id,
            "expected_version": expected_version,
            "action": action,
        }
    )
    replay = replay_mutation(
        db,
        idempotency_key=idempotency_key,
        request_hash_value=hash_value,
        action=action,
        actor=actor,
    )
    if replay is not None:
        return replay, True
    reminder = _reminder_for_customer(
        db,
        reminder_id=reminder_id,
        visible_customer_ids=visible_customer_ids,
    )
    if reminder.status != "active":
        raise FulfillmentReminderError(409, "备忘状态已变化，请刷新后重试")
    before_version = int(reminder.version)
    now = beijing_now_naive()
    values: dict[str, Any] = {
        "status": "resolved" if action == "resolve" else "cancelled",
        "version": FulfillmentReminder.version + 1,
        "updated_by": actor.id,
        "updated_at": now,
    }
    if action == "resolve":
        values.update(resolved_by=actor.id, resolved_at=now)
    else:
        values.update(cancelled_by=actor.id, cancelled_at=now)
    changed = db.execute(
        update(FulfillmentReminder)
        .where(
            FulfillmentReminder.id == reminder.id,
            FulfillmentReminder.version == expected_version,
            FulfillmentReminder.status == "active",
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise FulfillmentReminderError(409, "备忘版本已变化，请刷新后重试")
    db.expire(reminder)
    db.refresh(reminder)
    response = reminder_response(reminder)
    _audit_reminder(
        db,
        reminder=reminder,
        actor=actor,
        action_code=(
            "FULFILLMENT_REMINDER_RESOLVED"
            if action == "resolve"
            else "FULFILLMENT_REMINDER_CANCELLED"
        ),
        idempotency_key=idempotency_key,
        before_version=before_version,
    )
    record_mutation(
        db,
        reminder_id=reminder.id,
        return_receipt_id=reminder.source_return_receipt_id_snapshot,
        idempotency_key=idempotency_key,
        request_hash_value=hash_value,
        action=action,
        actor=actor,
        response=response,
    )
    return response, False


def list_receipt_reminders(
    db: Session,
    *,
    receipt_id: int,
    history: bool,
    page: int,
    page_size: int,
) -> dict:
    conditions = [
        FulfillmentReminder.source_return_receipt_id_snapshot == receipt_id,
        (
            FulfillmentReminder.status.in_({"resolved", "cancelled"})
            if history
            else FulfillmentReminder.status == "active"
        ),
    ]
    total = int(
        db.scalar(
            select(func.count(FulfillmentReminder.id)).where(*conditions)
        )
        or 0
    )
    today = beijing_today()
    due_rank = case(
        (FulfillmentReminder.remind_on <= today, 0),
        (FulfillmentReminder.remind_on.is_not(None), 1),
        else_=2,
    )
    rows = db.scalars(
        select(FulfillmentReminder)
        .where(*conditions)
        .order_by(
            due_rank,
            FulfillmentReminder.reminder_type,
            FulfillmentReminder.created_at,
            FulfillmentReminder.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [reminder_response(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def list_delivery_reminders(
    db: Session,
    *,
    customer_id: int,
    page: int,
    page_size: int,
) -> dict:
    """Project active due reminders into the delivery workbench.

    Receipt-only notes intentionally stop at the source receipt.  Delivery
    operators receive customer and exact-product reminders in one bounded
    query; matching the returned product ids to draft lines is a client-side
    concern and must never mutate the delivery draft.
    """

    today = beijing_today()
    conditions = [
        FulfillmentReminder.customer_id == customer_id,
        FulfillmentReminder.status == "active",
        FulfillmentReminder.source_valid.is_(True),
        FulfillmentReminder.scope_type.in_({"customer", "product"}),
        FulfillmentReminder.reminder_type.in_(
            {"replenishment", "delivery_attention"}
        ),
        or_(
            FulfillmentReminder.remind_on.is_(None),
            FulfillmentReminder.remind_on <= today,
        ),
    ]
    total = int(
        db.scalar(
            select(func.count(FulfillmentReminder.id)).where(*conditions)
        )
        or 0
    )
    due_rank = case(
        (FulfillmentReminder.remind_on < today, 0),
        (FulfillmentReminder.remind_on == today, 1),
        else_=2,
    )
    rows = db.scalars(
        select(FulfillmentReminder)
        .where(*conditions)
        .order_by(
            due_rank,
            FulfillmentReminder.scope_type,
            FulfillmentReminder.reminder_type,
            FulfillmentReminder.created_at,
            FulfillmentReminder.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [reminder_response(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": today,
    }


def _production_reminder_conditions(
    *,
    customer_ids: set[int],
) -> list[Any]:
    today = beijing_today()
    return [
        FulfillmentReminder.customer_id.in_(customer_ids),
        FulfillmentReminder.status == "active",
        FulfillmentReminder.source_valid.is_(True),
        FulfillmentReminder.scope_type.in_({"customer", "product"}),
        FulfillmentReminder.reminder_type == "production_attention",
        or_(
            FulfillmentReminder.remind_on.is_(None),
            FulfillmentReminder.remind_on <= today,
        ),
    ]


def _production_reminder_ordering() -> tuple[Any, ...]:
    today = beijing_today()
    due_rank = case(
        (FulfillmentReminder.remind_on < today, 0),
        (FulfillmentReminder.remind_on == today, 1),
        else_=2,
    )
    return (
        due_rank,
        FulfillmentReminder.scope_type,
        FulfillmentReminder.created_at,
        FulfillmentReminder.id,
    )


def list_order_production_reminders(
    db: Session,
    *,
    customer_id: int,
    page: int,
    page_size: int,
) -> dict:
    """Return current production reminders for one order-entry customer.

    The response intentionally includes all customer and product scoped rows
    for that customer. Order entry can then match formal product identifiers
    locally without issuing a request per draft line.
    """

    conditions = _production_reminder_conditions(customer_ids={customer_id})
    total = int(
        db.scalar(select(func.count(FulfillmentReminder.id)).where(*conditions))
        or 0
    )
    rows = db.scalars(
        select(FulfillmentReminder)
        .where(*conditions)
        .order_by(*_production_reminder_ordering())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [reminder_response(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": beijing_today(),
    }


def production_reminders_by_customer(
    db: Session,
    customer_ids: set[int] | list[int],
) -> dict[int, list[dict]]:
    """Load current production reminders for many visible customers once."""

    normalized_ids = {int(value) for value in customer_ids if value}
    if not normalized_ids:
        return {}
    rows = db.scalars(
        select(FulfillmentReminder)
        .where(*_production_reminder_conditions(customer_ids=normalized_ids))
        .order_by(
            FulfillmentReminder.customer_id,
            *_production_reminder_ordering(),
        )
    ).all()
    result: dict[int, list[dict]] = {customer_id: [] for customer_id in normalized_ids}
    for row in rows:
        result.setdefault(int(row.customer_id), []).append(reminder_response(row))
    return result


def matching_production_reminders(
    reminders: list[dict],
    *,
    product_ids: set[int] | list[int],
) -> list[dict]:
    """Keep customer reminders and exact formal-product reminders only."""

    normalized_product_ids = {int(value) for value in product_ids if value}
    return [
        row
        for row in reminders
        if row.get("scope_type") == "customer"
        or (
            row.get("scope_type") == "product"
            and int(row.get("product_id") or 0) in normalized_product_ids
        )
    ]


def annotate_production_reminders(
    db: Session,
    items: list[dict],
) -> list[dict]:
    """Append reminder projections to production task rows without N+1 reads."""

    by_customer = production_reminders_by_customer(
        db,
        {int(item.get("customer_id") or 0) for item in items},
    )
    for item in items:
        customer_id = int(item.get("customer_id") or 0)
        product_id = int(item.get("product_id") or 0)
        item["fulfillment_reminders"] = matching_production_reminders(
            by_customer.get(customer_id, []),
            product_ids={product_id} if product_id else set(),
        )
    return items


def reminder_product_options(
    db: Session,
    *,
    delivery_id: int,
    customer_id: int,
    query_text: str,
    page: int,
    page_size: int,
) -> dict:
    current_product_ids = {
        int(product_id)
        for product_id in db.scalars(
            select(func.coalesce(DeliveryItem.product_id, OrderItem.product_id))
            .select_from(DeliveryItem)
            .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.is_current.is_(True),
            )
        ).all()
        if product_id is not None
    }
    conditions = [
        Product.customer_id == customer_id,
        Product.purged_at.is_(None),
    ]
    keyword = query_text.strip()
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                Product.product_code.ilike(pattern),
                Product.customer_material_code.ilike(pattern),
                Product.product_name.ilike(pattern),
            )
        )
    total = int(
        db.scalar(select(func.count(Product.id)).where(*conditions)) or 0
    )
    products = db.scalars(
        select(Product)
        .where(*conditions)
        .order_by(Product.is_active.desc(), Product.product_code, Product.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": product.id,
                "product_code": product.product_code,
                "product_name": product.product_name,
                "customer_material_code": product.customer_material_code,
                "is_active": bool(product.is_active),
                "is_deleted": product.deleted_at is not None,
                "is_in_delivery": product.id in current_product_ids,
                "can_select": bool(
                    product.is_active
                    and product.deleted_at is None
                    and product.purged_at is None
                ),
            }
            for product in products
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def sync_receipt_source(
    db: Session,
    *,
    receipt_id: int,
    source_valid: bool,
    received_date: date,
    actor: User,
) -> int:
    now = beijing_now_naive()
    changed = db.execute(
        update(FulfillmentReminder)
        .where(
            FulfillmentReminder.source_return_receipt_id_snapshot == receipt_id,
            or_(
                FulfillmentReminder.source_valid.is_(not source_valid),
                FulfillmentReminder.source_received_date_snapshot
                != received_date,
            ),
        )
        .values(
            source_valid=source_valid,
            source_received_date_snapshot=received_date,
            version=FulfillmentReminder.version + 1,
            updated_by=actor.id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    count = int(changed.rowcount or 0)
    if count:
        append_audit_event(
            db,
            event_category="business",
            result="success",
            source="api",
            module_code="finance",
            action_code="FULFILLMENT_REMINDER_SOURCE_SYNCED",
            resource="ReturnReceipt",
            legacy_action="REMINDER_SOURCE_SYNCED",
            actor=actor,
            entity_type="return_receipt",
            entity_id=receipt_id,
            object_ref=f"return-receipt:{receipt_id}",
            description="同步回单内部履约备忘来源状态",
            details={
                "return_receipt_id": receipt_id,
                "source_valid": source_valid,
                "source_received_date": received_date,
                "affected_reminder_count": count,
            },
        )
    return count
