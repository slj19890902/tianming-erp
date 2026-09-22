"""Application transaction boundary for newly posted paperboard receipts.

Historical rows are not backfilled or scanned on unrelated commits. The
production SessionLocal uses this session; standalone migration/repair tools
still need their separate read-only health gate and authorization.
"""
from __future__ import annotations

import re

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause


class ReceiptPriceGuardSession(Session):
    def begin_nested(self):
        # Python sqlite3 legacy transaction mode does not BEGIN for SELECT or
        # SAVEPOINT. Without a physical outer transaction, RELEASE persists a
        # batch row before before_commit can validate it, making rollback moot.
        connection = self.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
        return super().begin_nested()

    def bulk_save_objects(self, objects, *args, **kwargs):
        objects = list(objects)
        if any(inspect(row).mapper.local_table.name in (_GUARDED_TABLES | {"inventory_lots"}) for row in objects):
            _reject("实收及冻结价不能使用不跟踪事务的 bulk_save_objects")
        return super().bulk_save_objects(objects, *args, **kwargs)

    def bulk_insert_mappings(self, mapper, *args, **kwargs):
        if inspect(mapper).local_table.name in (_GUARDED_TABLES | {"inventory_lots"}):
            _reject("实收、入库及冻结价不能使用不跟踪事务的 bulk_insert_mappings")
        return super().bulk_insert_mappings(mapper, *args, **kwargs)

    def bulk_update_mappings(self, mapper, *args, **kwargs):
        if inspect(mapper).local_table.name in _GUARDED_TABLES:
            _reject("实收及冻结价不能使用不跟踪事务的 bulk_update_mappings")
        return super().bulk_update_mappings(mapper, *args, **kwargs)


_ITEM_FIELDS = (
    "status", "received_quantity", "receipt_id", "order_id", "order_item_id",
    "supplier_order_id", "supplier_order_item_id", "requisition_id",
    "requisition_item_id", "stock_replenishment_item_id",
)
_GUARDED_TABLES = {
    "incoming_receipts", "incoming_receipt_items",
    "supplier_receipt_settlement_price_facts",
}


def _reject(message: str) -> None:
    # Lazy import avoids a database/models/services import cycle.
    from app.services.incoming_receipts import IncomingReceiptError

    raise IncomingReceiptError(
        message, 409, code="SUPPLIER_RECEIPT_PRICE_COMMIT_BLOCKED"
    )


@event.listens_for(ReceiptPriceGuardSession, "do_orm_execute")
def _require_tracked_receipt_writes(state) -> None:
    """Receipt DML must go through tracked ORM writes, including admin tools."""
    statement = state.statement
    table = getattr(statement, "table", None)
    if (state.is_insert or state.is_update or state.is_delete) and (
        getattr(table, "name", None) in _GUARDED_TABLES
    ):
        _reject("实收及冻结价必须通过收料事务服务写入，禁止批量 SQL 旁路")
    if isinstance(statement, TextClause):
        sql = str(statement).lower()
        if any(re.search(r"\b" + name + r"\b", sql) for name in _GUARDED_TABLES):
            if re.search(r"\b(insert|update|delete|replace)\b", sql):
                _reject("实收及冻结价必须通过收料事务服务写入，禁止原始 SQL 旁路")


@event.listens_for(ReceiptPriceGuardSession, "before_flush")
def _protect_frozen_prices(session, _flush_context, _instances) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    for row in session.dirty | session.deleted:
        if isinstance(row, SupplierReceiptSettlementPriceFact) and (
            row in session.deleted or session.is_modified(row, include_collections=False)
        ):
            _reject("供应商冻结结算价不可修改或删除；请使用正式差额更正流程")
        if isinstance(row, IncomingReceiptItem) and any(
            inspect(row).attrs[name].history.has_changes() for name in _ITEM_FIELDS[2:]
        ):
            _reject("已保存实收的采购来源不可换绑；请撤销后按正确来源重新收料")


@event.listens_for(ReceiptPriceGuardSession, "after_flush")
def _track_receipt_changes(session, _flush_context) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

    items = session.info.setdefault("receipt_price_guard_items", set())
    headers = session.info.setdefault("receipt_price_guard_headers", set())
    for row in session.new | session.dirty:
        if isinstance(row, IncomingReceiptItem) and (
            row in session.new
            or any(inspect(row).attrs[name].history.has_changes() for name in _ITEM_FIELDS)
        ):
            items.add(row.id)
        elif isinstance(row, IncomingReceipt) and row not in session.new and any(
            inspect(row).attrs[name].history.has_changes()
            for name in ("status", "receipt_number", "received_at")
        ):
            headers.add(row.id)


@event.listens_for(ReceiptPriceGuardSession, "before_commit")
def _check_receipt_prices(session) -> None:
    if session.in_nested_transaction():
        return
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.services.supplier_receipt_price_facts import (
        SupplierReceiptPriceFactError,
        assert_receipt_settlement_price,
    )

    session.flush()
    ids = set(session.info.get("receipt_price_guard_items", ()))
    header_ids = session.info.get("receipt_price_guard_headers", ())
    if header_ids:
        ids.update(session.scalars(select(IncomingReceiptItem.id).where(
            IncomingReceiptItem.receipt_id.in_(header_ids)
        )))
    for item_id in sorted(ids):
        item = session.get(IncomingReceiptItem, item_id, populate_existing=True)
        if item is None or item.status != "posted":
            continue
        receipt = session.get(IncomingReceipt, item.receipt_id, populate_existing=True)
        if receipt is None or receipt.status != "posted":
            continue
        try:
            assert_receipt_settlement_price(session, receipt_item=item)
        except SupplierReceiptPriceFactError as error:
            _reject(f"收料 {receipt.receipt_number} 缺少匹配的冻结结算价，整笔提交已阻止：{error}")


@event.listens_for(ReceiptPriceGuardSession, "after_transaction_end")
def _clear_receipt_changes(session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop("receipt_price_guard_items", None)
        session.info.pop("receipt_price_guard_headers", None)
        session.info.pop("receipt_price_guard_savepoints", None)


@event.listens_for(ReceiptPriceGuardSession, "after_transaction_create")
def _remember_savepoint(session, transaction) -> None:
    if transaction.nested:
        session.info.setdefault("receipt_price_guard_savepoints", {})[transaction] = (
            set(session.info.get("receipt_price_guard_items", ())),
            set(session.info.get("receipt_price_guard_headers", ())),
        )


@event.listens_for(ReceiptPriceGuardSession, "after_soft_rollback")
def _restore_savepoint(session, transaction) -> None:
    snapshot = session.info.get("receipt_price_guard_savepoints", {}).pop(transaction, None)
    if snapshot is not None:
        session.info["receipt_price_guard_items"], session.info["receipt_price_guard_headers"] = snapshot
