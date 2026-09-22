"""Explicit partner invoice consolidation; reconciliation statements stay separate."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api import invoice_tasks as api
from app.models.customer import Customer
from app.models.finance import Statement, StatementItem, SettlementRecord
from app.models.invoice_task import FinanceInvoiceTask as Task, FinanceInvoiceTaskItem as Item, FinanceInvoiceTaskStatement as Share
from app.models.user import User
from app.services.invoice_statement_scope import task_shares, task_statement_filter

MergePayload = api.PartnerMergePayload


def _active_tasks(db, statement_id):
    return db.scalars(select(Task).where(task_statement_filter(statement_id),
        Task.status.not_in(("voided", "issued"))).order_by(Task.id)).all()


def _partner_preview(db, statement_id, user):
    anchor = api._statement_for_user(db, statement_id, user)
    if not anchor.settlement_entity_id:
        raise HTTPException(409, "该账单不是合作归集账单")
    statements = db.scalars(select(Statement).where(
        Statement.settlement_entity_id == anchor.settlement_entity_id,
        Statement.statement_month == anchor.statement_month,
        Statement.confirmation_status != "cancelled",
        Statement.invoiced_amount < Statement.total_receivable,
    ).order_by(Statement.id)).all()
    rows, blockers, frozen = [], [], []
    for statement in statements:
        api._statement_for_user(db, statement.id, user)
        tasks = _active_tasks(db, statement.id)
        for task in tasks:
            api._task_for_user(db, task.id, user)
        names = db.scalars(select(Customer.name).where(Customer.id.in_(
            select(StatementItem.source_customer_id).where(StatementItem.statement_id == statement.id,
                StatementItem.source_customer_id.is_not(None))))).all()
        amount = statement.total_receivable - statement.invoiced_amount
        if statement.confirmation_status != "confirmed":
            blockers.append(f"{statement.statement_number} 尚未确认，请先完成该客户对账")
        if statement.invoiced_amount > 0:
            blockers.append(f"{statement.statement_number} 已部分开票，请按既有开票范围处理，不能整单合并")
        if len(tasks) > 1:
            blockers.append(f"{statement.statement_number} 有多个有效任务，请先核对任务占用")
        if tasks:
            task = tasks[0]
            share = next(s for s in task_shares(db, task) if s.statement_id == statement.id)
            if share.statement_version != statement.version or share.total_amount != amount:
                blockers.append(f"{statement.statement_number} 冻结任务与账单版本或金额不一致")
            frozen.append((statement.id, [(t.id, t.version, t.status, t.source_snapshot_hash) for t in tasks]))
        else:
            frozen.append((statement.id, []))
        rows.append({"statement_id": statement.id, "statement_number": statement.statement_number,
            "customer_name": "、".join(names) or db.get(Customer, statement.customer_id).name,
            "statement_version": statement.version, "ledger_version": statement.ledger_version,
            "confirmation_status": statement.confirmation_status,
            "total_amount": amount, "task_ids": [t.id for t in tasks]})
    if not rows:
        blockers.append("该账期已无可合并的未开票账单，请查看实际开票记录")
    scope_hash = api._snapshot_hash({"entity": anchor.settlement_entity_id,
        "month": anchor.statement_month, "rows": json.loads(json.dumps(rows, default=str)), "tasks": frozen})
    return {"statement_id": statement_id, "settlement_entity_id": anchor.settlement_entity_id,
        "settlement_name": anchor.settlement_name_snapshot, "statement_month": anchor.statement_month,
        "statements": rows, "total_amount": sum((r["total_amount"] for r in rows), Decimal(0)),
        "blockers": blockers, "scope_hash": scope_hash}


def partner_preview(statement_id: int, db: Session, user: User):
    return _partner_preview(db, statement_id, user)


def _same_buyer(task):
    buyer = json.loads(task.buyer_snapshot_json)
    return {key: buyer.get(key) for key in ("invoice_title", "tax_no", "invoice_address",
        "invoice_phone", "bank_name", "bank_account", "settlement_entity_id")}


def merge_partner_tasks(statement_id: int, payload: MergePayload,
    db: Session, user: User):
    # Denied member access must be detected before taking a business write lock.
    _partner_preview(db, statement_id, user)
    prefix = "partner:" + hashlib.sha256(f"{user.id}\0{payload.idempotency_key}".encode()).hexdigest() + ":"
    key = prefix + api._snapshot_hash({"statement": statement_id, "scope": payload.scope_hash})
    try:
        # Acquire the SQLite writer lock before re-reading membership, task and ledger versions.
        db.execute(update(Statement).where(Statement.id == statement_id).values(version=Statement.version))
        db.expire_all()
        replay = db.scalar(select(Task).where(Task.idempotency_key.like(prefix + "%")))
        if replay:
            api._task_for_user(db, replay.id, user)
            if replay.idempotency_key != key:
                raise HTTPException(409, "幂等键已用于不同的合并范围")
            return api._task_response(db, replay)
        preview = _partner_preview(db, statement_id, user)
        if preview["scope_hash"] != payload.scope_hash:
            raise HTTPException(409, "合作公司账单或任务已变化，请刷新合并范围后重试")
        if preview["blockers"]:
            raise HTTPException(409, {"message": "不能合并开票", "missing_items": preview["blockers"]})
        ids = {r["statement_id"] for r in preview["statements"]}
        tasks = {}
        for row in preview["statements"]:
            active = _active_tasks(db, row["statement_id"])
            if not active:
                created = api._create_invoice_task(row["statement_id"], api.TaskCreatePayload(
                    expected_version=row["statement_version"], idempotency_key=f"merge-source:{key[-64:]}:{row['statement_id']}"),
                    db, user, commit=False)
                active = [db.get(Task, created["id"])]
            for task in active:
                api._ensure_task_source_current(db, task)
                tasks[task.id] = task
        first = next(iter(tasks.values()))
        if len(tasks) == 1 and {s.statement_id for s in task_shares(db, first)} == ids:
            if first.status in ("draft", "failed"):
                first.status = "ready"
                first.confirmed_by, first.confirmed_at = user.id, datetime.now()
                first.version += 1
                api._audit(db, user=user, action="CONFIRM_INVOICE_TASK", resource="FinanceInvoiceTask",
                    entity_id=first.id, customer=db.get(Customer, first.customer_id),
                    details={"statement_ids": sorted(ids)}, description="确认合作公司开票任务")
            db.commit()
            return api._task_response(db, first)
        for task in tasks.values():
            if (task.seller_entity_id != first.seller_entity_id or
                task.seller_snapshot_json != first.seller_snapshot_json or
                task.invoice_type != first.invoice_type or task.price_tax_mode != first.price_tax_mode or
                _same_buyer(task) != _same_buyer(first)):
                raise HTTPException(409, "各账单的冻结购方、销方或开票方式不一致，不能强行合并；请核对税务资料与原任务")
            if not {s.statement_id for s in task_shares(db, task)} <= ids:
                raise HTTPException(409, "原合并任务包含本次范围外账单，不能拆散")
        shares = [s for task in tasks.values() for s in task_shares(db, task)]
        if len(shares) != len(ids) or sum((s.total_amount for s in shares), Decimal(0)) != preview["total_amount"]:
            raise HTTPException(409, "账单任务占用重复或金额不守恒，请刷新核对")
        merged = Task(task_number=f"IT-M{preview['settlement_entity_id']}-{key[-24:]}",
            statement_id=first.statement_id, statement_version=first.statement_version,
            customer_id=first.customer_id, seller_entity_id=first.seller_entity_id,
            buyer_snapshot_json=first.buyer_snapshot_json, seller_snapshot_json=first.seller_snapshot_json,
            invoice_type=first.invoice_type, price_tax_mode=first.price_tax_mode,
            net_amount=sum((s.net_amount for s in shares), Decimal(0)),
            tax_amount=sum((s.tax_amount for s in shares), Decimal(0)), total_amount=preview["total_amount"],
            status="ready", rule_version=max(t.rule_version for t in tasks.values()),
            source_snapshot_hash=preview["scope_hash"], idempotency_key=key,
            created_by=user.id, confirmed_by=user.id, confirmed_at=datetime.now())
        db.add(merged)
        db.flush()
        for share in shares:
            db.add(Share(task_id=merged.id, statement_id=share.statement_id, statement_version=share.statement_version,
                net_amount=share.net_amount, tax_amount=share.tax_amount, total_amount=share.total_amount))
        items = db.scalars(select(Item).where(Item.task_id.in_(tasks)).order_by(Item.task_id, Item.sequence_no)).all()
        source_ids = {i.statement_item_id for i in items}
        expected_ids = set(db.scalars(select(StatementItem.id).where(StatementItem.statement_id.in_(ids))).all())
        if len(source_ids) != len(items) or source_ids != expected_ids:
            raise HTTPException(409, "任务明细与全部来源账单不一致，不能漏行合并")
        for index, item in enumerate(items, 1):
            values = {c.name: getattr(item, c.name) for c in Item.__table__.columns if c.name not in ("id", "task_id", "sequence_no")}
            db.add(Item(task_id=merged.id, sequence_no=index, **values))
        for task in tasks.values():
            task.status, task.voided_by, task.voided_at = "voided", user.id, datetime.now()
            task.version += 1
            api._audit(db, user=user, action="SUPERSEDE_INVOICE_TASK", resource="FinanceInvoiceTask",
                entity_id=task.id, customer=db.get(Customer, task.customer_id),
                details={"merged_task_id": merged.id, "old_tax_template_must_not_be_used": True}, description="原开票任务由合作合并任务承接，旧文件停用")
        api._audit(db, user=user, action="MERGE_PARTNER_INVOICE_TASKS", resource="FinanceInvoiceTask",
            entity_id=merged.id, customer=db.get(Customer, merged.customer_id),
            details={"source_task_ids": sorted(tasks), "statement_ids": sorted(ids), "total_amount": merged.total_amount,
                "line_count": len(items), "scope_hash": preview["scope_hash"]}, description="独立对账单合并为合作公司一张发票任务")
        db.commit()
        return api._task_response(db, merged)
    except Exception:
        db.rollback()
        raise


def ensure_complete_partner_download(db, task, user):
    anchor = db.get(Statement, task.statement_id)
    if anchor.settlement_entity_id and task.status != "issued":
        preview = _partner_preview(db, anchor.id, user)
        if preview["blockers"] or {r["statement_id"] for r in preview["statements"]} != {s.statement_id for s in task_shares(db, task)}:
            raise HTTPException(409, "合作公司还有其他本期账单，请使用合并下载开票文件，核对完整范围后再开票")


def void_merged_task(db, task, payload, user):
    try:
        claimed = db.execute(update(Task).where(Task.id == task.id, Task.version == payload.expected_version,
            Task.status.not_in(("issued", "voided"))).values(status="voided", version=Task.version + 1,
            voided_by=user.id, voided_at=datetime.now()).returning(Task.id)).scalar_one_or_none()
        if claimed is None:
            raise HTTPException(409, "合并任务状态或版本已变化；实际开票不能普通撤销")
        db.expire_all()
        api._ensure_task_source_current(db, task)
        for share in task_shares(db, task):
            statement = db.get(Statement, share.statement_id)
            if statement.invoiced_amount > 0 or statement.settled_amount > 0 or db.scalar(select(SettlementRecord.id).where(
                SettlementRecord.statement_id == statement.id).limit(1)) is not None:
                raise HTTPException(409, "合并范围内已有开票或收款事实，不能普通撤销，请按关联事实处理")
        api._audit(db, user=user, action="VOID_MERGED_INVOICE_TASK", resource="FinanceInvoiceTask",
            entity_id=task.id, customer=db.get(Customer, task.customer_id), details={
                "statement_ids": [s.statement_id for s in task_shares(db, task)], "old_tax_template_must_not_be_used": True},
            description="撤销未开票合并任务，保留各子客户确认，需修改时分别发起异议")
        db.commit()
        return {**api._task_response(db, task), "reopened_statement": False,
            "message": "合并任务已撤销，旧文件不可继续使用；请在要修改的子客户账单上点击客户异议，其他客户确认保持。"}
    except Exception:
        db.rollback()
        raise
