"""Authorized finished-stock label projections and immutable print evidence."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from app.api.deps import get_db, PermissionChecker, customer_scope_ids, has_unrestricted_customer_access
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from app.services import finished_product_labels as labels
from app.services.production_label_operations import ProductionLabelOperationError, production_label_write_guard
from app.services.audit_log import append_audit_event

router = APIRouter(prefix='/finished-product-labels')
can_read = PermissionChecker('orders.view')


class Selection(BaseModel):
    lot_id: int = Field(gt=0, strict=True)
    expected_version: int = Field(gt=0, strict=True)


class PrintCount(BaseModel):
    selection_key: str = Field(min_length=1, max_length=100)
    print_label_count: int = Field(ge=0, le=10000, strict=True)


class Prepare(BaseModel):
    selections: list[Selection] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=120)
    plan_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    items: list[PrintCount] = Field(min_length=1, max_length=200)
    reprint_confirmed: bool = False

    @model_validator(mode='after')
    def unique(self):
        if len({s.lot_id for s in self.selections}) != len(self.selections) or len({i.selection_key for i in self.items}) != len(self.items):
            raise ValueError('成品批次或标签选择重复')
        return self


class Confirmation(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: bool = False


def _scope(db, user, customer_ids):
    if not customer_ids or None in customer_ids:
        raise HTTPException(404, '成品库存记录不存在')
    if not has_unrestricted_customer_access(user, db) and not set(customer_ids).issubset(set(customer_scope_ids(user, db))):
        raise HTTPException(404, '成品库存记录不存在')


def _selection_scope(db, user, selections):
    for item in selections:
        lot = db.get(InventoryLot, item['lot_id'])
        if lot is None or lot.finished_detail is None:
            raise HTTPException(404, '成品库存记录不存在')
        _scope(db, user, [lot.finished_detail.owner_customer_id])


def _audit(db, user, job, package, action, key):
    append_audit_event(db, event_category='business', result='success', source='web', module_code='production',
        action_code=f'production.finished_product_label.{action}', resource='FinishedProductLabelPrintJob',
        actor=user, entity_type='finished_product_label_print_job', entity_id=job.id,
        batch_id=key, description='已入库成品产品标签'+('冻结' if action=='prepared' else '实际打印确认'),
        details=dict(source_page='production.placement', source_type=labels.SOURCE,
            plan_fingerprint=job.plan_fingerprint, payload_hash=job.payload_hash,
            label_count=package['label_count'], print_selection=package.get('print_selection'),
            lot_versions=package['lot_versions'], reprint_confirmed=package.get('reprint_confirmed', False)))


def _fail(db, error):
    db.rollback()
    if isinstance(error, HTTPException):
        raise error
    if isinstance(error, ProductionLabelOperationError):
        raise HTTPException(error.status_code, str(error)) from error
    if isinstance(error, (IntegrityError, OperationalError)):
        raise HTTPException(409, '标签作业或库存发生并发变化，请重试原请求或刷新核对') from error
    from app.services.production_packaging_label import ProductionPackagingLabelError
    from app.services.production_label_strategy import ProductionLabelStrategyError
    from app.services.production_packaging_label_layout import ProductionPackagingLabelLayoutError
    if isinstance(error, (ProductionPackagingLabelError, ProductionLabelStrategyError, ProductionPackagingLabelLayoutError)):
        raise HTTPException(409, str(error)) from error
    raise error


@router.get('/package')
def get_package(lots: str = Query(min_length=3, max_length=5000), db: Session = Depends(get_db), user: User = Depends(can_read)):
    try:
        pieces = [s.split(':') for s in lots.split(',')]
        if any(len(p) != 2 for p in pieces):
            raise ValueError()
        selections = [Selection(lot_id=int(p[0]), expected_version=int(p[1])).model_dump() for p in pieces]
        if not 1 <= len(selections) <= 200:
            raise ValueError()
    except ValueError as error:
        raise HTTPException(422, '成品批次选择无效') from error
    _selection_scope(db, user, selections)
    try:
        package = labels.build_package(db, selections)
        return dict(package=package, latest_printed_job=labels.latest_printed(db, selections))
    except Exception as error:
        _fail(db, error)


@router.post('/jobs')
def prepare(payload: Prepare, db: Session = Depends(get_db), user: User = Depends(can_read), _guard=Depends(production_label_write_guard)):
    selections = [s.model_dump() for s in payload.selections]
    _selection_scope(db, user, selections)
    try:
        job, package, replayed = labels.prepare(db, selections=selections, key=payload.idempotency_key,
            fingerprint=payload.plan_fingerprint, counts={i.selection_key:i.print_label_count for i in payload.items},
            operator_id=user.id, reprint_confirmed=payload.reprint_confirmed)
        _scope(db, user, package['customer_ids'])
        if not replayed:
            _audit(db, user, job, package, 'prepared', payload.idempotency_key)
        db.commit()
        return labels.response(job, package, replayed=replayed)
    except Exception as error:
        _fail(db, error)


@router.get('/jobs/{job_id}')
def get_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    try:
        job, package = labels.read_job(db, job_id)
        _scope(db, user, package['customer_ids'])
        if job.status == 'prepared':
            labels.assert_current(db, package)
        return labels.response(job, package)
    except Exception as error:
        _fail(db, error)


@router.post('/jobs/{job_id}/confirm')
def confirm(job_id: int, payload: Confirmation, db: Session = Depends(get_db), user: User = Depends(can_read), _guard=Depends(production_label_write_guard)):
    if not payload.confirmed:
        raise HTTPException(422, '仅实际出纸后确认打印')
    try:
        _, package = labels.read_job(db, job_id)
        _scope(db, user, package['customer_ids'])
        job, package, replayed = labels.confirm(db, job_id=job_id, key=payload.idempotency_key, operator_id=user.id)
        if not replayed:
            _audit(db, user, job, package, 'printed', payload.idempotency_key)
        db.commit()
        return labels.response(job, package, replayed=replayed)
    except Exception as error:
        _fail(db, error)
