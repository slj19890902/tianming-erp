from __future__ import annotations

from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.customer import Customer


DELIVERY_NUMBER_MAX_LENGTH = 40
DELIVERY_NUMBER_SUFFIX_LENGTH = len("-YYYYMMDD-NNN")
DELIVERY_NUMBER_MAX_PREFIX_LENGTH = (
    DELIVERY_NUMBER_MAX_LENGTH - DELIVERY_NUMBER_SUFFIX_LENGTH
)


class DeliveryNumberingError(ValueError):
    """A delivery number cannot be allocated under the business contract."""


def _customer_prefix(customer: Customer) -> str:
    configured = str(customer.customer_code or "").strip()
    if configured:
        return configured.upper()
    if customer.id is None:
        raise DeliveryNumberingError(
            "客户缺少有效 ID，无法生成送货单号"
        )
    return f"KH{customer.id}"


def _ensure_unique_customer_prefix(
    db: Session,
    *,
    customer: Customer,
    prefix: str,
) -> None:
    for other_id, other_code in db.execute(
        select(Customer.id, Customer.customer_code)
    ).all():
        if other_id == customer.id:
            continue
        if str(other_code or "").strip().upper() == prefix:
            raise DeliveryNumberingError(
                f"客户缩写 {prefix} 与其他客户重复；"
                "请在客户资料中分别设置唯一缩写，例如 TH、THC、THX"
            )


def next_delivery_number(
    db: Session,
    *,
    customer: Customer,
    delivery_date: date,
) -> str:
    prefix = _customer_prefix(customer)
    if len(prefix) > DELIVERY_NUMBER_MAX_PREFIX_LENGTH:
        raise DeliveryNumberingError(
            "客户缩写过长，送货单号最多 40 个字符；"
            f"请将客户缩写控制在 {DELIVERY_NUMBER_MAX_PREFIX_LENGTH} 个字符以内"
        )
    _ensure_unique_customer_prefix(
        db,
        customer=customer,
        prefix=prefix,
    )

    sequence = db.execute(
        text(
            """
            INSERT INTO delivery_daily_sequences (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": delivery_date.isoformat()},
    ).scalar_one()
    if sequence > 999:
        raise DeliveryNumberingError("当日送货单流水号已超过 999")

    delivery_number = f"{prefix}-{delivery_date:%Y%m%d}-{sequence:03d}"
    if len(delivery_number) > DELIVERY_NUMBER_MAX_LENGTH:
        raise DeliveryNumberingError(
            "生成的送货单号超过 40 个字符，请缩短客户缩写"
        )
    return delivery_number
