"""One reviewed transaction for customer sales references and shared stock.

A reference copies physical production fields only. It creates no stock and
never guesses the customer's selling price, drawing, model or billing data.
Preview performs reads only; allocated product IDs are assigned at confirmation.
"""
import hashlib
import json
import re
from decimal import Decimal
from sqlalchemy import select, update
from app.models.customer import Customer
from app.models.product import Product
from app.models.mold_tool import MoldTool, MoldToolCustomer
from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedGroup, SharedFinishedMutation
from app.services import shared_finished_stock as shared, shared_finished_management as management
from app.services.finished_stock_identity import FIELDS
from app.services.bom_transactions import atomic_bom
from app.services.audit_log import append_audit_event

PHYSICAL_FIELDS = sorted(set(FIELDS + shared.EXTRA_FIELDS + (
    'unit','length_mm','width_mm','height_mm','material_id','legacy_material_text','flute_type','mold_tool_id','die_cut_path')))


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),
        default=lambda v: format(v.normalize(),'f') if isinstance(v,Decimal) else str(v))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def can_reference(db, product):
    return shared._ordinary(db,product)


def _validate_mold(db, source):
    mold = db.get(MoldTool,source.mold_tool_id) if source.mold_tool_id else None
    if source.box_category=='die_cut' and mold is None:
        raise shared._error('模切产品缺少明确的实物模具，不能补建共用销售资料')
    if mold and (not mold.is_active or mold.archive_status!='active' or mold.deleted_at):
        raise shared._error('实物模具已停用或归档，请先核实')
    return mold


def preview(db, *, source_product_id, product_ids, new_customer_ids, lot_ids=(),
            expected_group_version=None, auto_enroll=None, disabled_product_ids=(),
            confirmed_lot_fields=None):
    source=db.get(Product,source_product_id)
    if not can_reference(db,source):
        raise shared._error('缺销售资料时仅支持普通自制产品；BOM需先核对各客户整套与对应子件')
    if source.customer is None or not source.customer.is_active:
        raise shared._error('来源客户已停用，请先核对')
    mold=_validate_mold(db,source)
    member=db.get(SharedFinishedMember,source.id)
    group=db.get(SharedFinishedGroup,member.group_id) if member else None
    if (group.version if group else None)!=expected_group_version:
        raise shared._error('共用组已变化，请刷新后重新核对')
    ids=sorted(set(product_ids)|{source.id}|set(management.member_ids(db,group.id) if group else []))
    customers=sorted(set(new_customer_ids));lids=sorted(set(lot_ids));disabled=sorted(set(disabled_product_ids))
    if not customers or len(ids)+len(customers)>20 or not set(disabled).issubset(ids) or (not group and disabled):
        raise shared._error('请选择需要补建销售资料的客户，共用成员最多20个；停用项须属于原成员',422)
    products=[db.get(Product,pid) for pid in ids]
    if any(p is None for p in products):raise shared._error('共用产品不存在',404)
    if {p.customer_id for p in products}&set(customers) or len({p.customer_id for p in products})!=len(ids):
        raise shared._error('每个客户在同一共用组只能指定一个产品')
    new=[]
    for cid in customers:
        customer=db.get(Customer,cid)
        if customer is None or not customer.is_active:raise shared._error('目标客户不存在或已停用',422)
        if db.scalar(select(Product.id).where(Product.customer_id==cid,Product.product_code==source.product_code,
                Product.deleted_at.is_(None),Product.purged_at.is_(None)).limit(1)):
            raise shared._error('目标客户已有此存货编码的资料，请刷新并选择已有产品，不能重复补建')
        new.append(dict(customer_id=cid,customer_name=customer.name,customer_version=customer.version,
            code=source.product_code,name=source.product_name,sale_unit_price=None,customer_drawing_number=None,
            customer_category=None,customer_model=None))
    if len(ids)>1:
        value=shared.preview(db,product_ids=ids,lot_ids=lids,group_id=group.id if group else None,
            allow_new_members=bool(group),confirmed_lot_fields=confirmed_lot_fields)
    else:
        if member:raise shared._error('原共用组成员不完整')
        value=dict(products=[dict(product_id=source.id,customer_id=source.customer_id,version=source.version,
            code=source.product_code,name=source.product_name,identity_json=shared.member_identity(source),
            product_basis_json=shared.product_basis(source))],lots=shared._preview_lots(db,products,lids,confirmed_lot_fields=confirmed_lot_fields))
    summary=management.product_summary(source)
    proposed=[dict(product_id=-row['customer_id'],customer_name=row['customer_name'],code=row['code'],name=row['name'],
        unit=summary['unit'],spec=summary['spec'],material=summary['material'],process=summary['process'],
        identity_json=shared.member_identity(source),will_create_sales_reference=True) for row in new]
    plan=dict(policy='shared-customer-reference-v1',source_product_id=source.id,source_version=source.version,
        source_customer_id=source.customer_id,source_code=source.product_code,
        physical_fields=json.loads(canonical({f:shared._value(getattr(source,f)) if f!='sheet_cutting_settings' else source.sheet_cutting_settings for f in PHYSICAL_FIELDS})),
        mold=dict(id=mold.id,version=mold.version) if mold else None,
        existing_group_id=group.id if group else None,expected_group_version=expected_group_version,
        existing_product_ids=ids,disabled_product_ids=disabled,auto_enroll=auto_enroll,
        new_sales_references=new,products=value['products']+proposed,lots=value['lots'],
        confirmed_lot_fields=confirmed_lot_fields or {})
    plan['preview_hash']=digest(plan)
    return plan


