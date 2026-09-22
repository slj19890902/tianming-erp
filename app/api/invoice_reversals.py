"""Register an externally completed full invoice reversal, never call the tax bureau."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_db, PermissionChecker
from app.models.customer import Customer
from app.models.finance import Invoice, SettlementRecord, Statement, StatementItem
from app.models.invoice_task import FinanceInvoiceTask
from app.models.user import User
from app.services.invoice_statement_scope import task_shares, task_statement_filter, StatementInvoice

router = APIRouter()
can_register = PermissionChecker('finance.invoice_result.register')
can_operate = PermissionChecker('finance.execute')


class ReverseInvoice(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    expected_task_version: int | None = Field(default=None, ge=1)
    expected_versions: dict[int, int]
    expected_ledger_versions: dict[int, int]
    treatment: Literal['tax_void', 'full_red', 'registration_error']
    treatment_date: date
    reference: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    confirmed: Literal[True]


class InvalidateStatement(BaseModel):
    expected_version: int = Field(ge=1)
    expected_ledger_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)


@router.post('/statements/{statement_id}/invalidate-version')
def invalidate_version(statement_id: int, payload: InvalidateStatement, db: Session = Depends(get_db),
                       user: User = Depends(can_operate)):
    from app.api import finance
    action = 'invalidate_statement_version'
    try:
        statement = finance._statement_for_user(db, statement_id, user)
        request_hash = finance._finance_request_hash(action, {'statement_id': statement_id, **payload.model_dump()})
        replay, _ = finance._finance_idempotency_replay(db, idempotency_key=payload.idempotency_key,
            request_hash=request_hash, action=action, actor=user)
        if replay is not None:
            return replay
        finance._lock_unpaid_statement_for_dispute(db, statement_id=statement_id, expected_version=payload.expected_version)
        db.refresh(statement)
        if (statement.ledger_version != payload.expected_ledger_version
            or statement.confirmation_status == 'cancelled' or statement.invoiced_amount != 0):
            raise HTTPException(409, '账单已开票或版本已变化，请刷新核对')
        if db.scalar(select(StatementInvoice.id).where(StatementInvoice.statement_id == statement_id,
                StatementInvoice.invoice_status == 'issued').limit(1)) is not None:
            raise HTTPException(409, '请先登记实际发票的作废或红冲处理结果')
        tasks = db.scalars(select(FinanceInvoiceTask).where(task_statement_filter(statement_id),
            FinanceInvoiceTask.status != 'voided')).all()
        if any(t.status == 'issued' for t in tasks):
            raise HTTPException(409, '关联任务已开票，不能直接作废对账版本')
        if not payload.reason.strip():
            raise HTTPException(422, '请填写作废原因')
        snapshot = finance._statement_detail_response(db, statement_id, user)
        snapshot.pop('voided_versions', None)
        snapshot['total_gross_profit'] = statement.total_gross_profit
        frozen_items = {i.id: i for i in db.scalars(select(StatementItem).where(StatementItem.statement_id == statement.id))}
        for item in snapshot['items']:
            frozen = frozen_items[item['statement_item_id']]
            item['unit_cost_snapshot'] = frozen.unit_cost_snapshot
            item['gross_profit_amount'] = frozen.gross_profit_amount

        before_version = statement.version
        for task in tasks:
            task.status = 'voided'
            task.voided_at = datetime.now()
            task.voided_by = user.id
            task.version += 1
        statement.confirmation_status = 'draft'
        statement.confirmed_by = None
        statement.confirmed_at = None
        statement.version += 1
        statement.ledger_version += 1
        finance._statement_adjustment_log(db, statement=statement, action=action, reason=payload.reason,
            before_version=before_version, details={'snapshot': snapshot, 'voided_task_ids': [t.id for t in tasks]}, user=user)
        finance._audit(db, user=user, action='INVALIDATE_STATEMENT_VERSION', resource='Statement',
            entity_id=statement.id, details={'before_version': before_version, 'after_version': statement.version,
            'reason': payload.reason, 'voided_task_ids': [t.id for t in tasks]}, description='作废旧对账版本并建立新草稿')
        response = {'statement_id': statement.id, 'version': statement.version, 'ledger_version': statement.ledger_version}
        finance._record_finance_idempotency(db, idempotency_key=payload.idempotency_key, request_hash=request_hash,
            action=action, actor=user, resource_type='Statement', resource_id=statement.id, response=response)
        db.commit()
        return response
    except (IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(409, '账单正在被其他操作更新，请保持原请求重试或刷新核对') from error
    except Exception:
        db.rollback()
        raise


def _scope(db, invoice_id, user):
    from app.api.invoice_tasks import _task_for_user, _statement_for_user
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, '发票记录不存在')
    task = _task_for_user(db, invoice.invoice_task_id, user) if invoice.invoice_task_id else None
    shares = task_shares(db, task) if task else [SimpleNamespace(statement_id=invoice.statement_id, total_amount=invoice.invoice_amount)]
    statements = [_statement_for_user(db, share.statement_id, user) for share in shares]
    return invoice, task, shares, statements


def _payment_blockers(db, statements):
    return [s.statement_number for s in statements if s.settled_amount != 0 or db.scalar(
        select(SettlementRecord.id).where(SettlementRecord.statement_id == s.id).limit(1)) is not None]


@router.get('/invoices/{invoice_id}/void-preview')
def preview(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(can_register),
            _permission: User = Depends(can_operate)):
    invoice, task, shares, statements = _scope(db, invoice_id, user)
    return {'invoice_id': invoice.id, 'customer_name': statements[0].settlement_name_snapshot or db.get(Customer, statements[0].customer_id).name, 'invoice_number': invoice.invoice_number,
            'invoice_amount': invoice.invoice_amount, 'invoice_status': invoice.invoice_status,
            'expected_task_version': task.version if task else None,
            'expected_versions': {s.id: s.version for s in statements},
            'expected_ledger_versions': {s.id: s.ledger_version for s in statements},
            'payment_blockers': _payment_blockers(db, statements),
            'statements': [{'id': s.id, 'statement_number': s.statement_number,
                            'statement_month': s.statement_month, 'amount': share.total_amount}
                           for s, share in zip(statements, shares)]}


@router.post('/invoices/{invoice_id}/void')
def reverse(invoice_id: int, payload: ReverseInvoice, db: Session = Depends(get_db),
            user: User = Depends(can_register), _permission: User = Depends(can_operate)):
    from app.api import finance
    from app.core.time_contract import beijing_today
    action = 'void_issued_invoice'
    try:
        invoice, task, shares, statements = _scope(db, invoice_id, user)
        request_hash = finance._finance_request_hash(action, {'invoice_id': invoice_id, **payload.model_dump()})
        replay, _ = finance._finance_idempotency_replay(db, idempotency_key=payload.idempotency_key,
            request_hash=request_hash, action=action, actor=user)
        if replay is not None:
            return replay
        if not payload.reason.strip() or not payload.reference.strip():
            raise HTTPException(422, '请填写处理依据及原因')
        if payload.treatment_date > beijing_today() or payload.treatment_date < invoice.invoice_date:
            raise HTTPException(422, '处理日期不能早于开票日期或晚于今天')
        ids = {s.id for s in statements}
        if set(payload.expected_versions) != ids or set(payload.expected_ledger_versions) != ids:
            raise HTTPException(409, '必须核对发票关联的全部账单，请刷新')
        if invoice.invoice_status != 'issued' or (task and task.status != 'issued'):
            raise HTTPException(409, '发票或任务状态已变化，请刷新，不能重复作废')
        if task and task.version != payload.expected_task_version:
            raise HTTPException(409, '开票任务版本已变化，请刷新')
        if _payment_blockers(db, statements):
            raise HTTPException(409, '关联账单已有收款，不能直接作废退回修改；请先核对收款事实')
        if sum((Decimal(str(s.total_amount)) for s in shares), Decimal(0)) != invoice.invoice_amount:
            raise HTTPException(409, '发票分配金额与原发票不一致，禁止回退')
        # Claim every ledger before touching any amount. This also serializes
        # concurrent payment/invoice registration against this transaction.
        for s, share in zip(statements, shares):
            claimed = db.execute(update(Statement).where(Statement.id == s.id,
                Statement.version == payload.expected_versions[s.id],
                Statement.ledger_version == payload.expected_ledger_versions[s.id],
                Statement.settled_amount == 0, Statement.invoiced_amount >= share.total_amount,
                Statement.confirmation_status != 'cancelled').values(
                ledger_version=Statement.ledger_version + 1,
                invoiced_amount=Statement.invoiced_amount - share.total_amount),
                execution_options={'synchronize_session': False})
            if claimed.rowcount != 1:
                raise HTTPException(409, '账单状态、金额或版本已变化，请刷新后重新核对')
        claimed = db.execute(update(Invoice).where(Invoice.id == invoice.id, Invoice.invoice_status == 'issued')
            .values(invoice_status='voided'), execution_options={'synchronize_session': False})
        if claimed.rowcount != 1:
            raise HTTPException(409, '发票已处理，请刷新')
        if task:
            claimed = db.execute(update(FinanceInvoiceTask).where(FinanceInvoiceTask.id == task.id,
                FinanceInvoiceTask.version == payload.expected_task_version, FinanceInvoiceTask.status == 'issued')
                .values(status='voided', voided_by=user.id, voided_at=datetime.now(),
                        version=FinanceInvoiceTask.version + 1), execution_options={'synchronize_session': False})
            if claimed.rowcount != 1:
                raise HTTPException(409, '任务版本已变化，请刷新')
        db.expire_all()
        reopened, retained = [], []
        for statement_id in sorted(ids):
            s = db.get(Statement, statement_id)
            active_invoice = db.scalar(select(StatementInvoice.id).where(
                StatementInvoice.statement_id == s.id, StatementInvoice.invoice_status == 'issued').limit(1))
            active_task = db.scalar(select(FinanceInvoiceTask.id).where(task_statement_filter(s.id),
                FinanceInvoiceTask.status != 'voided').limit(1))
            before_version = s.version
            before_confirmation = {'status': s.confirmation_status, 'confirmed_by': s.confirmed_by,
                                   'confirmed_at': s.confirmed_at}
            if active_invoice is None and active_task is None and s.invoiced_amount == 0:
                s.confirmation_status = 'draft'
                s.confirmed_by = None
                s.confirmed_at = None
                s.version += 1
                reopened.append(s.id)
            else:
                retained.append(s.id)
            if s.id not in reopened:
                continue  # Ledger-only change; do not invalidate another task's frozen bill version.
            finance._statement_adjustment_log(db, statement=s, action='void_issued_invoice_for_modify',
                reason=payload.reason.strip(), before_version=before_version, user=user,
                details={'invoice_id': invoice.id, 'invoice_number': invoice.invoice_number,
                         'treatment': payload.treatment, 'reference': payload.reference.strip(),
                         'treatment_date': payload.treatment_date, 'before_confirmation': before_confirmation,
                         'reopened': s.id in reopened, 'old_files_invalid': True})
        response = {'invoice_id': invoice.id, 'invoice_status': 'voided',
                    'treatment': payload.treatment, 'treatment_date': payload.treatment_date.isoformat(),
                    'reference': payload.reference.strip(), 'reason': payload.reason.strip(),
                    'reopened_statement_ids': reopened, 'retained_statement_ids': retained}
        finance._audit(db, user=user, action='VOID_ISSUED_INVOICE', resource='Invoice', entity_id=invoice.id,
            details={**payload.model_dump(), **response, 'invoice_number': invoice.invoice_number,
                     'invoice_amount': invoice.invoice_amount}, description='登记发票处理结果并受控退回对账')
        finance._record_finance_idempotency(db, idempotency_key=payload.idempotency_key, request_hash=request_hash,
            action=action, actor=user, resource_type='Invoice', resource_id=invoice.id, response=response)
        db.commit()
        return response
    except (IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(409, '账单正在被其他操作更新，请保持原请求重试或刷新核对') from error
    except Exception:
        db.rollback()
        raise
