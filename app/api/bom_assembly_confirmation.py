"""One physical confirmation, customer-scoped and transaction-owned."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.api.deps import get_db, require_customer_access
from app.api.production import can_read, can_complete, admin_only, _allowed_customer_ids
from app.models.order import OrderItem, Order
from app.models.user import User
from app.services import bom_pending_assembly as service
from app.services.bom_subkits import SubkitError
from app.services.multilevel_bom_plan import BomPlanError
from app.services.production_workflow import ProductionWorkflowError
from app.services.warehouse_inventory import WarehouseInventoryError

router = APIRouter()


class ReverseAssembly(BaseModel):
    confirm_reverse: bool


@router.post('/assemblies/{assembly_id}/reverse')
def reverse(assembly_id: int, payload: ReverseAssembly, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    import json
    from app.models.multilevel_bom import BomAssembly
    from app.services.multilevel_bom_inventory import reverse_order_assembly
    from app.services.production_workflow import refresh_production_task, refresh_order_production_status
    from app.services.audit_log import append_audit_event
    row = db.get(BomAssembly, assembly_id)
    if not row:
        raise HTTPException(404, '组套记录不存在')
    item = db.get(OrderItem,row.order_item_id)
    order = db.get(Order,item.order_id)
    require_customer_access(order.customer_id,user,db)
    if not payload.confirm_reverse:
        raise HTTPException(409,'请确认撤销本次组套')
    operation = json.loads(row.cost_detail_json).get('graph_operation',{})
    if not operation.get('key'):
        raise HTTPException(409,'缺少原组套来源，不能直接撤销')
    try:
        from app.services.bom_transactions import atomic_bom
        with atomic_bom(db):
            results = reverse_order_assembly(db,order_item_id=item.id,operation_key=operation['key'],operator_id=user.id,
                source_snapshot_id=next(iter(operation.get('source_ids',[])),None))
            refresh_production_task(db,item.id)
            refresh_order_production_status(db,order.id)
            append_audit_event(db,event_category='business',result='success',source='web',module_code='production',
                action_code='bom.reverse_assembly',resource='production',actor=user,entity_type='order_item',entity_id=item.id,
                details=dict(assembly_ids=[r.id for r in results],confirm_reverse=True))
        db.commit()
        return dict(assembly_ids=[r.id for r in results],status='reversed')
    except (SubkitError,BomPlanError,ProductionWorkflowError,WarehouseInventoryError) as error:
        db.rollback()
        raise HTTPException(getattr(error,'status_code',409),str(error)) from error
    except Exception:
        db.rollback()
        raise


class ConfirmAssembly(BaseModel):
    operation_key: str = Field(min_length=1, max_length=100)
    physical_assembly_confirmed: bool
    source_lot_versions: dict[int, int]
    target_locations: dict[int, int]
    available_lot_ids: list[int]
    expected_outputs: dict[int, int]


@router.get('/pending-assemblies')
def pending(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
            user: User = Depends(can_read), db: Session = Depends(get_db)):
    rows = service.pending(db, _allowed_customer_ids(user, db))
    return dict(items=rows[(page-1)*page_size:page*page_size], total=len(rows), page=page, page_size=page_size)


@router.get('/assembly-locations')
def locations(user: User = Depends(can_read), db: Session = Depends(get_db)):
    from app.services.location_candidates import list_operational_locations
    from app.services.warehouse_location_address import employee_location_name
    rows = list_operational_locations(db, warehouse_types={'finished'}, empty_only=False)
    return dict(items=[dict(id=r.location.id, name=employee_location_name(r.location, area=r.area, floor=r.floor)) for r in rows])


@router.post('/assemblies/{item_id}/confirm')
def confirm(item_id: int, payload: ConfirmAssembly, user: User = Depends(can_complete), db: Session = Depends(get_db)):
    item = db.get(OrderItem, item_id)
    if not item:
        raise HTTPException(404, '订单明细不存在')
    order = db.get(Order, item.order_id)
    require_customer_access(order.customer_id, user, db)
    if not payload.physical_assembly_confirmed:
        raise HTTPException(409, '请在现场组装（插合或粘合）完成后确认')
    try:
        result = service.confirm(db, item_id=item_id, command=payload.model_dump(), actor=user)
        db.commit()
        return result
    except (SubkitError, BomPlanError, ProductionWorkflowError, WarehouseInventoryError) as error:
        db.rollback()
        raise HTTPException(getattr(error, 'status_code', 409), str(error)) from error
    except Exception:
        db.rollback()
        raise