def confirm(db, *, actor, preview_hash, operation_key, evidence, source='web', backup_receipt=None,
            confirmed_lot_fields=None, **args):
    if source not in {'web','script'} or (source=='web' and (actor is None or not actor.is_active or actor.role!='admin')):
        raise shared._error('仅活动管理员可设置客户共用',403)
    if source=='script' and (not isinstance(backup_receipt,dict) or backup_receipt.get('verified') is not True
            or not re.fullmatch('[0-9a-fA-F]{64}',str(backup_receipt.get('sha256','')))):
        raise shared._error('缺少本次已验证备份，不能执行正式客户共用设置')
    if confirmed_lot_fields and source!='script':raise shared._error('旧批次补充确认只供逐项审阅维护使用',403)
    if not evidence.strip():raise shared._error('请核对本次补建资料和共用范围',422)
    request=canonical(dict(args=args,preview_hash=preview_hash,evidence=evidence.strip(),actor_id=actor.id if actor else None,
        source=source,confirmed_lot_fields=confirmed_lot_fields or {}))
    with atomic_bom(db):
        previous=db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key==operation_key))
        if previous:
            if previous.request_json!=request:raise shared._error('操作标识已用于另一项客户共用设置')
            return {**json.loads(previous.result_json),'replayed':True}
        plan=preview(db,**args,confirmed_lot_fields=confirmed_lot_fields)
        if plan['preview_hash']!=preview_hash:raise shared._error('客户、产品或库存已变化，请重新核对预览')
        source_product=db.get(Product,plan['source_product_id'])
        claimed=db.execute(update(Product).where(Product.id==source_product.id,Product.version==plan['source_version'])
            .values(version=Product.version,updated_at=Product.updated_at))
        if claimed.rowcount!=1:raise shared._error('源产品已变化')
        for row in plan['new_sales_references']:
            claimed=db.execute(update(Customer).where(Customer.id==row['customer_id'],Customer.version==row['customer_version'],
                Customer.is_active.is_(True)).values(version=Customer.version,updated_at=Customer.updated_at))
            if claimed.rowcount!=1:raise shared._error('目标客户已变化')
        db.expire_all()
        plan=preview(db,**args,confirmed_lot_fields=confirmed_lot_fields)
        if plan['preview_hash']!=preview_hash:raise shared._error('补建前资料已变化，请重新核对')
        source_product=db.get(Product,plan['source_product_id']);created=[]
        for row in plan['new_sales_references']:
            product=Product(customer_id=row['customer_id'],product_code=source_product.product_code,
                customer_material_code=source_product.product_code,product_name=source_product.product_name,
                sale_unit_price=None,sale_unit_price_no_tax=None,customer_drawing_number=None,customer_model=None,
                customer_category=None,is_active=True,version=1,
                **{f:getattr(source_product,f) for f in PHYSICAL_FIELDS})
            db.add(product);db.flush();created.append(dict(product_id=product.id,customer_id=product.customer_id,code=product.product_code))
        if plan['mold']:
            mold=db.get(MoldTool,plan['mold']['id'])
            claimed=db.execute(update(MoldTool).where(MoldTool.id==mold.id,MoldTool.version==plan['mold']['version'])
                .values(version=MoldTool.version+1,updated_at=MoldTool.updated_at))
            if claimed.rowcount!=1:raise shared._error('模具资料已变化')
            links=list(db.scalars(select(MoldToolCustomer).where(MoldToolCustomer.mold_tool_id==mold.id)))
            used={link.display_order for link in links};linked={link.customer_id for link in links}
            for row in created:
                if row['customer_id'] not in linked:
                    order=next((i for i in (1,2) if i not in used),None);used.add(order)
                    db.add(MoldToolCustomer(mold_tool_id=mold.id,customer_id=row['customer_id'],display_order=order,
                        created_by=actor.id if actor else None))
            db.flush()
        pids=sorted(plan['existing_product_ids']+[r['product_id'] for r in created]);gid=plan['existing_group_id']
        subkey='shared-access-'+hashlib.sha256(operation_key.encode()).hexdigest()[:48]
        if gid is None:
            value=shared.preview(db,product_ids=pids,lot_ids=args.get('lot_ids',()),
                auto_enroll=bool(args.get('auto_enroll')),confirmed_lot_fields=confirmed_lot_fields)
            gid=shared._apply_confirmed(db,product_ids=pids,lot_ids=args.get('lot_ids',()),
                preview_hash=value['preview_hash'],auto_enroll=bool(args.get('auto_enroll')),
                operation_key=subkey,evidence=evidence,actor=actor,source=source,confirmed_lot_fields=confirmed_lot_fields)['group_id']
        else:
            change_args=dict(group_id=gid,action='members',expected_version=plan['expected_group_version'],
                product_ids=pids,disabled_product_ids=args.get('disabled_product_ids',()))
            value=management.preview_change(db,**change_args)
            management.change(db,**change_args,preview_hash=value['preview_hash'],operation_key=subkey,
                evidence=evidence,actor=actor,source=source)
            if args.get('lot_ids'):
                if confirmed_lot_fields:raise shared._error('旧组批次补充确认须独立审阅，已回滚本次操作')
                change_args=dict(group_id=gid,action='add_lots',expected_version=db.get(SharedFinishedGroup,gid).version,
                    lot_ids=args['lot_ids']);value=management.preview_change(db,**change_args)
                management.change(db,**change_args,preview_hash=value['preview_hash'],operation_key=subkey+'-lots',
                    evidence=evidence,actor=actor,source=source)
        result=dict(group_id=gid,created_sales_references=created,replayed=False)
        db.add(SharedFinishedMutation(group_id=gid,operation_key=operation_key,request_json=request,result_json=canonical(result)))
        append_audit_event(db,event_category='business',result='success',source=source,module_code='warehouse',
            action_code='grant_shared_customer_access',resource='products',actor=actor,
            entity_type='shared_finished_group',entity_id=gid,
            operator_name='老板明确授权的客户共用维护' if source=='script' else None,
            details=dict(preview_hash=preview_hash,source_product_id=source_product.id,
                created_sales_references=created,lot_ids=args.get('lot_ids',()),
                backup_sha256=(backup_receipt or {}).get('sha256'),evidence=evidence,confirmed_lot_fields=confirmed_lot_fields or {}))
        db.flush();return result
