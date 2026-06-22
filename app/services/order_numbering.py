from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.order import OrderItemNumberSequence


def format_main_order_number(order_date: date, sequence: int) -> str:
    return f"TM{order_date:%Y%m%d}{sequence:03d}"


def format_item_order_number(order_number: str, item_sequence: int) -> str:
    return f"{order_number}-{item_sequence:03d}"


def preview_next_order_number(db: Session, order_date: date) -> str:
    sequence = db.execute(
        text(
            """
            SELECT COALESCE(last_value, 0) + 1
            FROM order_daily_sequences
            WHERE sequence_date = :sequence_date
            """
        ),
        {"sequence_date": order_date.isoformat()},
    ).scalar_one_or_none()
    next_sequence = int(sequence or 1)
    if next_sequence > 999:
        raise HTTPException(status_code=409, detail="当日订单流水号已超过999")
    return format_main_order_number(order_date, next_sequence)


def reserve_next_order_number(db: Session, order_date: date) -> str:
    sequence = db.execute(
        text(
            """
            INSERT INTO order_daily_sequences (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": order_date.isoformat()},
    ).scalar_one()
    if sequence > 999:
        raise HTTPException(status_code=409, detail="当日订单流水号已超过999")
    return format_main_order_number(order_date, int(sequence))


def reserve_next_item_sequence(db: Session, order_id: int) -> int:
    sequence = db.execute(
        text(
            """
            INSERT INTO order_item_number_sequences (order_id, last_item_sequence, updated_at)
            VALUES (:order_id, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(order_id)
            DO UPDATE SET
                last_item_sequence = last_item_sequence + 1,
                updated_at = CURRENT_TIMESTAMP
            RETURNING last_item_sequence
            """
        ),
        {"order_id": order_id},
    ).scalar_one()
    return int(sequence)


def peek_next_item_sequence(db: Session, order_id: int) -> int:
    row = db.get(OrderItemNumberSequence, order_id)
    return 1 if row is None else int(row.last_item_sequence) + 1
