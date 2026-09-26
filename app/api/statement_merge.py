"""User-selected same-customer statements merge without duplicating receivables."""
import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_db, PermissionChecker
from app.models.customer import Customer
from app.models.finance import Statement, StatementItem, SettlementRecord
from app.models.invoice_task import FinanceInvoiceTask
from app.models.user import User
from app.services.invoice_statement_scope import task_statement_filter, StatementInvoice

router = APIRouter()
can_operate = PermissionChecker('finance.execute')


class MergeSource(BaseModel):
    statement_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    expected_ledger_version: int = Field(gt=0)


class MergeStatements(BaseModel):
    sources: list[MergeSource] = Field(min_length=2, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode='after')
    def unique_sources(self):
        if len({s.statement_id for s in self.sources}) != len(self.sources):
            raise ValueError('不能重复选择同一份对账单')
        return self


def _blocker(db, statement):
    if statement.confirmation_status == 'cancelled':
        return '原对账单已停用，请查看承接的新对账单'
    if statement.invoiced_amount != 0 or db.scalar(select(StatementInvoice.id).where(
            StatementInvoice.statement_id == statement.id, StatementInvoice.invoice_status == 'issued').limit(1)):
        return '已有实际开票，请先按关联范围办理发票作废／红冲登记'
    if statement.settled_amount != 0 or db.scalar(select(SettlementRecord.id).where(
            SettlementRecord.statement_id == statement.id).limit(1)):
        return '已有收款流水，不能直接合并改写'
    if db.scalar(select(FinanceInvoiceTask.id).where(task_statement_filter(statement.id),
            FinanceInvoiceTask.status != 'voided').limit(1)):
        return '已有有效开票任务，请先在开票办理受控撤销任务；旧文件不可继续使用'
    if not db.scalar(select(StatementItem.id).where(StatementItem.statement_id == statement.id).limit(1)):
        return '没有可合并明细'
    return ''


def _identity(db, statement):
    from app.api import finance
    return (statement.customer_id, statement.statement_month, statement.settlement_entity_id,
            statement.settlement_name_snapshot, statement.statement_cycle_start_day_snapshot,
            frozenset(finance._statement_scope_customer_ids(db, statement)))


@router.get('/statements/{statement_id}/merge-candidates')
def candidates(statement_id: int, db: Session = Depends(get_db), user: User = Depends(can_operate)):
    from app.api import finance
    anchor = finance._statement_for_user(db, statement_id, user)
    rows = db.scalars(select(Statement).where(Statement.customer_id == anchor.customer_id,
        Statement.statement_month == anchor.statement_month,
        Statement.confirmation_status != 'cancelled').order_by(Statement.id)).all()
    result = []
    for row in rows:
        finance._statement_for_user(db, row.id, user)
        blocker = ('冻结结算归属或账期规则不一致，不能合并'
                   if _identity(db, row) != _identity(db, anchor) else _blocker(db, row))
        result.append({'id': row.id, 'statement_number': row.statement_number,
            'statement_month': row.statement_month, 'total_receivable': row.total_receivable,
            'version': row.version, 'ledger_version': row.ledger_version,
            'confirmation_status': row.confirmation_status, 'blocker': blocker})
    return {'items': result, 'customer_name': anchor.settlement_name_snapshot or db.get(Customer, anchor.customer_id).name,
            'statement_month': anchor.statement_month}


