"""Precise owner-authorized removal of a generated business-description suffix.

Preview is read only. Execute compares the complete reviewed plan, records an
immutable receipt, and leaves commit/backup verification to the caller.
"""
import hashlib
import json
import re
from sqlalchemy import inspect, select, text, update
from fastapi import HTTPException
from app.models.finance import FinanceIdempotencyRecord
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom

PHRASE = '单片尺寸未登记'
ACTION = 'delivery.description_cleanup_20261017'
FIELDS = {
 'products': ('remark', 'report_notes', 'base_report_notes', 'production_notes'),
 'sales_order_items': ('snapshot_spec', 'requisition_spec', 'snapshot_production_notes'),
 'sales_delivery_items': ('specification_snapshot', 'remarks'),
 'finished_goods_inventory_details': ('physical_basis_json',),
 'inventory_lots': ('remarks',),
 'production_tasks': ('snapshot_spec', 'production_notes'),
 'stock_replenishment_order_items': ('snapshot_spec', 'specification_snapshot'),
}


def cleaned(value):
    return re.sub(r'[（(]\s*单片尺寸未登记\s*[）)]|单片尺寸未登记', '', value)


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def preview(db):
    schema=inspect(db.connection());names=set(schema.get_table_names());entries=[]
    for table, fields in FIELDS.items():
        if table not in names:continue
        columns={c['name'] for c in schema.get_columns(table)}
        if 'id' not in columns:continue
        for field in fields:
            if field not in columns:continue
            for row in db.execute(text(f'SELECT id, "{field}" FROM "{table}" WHERE "{field}" LIKE :pattern ORDER BY id'),{'pattern':'%'+PHRASE+'%'}):
                entries.append(dict(table=table,id=row[0],field=field,before=row[1],after=cleaned(row[1])))
    return {'entries':entries,'hash':digest(entries)}


def execute(db, *, actor, plan):
    if actor is None or not actor.is_active or actor.role!='admin':
        raise HTTPException(403,'仅活动管理员可执行本次资料文字清理')
    key='delivery-text-cleanup-20261017:'+str(plan.get('hash',''))
    if len(key)>120 or plan.get('hash')!=digest(plan.get('entries',[])):
        raise HTTPException(409,'清理清单无效')
    with atomic_bom(db):
        record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==key))
        if record:
            if record.actor_user_id!=actor.id or record.request_hash!=plan['hash'] or record.action!=ACTION:
                raise HTTPException(409,'清理回执不匹配')
            return json.loads(record.response_json)
        current=preview(db)
        if current!=plan:raise HTTPException(409,'资料已变化，请重新生成清理清单')
        schema=inspect(db.connection()); touched=set()
        for item in current['entries']:
            table,field=item['table'],item['field']
            changed=db.execute(text(f'UPDATE "{table}" SET "{field}"=:after WHERE id=:id AND "{field}"=:before'),item)
            if changed.rowcount!=1:raise HTTPException(409,'资料并发变化，清理已停止')
            touched.add((table,item['id']))
        for table,row_id in sorted(touched):
            columns={c['name'] for c in schema.get_columns(table)}
            if 'version' in columns:
                db.execute(text(f'UPDATE "{table}" SET version=version+1 WHERE id=:id'),{'id':row_id})
        # Delivery versions must reflect a change to a frozen display field too.
        ids={item['id'] for item in current['entries'] if item['table']=='sales_delivery_items'}
        if ids:
            from app.models.delivery import Delivery,DeliveryItem
            dids=list(db.scalars(select(DeliveryItem.delivery_id).where(DeliveryItem.id.in_(ids))).unique())
            db.execute(update(Delivery).where(Delivery.id.in_(dids)).values(version=Delivery.version+1))
        result={'hash':current['hash'],'fields_changed':len(current['entries']),'rows_changed':len(touched)}
        append_audit_event(db,actor=actor,event_category='business',result='success',source='script',
            module_code='delivery',action_code=ACTION,resource='business_description',
            description='按老板要求删除指定资料后缀',details=result)
        db.add(FinanceIdempotencyRecord(idempotency_key=key,request_hash=current['hash'],action=ACTION,
            actor_user_id=actor.id,resource_type='business_description',resource_id=0,
            response_json=json.dumps({**result,'changes':current['entries']},ensure_ascii=False)))
        db.flush()
        return {**result,'changes':current['entries']}


def other_matches(db):
    """Report remaining business text matches; audit/source originals are evidence."""
    immutable = ('operation_logs', 'master_data_object_versions', 'finance_idempotency_records')
    schema = inspect(db.connection()); result = []
    for table in schema.get_table_names():
        if table in immutable or table.startswith(('sqlite_', 'pdf_', 'legacy_')):
            continue
        for column in schema.get_columns(table):
            field = column['name']
            if field in FIELDS.get(table, ()) or not any(t in str(column['type']).upper() for t in ('CHAR','TEXT','JSON')):
                continue
            # Names originate only in inspected application metadata.
            count = db.execute(text(f'SELECT count(*) FROM "{table}" WHERE "{field}" LIKE :pattern'),
                               {'pattern':'%'+PHRASE+'%'}).scalar()
            if count: result.append({'table':table,'field':field,'count':count})
    return result
