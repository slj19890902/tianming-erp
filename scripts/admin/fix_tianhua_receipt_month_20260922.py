"""Scoped owner-authorized correction of exactly three unbilled receipt months.

Read-only by default. Apply requires exact preview, active administrator,
verified new SQLite backup, versions, customer scope, idempotency and audit.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from app.api import finance
from app.api.deps import has_permission
from app.models.customer import Customer
from app.models.delivery import Delivery
from app.models.finance import ReturnReceipt, ReturnReceiptItem, FinanceIdempotencyRecord
from app.models.user import User
from app.services.audit_log import append_audit_event

NUMBERS = ('TH-20260922-001', 'TH-20260922-002', 'TH-20260922-003')
BATCH = 'owner-approved-th-month-20260922'
REASON = '老板2026-09-22明确授权：天华三张9月22日未对账回单从9月改为10月；仅月份及版本变更，Codex通过维护脚本执行'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))


def preview(db):
    rows = db.execute(select(Delivery, ReturnReceipt, Customer).join(ReturnReceipt, ReturnReceipt.delivery_id == Delivery.id)
        .join(Customer, Customer.id == Delivery.customer_id).where(Delivery.delivery_number.in_(NUMBERS))
        .order_by(Delivery.delivery_number)).all()
    assert len(rows) == 3 and {d.delivery_number for d, r, c in rows} == set(NUMBERS)
    result = []
    for delivery, receipt, customer in rows:
        assert customer.name == '苏州天华超净科技股份有限公司' and customer.statement_cycle_start_day == 20
        assert str(delivery.delivery_date) == '2026-09-22' and receipt.status == 'confirmed'
        items = db.scalars(select(ReturnReceiptItem).where(ReturnReceiptItem.return_receipt_id == receipt.id)).all()
        assert items and not any(i.reconciliation_month_override for i in items)
        assert finance._reconciliation_month_block_reason(db, [i.id for i in items]) is None, delivery.delivery_number
        result.append(dict(delivery_number=delivery.delivery_number, customer_id=customer.id,
            receipt_id=receipt.id, version=receipt.version, month=receipt.reconciliation_month,
            signed_date=str(receipt.actual_received_date), quantities=[(i.id, i.actual_received_quantity) for i in items]))
    return {'receipts': result, 'fingerprint': hashlib.sha256(canonical(result).encode()).hexdigest()}


def fingerprints(db):
    result = {}
    tables = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")).scalars()
    for name in tables:
        if name in {'finance_return_receipts', 'finance_idempotency_records', 'operation_logs'}:
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        digest = hashlib.sha256()
        for row in db.execute(text(f'SELECT * FROM {quoted} ORDER BY rowid')):
            digest.update(canonical(list(row)).encode() + b'\n')
        result[name] = digest.hexdigest()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--expected')
    p.add_argument('--actor-id', type=int)
    p.add_argument('--backup')
    args = p.parse_args()
    target = Path(args.database).resolve(strict=True)
    output = Path(args.output).resolve()
    assert not output.exists() and output != target
    if args.apply:
        assert args.expected and args.actor_id and args.backup
    backup = Path(args.backup).resolve() if args.backup else None
    if backup:
        assert not backup.exists() and backup not in {target, output}
    def connect():
        c = sqlite3.connect(target.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True, timeout=30)
        c.execute('PRAGMA foreign_keys=ON')
        if not args.apply: c.execute('PRAGMA query_only=ON')
        return c
    engine = create_engine('sqlite+pysqlite://', creator=connect)
    with Session(engine) as db:
        db.execute(text('BEGIN IMMEDIATE' if args.apply else 'BEGIN'))
        assert db.execute(text('PRAGMA integrity_check')).scalars().all() == ['ok']
        assert not db.execute(text('PRAGMA foreign_key_check')).all()
        assert db.execute(text('SELECT version_num FROM alembic_version')).scalars().all() == ['dz0922']
        result = preview(db)
        result.update(database=str(target), read_only=not args.apply)
        if args.apply:
            user = db.get(User, args.actor_id)
            assert user and user.is_active and user.role == 'admin'
            assert has_permission(user, 'finance.execute') and has_permission(user, 'finance.return_receipt.period.adjust')
            for row in result['receipts']:
                finance._return_receipt_for_user(db, row['receipt_id'], user)
            previous = db.scalars(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key == BATCH)).first()
            if previous:
                assert previous.actor_user_id == user.id and all(r['month'] == '2026-10' for r in result['receipts'])
                result.update(replayed=True, changed=0)
            else:
                assert result['fingerprint'] == args.expected, 'Preview changed; stop and recheck'
                assert all(r['month'] == '2026-09' for r in result['receipts'])
                # Writer lock is held throughout point-in-time backup and mutation.
                with sqlite3.connect(target.as_uri()+'?mode=ro', uri=True) as source, sqlite3.connect(backup) as dest:
                    source.backup(dest)
                    assert dest.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                    assert not dest.execute('PRAGMA foreign_key_check').fetchall()
                digest = hashlib.file_digest(backup.open('rb'), 'sha256').hexdigest()
                before = fingerprints(db)
                receipt_rows = [dict(r) for r in db.execute(text('SELECT * FROM finance_return_receipts ORDER BY id')).mappings()]
                for row in result['receipts']:
                    receipt = db.get(ReturnReceipt, row['receipt_id'])
                    version = finance._claim_return_receipt_version(db, receipt_id=receipt.id,
                        expected_status='confirmed', expected_version=row['version'])
                    db.expire(receipt)
                    receipt.reconciliation_month = '2026-10'
                    append_audit_event(db, actor=user, event_category='business', result='success', source='script',
                        module_code='finance', action_code='UPDATE_RETURN_RECONCILIATION_MONTH', resource='ReturnReceipt',
                        entity_type='ReturnReceipt', entity_id=receipt.id, customer_id=row['customer_id'],
                        batch_id=BATCH, description=REASON, details={'delivery_number':row['delivery_number'],
                        'before': {'month':row['month'],'version':row['version']},
                        'after': {'month':'2026-10','version':version}, 'preview':args.expected,'backup_sha256':digest})
                db.flush()
                assert fingerprints(db) == before, 'Unrelated business table changed'
                after_receipts = [dict(r) for r in db.execute(text('SELECT * FROM finance_return_receipts ORDER BY id')).mappings()]
                selected = {r['receipt_id'] for r in result['receipts']}
                expected_rows = [{**r, **({'reconciliation_month':'2026-10','version':r['version']+1} if r['id'] in selected else {})} for r in receipt_rows]
                assert after_receipts == expected_rows, 'Receipt fields changed outside authorized scope'
                response = dict(changed=3, receipt_ids=sorted(selected), target_month='2026-10')
                finance._record_finance_idempotency(db, idempotency_key=BATCH, request_hash=args.expected,
                    action='owner_receipt_month_correction', actor=user, resource_type='ReturnReceipt',
                    resource_id=min(selected), response=response)
                db.flush()
                assert not db.execute(text('PRAGMA foreign_key_check')).all()
                result.update(**response, backup=str(backup), backup_sha256=digest,
                    unchanged_tables=len(before), after=preview(db)['receipts'], reason=REASON)
                db.commit()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(canonical(result))


if __name__ == '__main__':
    main()