@router.post('/statements/{statement_id}/merge')
def merge(statement_id: int, payload: MergeStatements, db: Session = Depends(get_db),
          user: User = Depends(can_operate)):
    from app.api import finance
    sources = sorted(payload.sources, key=lambda row: row.statement_id)
    if statement_id not in {row.statement_id for row in sources}:
        raise HTTPException(422, '合并范围必须包含当前对账单')
    action = 'merge_statements'
    request_hash = finance._finance_request_hash(action, {'anchor': statement_id,
        'sources': [row.model_dump() for row in sources]})
    try:
        # Verify every frozen member, then serialize all eligibility checks and writes.
        for source in sources:
            finance._statement_for_user(db, source.statement_id, user)
        db.execute(update(Statement).where(Statement.id == sources[0].statement_id).values(version=Statement.version))
        db.expire_all()
        replay, _ = finance._finance_idempotency_replay(db, idempotency_key=payload.idempotency_key,
            request_hash=request_hash, action=action, actor=user)
        if replay is not None:
            finance._statement_for_user(db, replay['id'], user)
            db.rollback()
            return replay
        rows = [finance._statement_for_user(db, source.statement_id, user) for source in sources]
        first = rows[0]
        snapshots = {}
        for source, row in zip(sources, rows):
            if row.version != source.expected_version or row.ledger_version != source.expected_ledger_version:
                raise HTTPException(409, '账单或财务流水版本已变化，请刷新后重新选择')
            if _identity(db, row) != _identity(db, first):
                raise HTTPException(409, '只能合并同一客户、同一账期且冻结结算归属一致的对账单')
            blocker = _blocker(db, row)
            if blocker:
                raise HTTPException(409, f'{row.statement_number}：{blocker}')
            snapshot = finance._statement_detail_response(db, row.id, user)
            snapshot.pop('voided_versions', None)
            snapshot['total_gross_profit'] = row.total_gross_profit
            items = {item.id: item for item in db.scalars(select(StatementItem).where(StatementItem.statement_id == row.id))}
            if sum((i.receivable_amount for i in items.values()), Decimal(0)) != row.total_receivable:
                raise HTTPException(409, '账单总额与明细不一致，请先核对，不能合并')
            for item in snapshot['items']:
                frozen = items[item['statement_item_id']]
                item.update(unit_cost_snapshot=frozen.unit_cost_snapshot, gross_profit_amount=frozen.gross_profit_amount)
            snapshots[row.id] = snapshot
        merged = Statement(statement_number=finance._next_statement_number(db, first.statement_month),
            customer_id=first.customer_id, statement_month=first.statement_month,
            settlement_entity_id=first.settlement_entity_id, settlement_name_snapshot=first.settlement_name_snapshot,
            settlement_customer_ids_snapshot_json=json.dumps(sorted(finance._statement_scope_customer_ids(db, first))),
            statement_cycle_start_day_snapshot=first.statement_cycle_start_day_snapshot,
            total_receivable=sum((row.total_receivable for row in rows), Decimal(0)),
            total_gross_profit=sum((row.total_gross_profit for row in rows), Decimal(0)),
            status='unsettled', confirmation_status='draft', created_by=user.id)
        db.add(merged)
        db.flush()
        for row in rows:
            before_version = row.version
            row.settlement_customer_ids_snapshot_json = json.dumps(snapshots[row.id]["source_customer_ids"])
            db.execute(update(StatementItem).where(StatementItem.statement_id == row.id).values(statement_id=merged.id))
            row.confirmation_status = 'cancelled'
            row.version += 1
            row.ledger_version += 1
            finance._statement_adjustment_log(db, statement=row, action=action,
                reason=f'手动合并至 {merged.statement_number}，原文件停用', before_version=before_version,
                details={'snapshot': snapshots[row.id], 'merged_statement_id': merged.id,
                         'merged_statement_number': merged.statement_number}, user=user)
        finance._audit(db, user=user, action='MERGE_STATEMENTS', resource='Statement', entity_id=merged.id,
            details={'source_statement_ids': [row.id for row in rows], 'total_receivable': merged.total_receivable,
                     'old_files_must_not_be_used': True}, description='多份对账单合并为新账单，重新核对后统一开票')
        db.flush()
        response = finance._statement_detail_response(db, merged.id, user)
        finance._record_finance_idempotency(db, idempotency_key=payload.idempotency_key, request_hash=request_hash,
            action=action, actor=user, resource_type='Statement', resource_id=merged.id, response=response)
        db.commit()
        return response
    except (IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(409, '对账单正在被其他操作更新，请保留原请求重试或刷新核对') from error
    except Exception:
        db.rollback()
        raise
