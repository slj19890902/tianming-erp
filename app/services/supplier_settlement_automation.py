"""Draft-only supplier settlement automation; never confirms or pays a bill."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from app.models.audit import OperationLog
from app.services.supplier_monthly_settlement import generate_due_supplier_settlements

log = logging.getLogger(__name__)


def run_due_cycle(session_factory):
    # A system-origin draft must not impersonate an administrator account.
    with session_factory() as db:
        try:
            result = generate_due_supplier_settlements(
                db, user=SimpleNamespace(id=None), replace_changed_drafts=True
            )
            if result['changed_statement_count']:
                db.add(OperationLog(
                    action='AUTO_DRAFT', resource='SupplierMonthlyStatement',
                    description='按供应商对账日自动生成或更新未确认草稿',
                    details=json.dumps(result, ensure_ascii=False, default=str),
                    event_category='data_change', result='success', source='scheduler',
                    module_code='supplier_monthly_settlement',
                    action_code='supplier_settlement.auto_draft',
                    operator_name_snapshot='系统自动草稿', schema_version=1,
                ))
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


async def settlement_loop(stop: asyncio.Event, session_factory, interval=3600):
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_due_cycle, session_factory)
        except Exception:
            log.exception('供应商到期草稿自动处理失败；事务回滚，下次重试')
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
