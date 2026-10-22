"""Bounded, audited physical-date correction for an already invoiced delivery.

Only the two fact tables and the success audit are written. The caller owns
commit; every failure after acquiring our SQLite write transaction rolls it
back. A dirty session or an existing physical transaction is never adopted.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import func, inspect, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, object_session

from app.api.deps import customer_scope_ids, has_permission, has_unrestricted_customer_access
from app.core.time_contract import beijing_today
from app.models import Base
from app.models.delivery_fulfillment_fact import FULFILLMENT_GUARD_NAMES, DeliveryFulfillmentRevision, DeliveryFulfillmentSegment
from app.services.audit_log import append_audit_event
from app.services.delivery_quantities import decode as decode_quantity_contract

PositiveInt = Annotated[StrictInt, Field(gt=0, le=2147483647)]


class FulfillmentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_file: str = Field(min_length=1, max_length=300)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sheet: str = Field(min_length=1, max_length=200)
    rows: list[PositiveInt] = Field(min_length=1, max_length=100)
    confirmation: str = Field(min_length=1, max_length=2000)
    quantity_basis_confirmed: bool = False
    customer_unit: str | None = Field(default=None, min_length=1, max_length=20)
    physical_unit: str | None = Field(default=None, min_length=1, max_length=20)


class FulfillmentSegmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_item_id: PositiveInt
    actual_date: date
    quantity: PositiveInt
    evidence: FulfillmentEvidence

    @field_validator("actual_date", mode="before")
    @classmethod
    def exact_date(cls, value):
        if isinstance(value, datetime) or not isinstance(value, (str, date)):
            raise ValueError("实际日期必须为 YYYY-MM-DD")
        if isinstance(value, str) and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
            raise ValueError("实际日期必须为 YYYY-MM-DD")
        return value


class FulfillmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: PositiveInt
    segments: list[FulfillmentSegmentInput] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: FulfillmentEvidence


class FulfillmentSubmitRequest(FulfillmentRequest):
    operation_key: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    reviewed_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: Annotated[StrictInt, Field(ge=0)]


class FulfillmentError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def _fail(code, message, status=409):
    raise FulfillmentError(code, message, status)


def _json(value):
    def encode(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _rows(db, name, *conditions):
    table = Base.metadata.tables[name]
    statement = select(table).where(*conditions)
    if table.primary_key.columns:
        statement = statement.order_by(*table.primary_key.columns)
    return [dict(row) for row in db.execute(statement).mappings()]


def _table(name):
    return Base.metadata.tables[name]


def _authorize(db, actor, customer_id):
    # Caller already holds no_autoflush, including expired/lazy actor fields.
    if not actor.is_active:
        _fail("fulfillment_permission_denied", "账号不可用", 403)
    if any(not has_permission(actor, permission) for permission in (
        "deliveries.execute", "finance.return_receipt.period.adjust", "finance.view"
    )):
        _fail("fulfillment_permission_denied", "需要送货操作、对账月份调整和财务查看权限", 403)
    if not has_unrestricted_customer_access(actor, db) and customer_id not in customer_scope_ids(actor, db):
        _fail("fulfillment_customer_denied", "无客户访问权限", 403)


def _session_actor(db, actor):
    if actor is None or object_session(actor) is not db or not inspect(actor).persistent:
        _fail("fulfillment_permission_denied", "必须使用当前会话中的登录用户", 403)


def _base_request(payload):
    value = payload.model_dump(mode="json", exclude={"operation_key", "reviewed_hash", "expected_revision"})
    value["segments"].sort(key=lambda row: (row["delivery_item_id"], row["actual_date"]))
    return value


def _schema_ready(db):
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        _fail("fulfillment_unsupported_database", "候选仅支持 SQLite", 503)
    if not connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
        _fail("fulfillment_foreign_keys_required", "数据库外键保护未启用", 503)
    guards = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'delivery_fulfillment_%'").scalars())
    if not FULFILLMENT_GUARD_NAMES <= guards:
        _fail("fulfillment_schema_not_ready", "交付事实迁移保护尚未安装", 503)
    return connection


def _snapshot(db, delivery_id, customer_id):
    delivery = _rows(db, "sales_deliveries", _table("sales_deliveries").c.id == delivery_id)
    if not delivery or delivery[0]["customer_id"] != customer_id:
        _fail("fulfillment_customer_denied", "送货单不属于当前客户", 403)
    result = {"sales_deliveries": delivery}
    items = _rows(db, "sales_delivery_items", _table("sales_delivery_items").c.delivery_id == delivery_id)
    current = [row for row in items if row["is_current"]]
    result["sales_delivery_items"] = items
    item_ids = [row["id"] for row in items]
    order_item_ids = [row["order_item_id"] for row in current if row["order_item_id"]]
    order_items = _rows(db, "sales_order_items", _table("sales_order_items").c.id.in_(order_item_ids))
    orders = _rows(db, "sales_orders", _table("sales_orders").c.id.in_([row["order_id"] for row in order_items]))
    if any(row["customer_id"] != customer_id for row in orders):
        _fail("fulfillment_customer_denied", "来源订单客户不一致", 403)
    result.update(sales_orders=orders, sales_order_items=order_items)
    receipts = _rows(db, "finance_return_receipts", _table("finance_return_receipts").c.delivery_id == delivery_id)
    receipt_items = _rows(db, "finance_return_receipt_items", or_(
        _table("finance_return_receipt_items").c.delivery_item_id.in_(item_ids),
        _table("finance_return_receipt_items").c.return_receipt_id.in_([r["id"] for r in receipts])))
    statement_items = _rows(db, "finance_statement_items", _table("finance_statement_items").c.return_receipt_item_id.in_([r["id"] for r in receipt_items]))
    statement_ids = sorted({r["statement_id"] for r in statement_items})
    statements = _rows(db, "finance_statements", _table("finance_statements").c.id.in_(statement_ids))
    if any(row["customer_id"] != customer_id for row in statements):
        _fail("fulfillment_customer_denied", "来源对账客户不一致", 403)
    result.update(finance_return_receipts=receipts, finance_return_receipt_items=receipt_items,
                  finance_statements=statements)
    # Freeze the entire linked statement, including its other lines and counters.
    statement_items = _rows(db, "finance_statement_items", _table("finance_statement_items").c.statement_id.in_(statement_ids))
    if any(r["source_customer_id"] not in (None, customer_id) for r in statement_items):
        _fail("fulfillment_customer_denied", "候选不支持跨客户合并对账", 403)
    all_receipt_ids = [r["return_receipt_item_id"] for r in statement_items if r["return_receipt_item_id"]]
    receipt_lines = _table("finance_return_receipt_items")
    delivery_lines = _table("sales_delivery_items")
    deliveries = _table("sales_deliveries")
    owners = list(db.execute(select(receipt_lines.c.id, deliveries.c.customer_id).select_from(
        receipt_lines.join(delivery_lines, delivery_lines.c.id == receipt_lines.c.delivery_item_id)
        .join(deliveries, deliveries.c.id == delivery_lines.c.delivery_id)
    ).where(receipt_lines.c.id.in_(all_receipt_ids))))
    charge_ids = [r["customer_charge_id"] for r in statement_items if r["customer_charge_id"]]
    charges = _rows(db, "finance_customer_charges", _table("finance_customer_charges").c.id.in_(charge_ids))
    if len(owners) != len(all_receipt_ids) or len(charges) != len(charge_ids) or any(r.customer_id != customer_id for r in owners) or any(r["customer_id"] != customer_id for r in charges):
        _fail("fulfillment_customer_denied", "对账来源实际归属客户不一致", 403)
    result["finance_statement_items"] = statement_items
    result["finance_customer_charges"] = charges
    for name in ("finance_invoices", "finance_settlement_records", "finance_statement_adjustments", "finance_invoice_tasks", "finance_invoice_task_statements"):
        result[name] = _rows(db, name, _table(name).c.statement_id.in_(statement_ids))
    task_ids = {r["id"] for r in result["finance_invoice_tasks"]} | {r["task_id"] for r in result["finance_invoice_task_statements"]}
    if task_ids:
        result["finance_invoice_tasks"] = _rows(db, "finance_invoice_tasks", _table("finance_invoice_tasks").c.id.in_(task_ids))
        shares = _rows(db, "finance_invoice_task_statements", _table("finance_invoice_task_statements").c.task_id.in_(task_ids))
        if any(r["statement_id"] not in statement_ids for r in shares):
            _fail("fulfillment_unsupported_chain", "候选不支持跨账单合并开票")
        result["finance_invoice_task_statements"] = shares
        result["finance_invoice_task_items"] = _rows(db, "finance_invoice_task_items", _table("finance_invoice_task_items").c.task_id.in_(task_ids))
    # Capture directly linked stock/cost/BOM facts and the referenced source rows.
    # This includes newly added allocations (phantoms), not just known IDs.
    for name, table in sorted(Base.metadata.tables.items()):
        if name in result or name.startswith("delivery_fulfillment_"):
            continue
        predicates = []
        for key, values in (("delivery_id", [delivery_id]), ("related_delivery_id", [delivery_id]),
                            ("delivery_item_id", item_ids), ("order_item_id", order_item_ids),
                            ("sales_order_item_id", order_item_ids)):
            if key in table.c:
                predicates.append(table.c[key].in_(values))
        if predicates:
            result[name] = _rows(db, name, or_(*predicates))
    # Follow stock/cost provenance foreign keys, but never users/authentication.
    follow = {"inventory_reservations", "inventory_movements", "inventory_lots",
              "production_completions", "incoming_receipts", "incoming_receipt_items",
              "purchase_receipt_facts", "incoming_receipt_purpose_allocations", "purchase_purpose_source_snapshots",
              "supplier_requisition_order_items", "material_requisition_items"}
    changed = True
    while changed:
        changed = False
        for name, rows in list(result.items()):
            table = _table(name)
            for fk in table.foreign_keys:
                target = fk.column.table.name
                if target not in follow or fk.column.name != "id":
                    continue
                known = {r["id"] for r in result.get(target, [])}
                wanted = {r[fk.parent.name] for r in rows if r.get(fk.parent.name) is not None} - known
                if wanted:
                    fetched = _rows(db, target, _table(target).c.id.in_(wanted))
                    if len(fetched) != len(wanted):
                        _fail("fulfillment_incomplete_source", "库存或成本来源关联缺失")
                    result.setdefault(target, []).extend(fetched)
                    result[target].sort(key=lambda r: r["id"])
                    changed = True
    # Freeze reverse links used by stock and price projections as well.
    lot_ids = [r["id"] for r in result.get("inventory_lots", [])]
    for name in ("finished_goods_inventory_details", "semi_finished_inventory_details"):
        if name in Base.metadata.tables:
            result[name] = _rows(db, name, _table(name).c.inventory_lot_id.in_(lot_ids))
    for rows in result.values():
        for row in rows:
            if any(row.get(key) not in (None, customer_id) for key in ("customer_id", "source_customer_id", "owner_customer_id")):
                _fail("fulfillment_customer_denied", "库存、成本或票据来源客户不一致", 403)
    # Audit records are needed to detect a settled-then-reversed chain.
    logs = _table("operation_logs")
    result["chain_reversal_audits"] = [dict(r) for r in db.execute(select(logs).where(
        logs.c.entity_type == "statement", logs.c.entity_id.in_(statement_ids),
        logs.c.action_code.in_(["finance.reverse_settlement", "finance.reverse_invoice"])
    ).order_by(logs.c.id)).mappings()]
    revisions = _rows(db, "delivery_fulfillment_revisions", _table("delivery_fulfillment_revisions").c.delivery_id == delivery_id)
    # Refer to immutable earlier snapshots by digest instead of recursively
    # embedding them again in every subsequent revision.
    for revision in revisions:
        revision["before_json_sha256"] = hashlib.sha256(revision.pop("before_json").encode()).hexdigest()
    result["delivery_fulfillment_revisions"] = revisions
    result["delivery_fulfillment_segments"] = _rows(db, "delivery_fulfillment_segments", _table("delivery_fulfillment_segments").c.revision_id.in_([r["id"] for r in revisions]))
    return result


def _validate(before, payload):
    delivery = before["sales_deliveries"][0]
    items = [row for row in before["sales_delivery_items"] if row["is_current"]]
    if delivery["status"] != "dispatched" or delivery["source_mode"] != "order" or not items:
        _fail("fulfillment_unsupported_source", "只支持已发货普通订单送货")
    if any(row["source_type"] != "order" for row in items) or sum(row["delivered_quantity"] for row in items) != delivery["total_quantity"]:
        _fail("fulfillment_quantity_mismatch", "原单数量或来源不一致")
    order_items = {r["id"]: r for r in before["sales_order_items"]}
    orders = {r["id"]: r for r in before["sales_orders"]}
    for item in items:
        oi = order_items.get(item["order_item_id"])
        if not oi or oi["supply_mode_snapshot"] != "corrugated_production" or oi["combination_role"] != "standalone":
            _fail("fulfillment_unsupported_source", "候选只支持销售与实物一比一的普通纸箱")
        try:
            contract = decode_quantity_contract(item["quantity_contract_json"])
        except ValueError:
            _fail("fulfillment_quantity_mismatch", "原单数量快照损坏")
        if contract and (contract["customer_id"] != payload.customer_id or contract["product_id"] != oi["product_id"]
                         or contract["customer_basis"] != contract["physical_basis"]
                         or contract["customer_unit"] != contract["physical_unit"]
                         or contract["customer_quantity"] != item["delivered_quantity"]
                         or contract["physical_quantity"] != item["delivered_quantity"]):
            _fail("fulfillment_unsupported_source", "原单客户、产品或实物数量换算不匹配")
        if contract is None:
            proof = payload.evidence
            if (not proof.quantity_basis_confirmed or not proof.customer_unit
                    or proof.customer_unit != proof.physical_unit
                    or (oi["sales_unit_snapshot"] and oi["sales_unit_snapshot"] != proof.customer_unit)
                    or (item["unit_snapshot"] and item["unit_snapshot"] != proof.customer_unit)):
                _fail("fulfillment_quantity_basis_unproven", "原单缺少数量快照，须核对来源并确认销售与实物同单位、一比一")
    for name, rows in before.items():
        if rows and ("bom" in name or "subkit" in name or name in ("ordered_finished_receipt_returns", "unordered_finished_delivery_reversals")):
            _fail("fulfillment_unsupported_source", "候选不支持组合件或退货来源")
        if rows and name == "delivery_inventory_allocations" and any(r["status"] != "active" or r["reversed_stock_quantity"] or r["reversed_requirement_quantity"] for r in rows):
            _fail("fulfillment_unsupported_source", "库存来源存在冲销，需另行核对")
        if rows and name == "inventory_movements" and any(r.get("reversal_of_movement_id") or str(r.get("idempotency_key") or "").startswith("historical-dual-quantity:") for r in rows):
            _fail("fulfillment_unsupported_source", "库存来源存在冲销或双单位更正")
    receipts, statements = before["finance_return_receipts"], before["finance_statements"]
    if len(receipts) != 1 or receipts[0]["status"] != "confirmed" or len(statements) != 1 or statements[0]["confirmation_status"] != "confirmed":
        _fail("fulfillment_unsupported_chain", "需要唯一已确认回单和已确认对账单")
    statement = statements[0]
    invoices = before["finance_invoices"]
    ar = Decimal(statement["total_receivable_exact"] if statement["total_receivable_exact"] is not None else statement["total_receivable"])
    line_total = sum((Decimal(r["receivable_amount_exact"] if r["receivable_amount_exact"] is not None else r["receivable_amount"]) for r in before["finance_statement_items"]), Decimal(0))
    if line_total != ar:
        _fail("fulfillment_unsupported_chain", "对账明细与应收合计不一致")
    invoice_sum = sum((r["invoice_amount"] for r in invoices if r["invoice_status"] == "issued"), Decimal(0))
    if ar <= 0 or not invoices or any(r["invoice_status"] != "issued" for r in invoices) or invoice_sum != ar or invoice_sum != statement["invoiced_amount"]:
        _fail("fulfillment_not_fully_invoiced", "对账必须全量开票且开票流水与金额一致")
    if statement["settled_amount"] or before["finance_settlement_records"] or before["chain_reversal_audits"]:
        _fail("fulfillment_unsupported_chain", "候选不支持存在收款或撤销收款记录的票据链")
    ri = {r["delivery_item_id"]: r for r in before["finance_return_receipt_items"]}
    si = {r["return_receipt_item_id"]: r for r in before["finance_statement_items"] if r["return_receipt_item_id"]}
    if set(ri) != {r["id"] for r in items} or any(r["return_receipt_id"] != receipts[0]["id"] for r in ri.values()):
        _fail("fulfillment_quantity_mismatch", "回单未完整对应当前送货明细")
    totals, seen = {}, set()
    for segment in payload.segments:
        identity = (segment.delivery_item_id, segment.actual_date)
        if identity in seen or segment.delivery_item_id not in ri:
            _fail("fulfillment_quantity_mismatch", "分批包含重复日期或非原单明细")
        seen.add(identity)
        item = next(r for r in items if r["id"] == segment.delivery_item_id)
        order = orders[order_items[item["order_item_id"]]["order_id"]]
        if segment.actual_date < order["order_date"] or segment.actual_date > beijing_today():
            _fail("fulfillment_invalid_date", "实际日期须介于订单日期和今天之间")
        totals[segment.delivery_item_id] = totals.get(segment.delivery_item_id, 0) + segment.quantity
    for item in items:
        receipt_item = ri[item["id"]]
        statement_item = si.get(receipt_item["id"])
        if (totals.get(item["id"]) != item["delivered_quantity"] or receipt_item["actual_received_quantity"] != item["delivered_quantity"]
                or not statement_item or statement_item["actual_received_quantity"] != item["delivered_quantity"]):
            _fail("fulfillment_quantity_mismatch", "逐行分批合计须等于原送货、回单及对账数量")


def preview_delivery_fulfillment(db: Session, actor, delivery_id: int, payload: FulfillmentRequest) -> dict[str, Any]:
    _session_actor(db, actor)
    with db.no_autoflush:
        _authorize(db, actor, payload.customer_id)
        _schema_ready(db)
        before = _snapshot(db, delivery_id, payload.customer_id)
        _validate(before, payload)
        revision = max((r["revision"] for r in before["delivery_fulfillment_revisions"]), default=0)
        return dict(can_submit=True, delivery_id=delivery_id, customer_id=payload.customer_id,
                    before=json.loads(_json(before)), expected_revision=revision,
                    reviewed_hash=_hash(dict(before=before, request=_base_request(payload), delivery_id=delivery_id)),
                    segments=_base_request(payload)["segments"], cost_allocation_status="pending")


def _response(db, row, replayed):
    segments = _rows(db, "delivery_fulfillment_segments", _table("delivery_fulfillment_segments").c.revision_id == row["id"])
    return dict(revision_id=row["id"], revision=row["revision"], delivery_id=row["delivery_id"],
                customer_id=row["customer_id"], replayed=replayed, cost_allocation_status="pending",
                segments=[dict(id=r["id"], delivery_item_id=r["delivery_item_id"], actual_date=r["actual_date"].isoformat(), quantity=r["quantity"]) for r in segments])


def append_delivery_fulfillment(db: Session, actor, delivery_id: int, payload: FulfillmentSubmitRequest) -> dict[str, Any]:
    _session_actor(db, actor)
    if db.new or db.dirty or db.deleted:
        _fail("fulfillment_session_dirty", "提交必须使用没有其他待保存变更的会话")
    with db.no_autoflush:
        _authorize(db, actor, payload.customer_id)
        connection = _schema_ready(db)
        if connection.connection.driver_connection.in_transaction:
            _fail("fulfillment_transaction_busy", "提交需要独立的外层事务")
        try:
            # A real outer transaction is essential: releasing a top-level
            # SQLite SAVEPOINT would otherwise commit before the caller does.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            db.expire_all()
            _authorize(db, actor, payload.customer_id)
            request_hash = _hash(dict(delivery_id=delivery_id, payload=payload.model_dump(mode="json")))
            existing = _rows(db, "delivery_fulfillment_revisions", _table("delivery_fulfillment_revisions").c.operation_key == payload.operation_key)
            if existing:
                row = existing[0]
                if row["actor_id"] != actor.id or row["request_hash"] != request_hash or row["customer_id"] != payload.customer_id:
                    _fail("fulfillment_idempotency_conflict", "操作编号已用于其他操作者或请求")
                return _response(db, row, True)
            preview = preview_delivery_fulfillment(db, actor, delivery_id, payload)
            if preview["expected_revision"] != payload.expected_revision or preview["reviewed_hash"] != payload.reviewed_hash:
                _fail("fulfillment_stale_preview", "来源或分批已变化，请重新预览确认")
            previous = preview["before"]["delivery_fulfillment_revisions"]
            revision_id = (db.scalar(select(func.max(DeliveryFulfillmentRevision.id))) or 0) + 1
            # Deferred FK segments first; the final header seals the revision.
            db.execute(DeliveryFulfillmentSegment.__table__.insert(), [dict(
                revision_id=revision_id, delivery_item_id=s.delivery_item_id,
                actual_date=s.actual_date, quantity=s.quantity, evidence_json=_json(s.evidence.model_dump()))
                for s in sorted(payload.segments, key=lambda s: (s.delivery_item_id, s.actual_date))])
            header = dict(id=revision_id, delivery_id=delivery_id, customer_id=payload.customer_id,
                          revision=payload.expected_revision + 1,
                          previous_revision_id=max(previous, key=lambda r: r["revision"])["id"] if previous else None,
                          operation_key=payload.operation_key, request_hash=request_hash, reviewed_hash=payload.reviewed_hash,
                          before_json=_json(preview["before"]), evidence_json=_json(payload.evidence.model_dump()),
                          reason=payload.reason, actor_id=actor.id)
            db.execute(DeliveryFulfillmentRevision.__table__.insert().values(**header))
            append_audit_event(db, actor=actor, event_category="business", result="success", source="api",
                module_code="deliveries", action_code="delivery.fulfillment.append", resource="DeliveryFulfillmentRevision",
                entity_type="delivery", entity_id=delivery_id, customer_id=payload.customer_id,
                object_ref=payload.operation_key, description="保留原票据，追加实际交付分批事实",
                details=dict(revision_id=revision_id, revision=header["revision"], reviewed_hash=payload.reviewed_hash,
                             request_hash=request_hash, previous_revision_id=header["previous_revision_id"]))
            return _response(db, header, False)
        except (IntegrityError, OperationalError) as error:
            db.rollback()
            _fail("fulfillment_write_conflict", "来源已变化或提交冲突，请重新预览")
        except Exception:
            db.rollback()
            raise
