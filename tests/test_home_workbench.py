from datetime import date
from pathlib import Path
import json

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.business_approval import BusinessApproval
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.access_control import UserPermissionOverride
from app.services.dashboard_workbench import build_workbench
from tests.test_n028_dashboard_permissions import _seed_dashboard_data


def test_workbench_scope_quantities_units_and_read_only(tmp_path: Path):
    engine=create_sqlite_engine(tmp_path/'home.sqlite3');Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False);ids=_seed_dashboard_data(factory)
    with factory() as db:
        user=db.get(User,ids['sales'])
        user.permission_overrides.append(UserPermissionOverride(permission_code='business_requests.submit',is_allowed=True))
        customer=db.scalar(select(Customer).where(Customer.name=='Allowed customer'))
        other=db.scalar(select(Customer).where(Customer.name=='Excluded customer'))
        order=db.scalar(select(Order).where(Order.customer_id==customer.id));item=db.scalar(select(OrderItem).where(OrderItem.order_id==order.id))
        item.sales_unit_snapshot='套';item.quantity=100;item.delivered_quantity=20;db.commit()
        sources={'pending_delivery':[{'customer_id':customer.id,'workbench_items':[{'item_id':item.id,'ready_quantity':35}]},
          {'customer_id':other.id,'workbench_items':[]}],
          'pending_material':[{'customer_id':None,'_workbench_customer_ids':[customer.id], 'item_id':'mg3','merge_group_id':3,'product_code':'ABC'}]}
        result=build_workbench(db,user=user,data={'rows':sources,'snapshot':{'statement_month':'2026-10'}},warnings=[],today=date.today(),visible_customer_ids={customer.id})
        assert [c['id'] for c in result['customers']]==[customer.id]
        delivery=next(t for t in result['tasks'] if t['key']=='pending_delivery')
        assert delivery['message']=='待交 80套 · 可送 35套'
        assert delivery['action_text']=='提交送货申请'
        assert delivery['order_id']==order.id
        merged=next(t for t in result['tasks'] if t['key']=='pending_material')
        assert merged['source_item_id']=='mg3' and merged['customer_ids']==[customer.id]
        assert 'unit_price' not in json.dumps(result) and 'Excluded' not in json.dumps(result)
        assert not db.dirty and not db.new
        empty=build_workbench(db,user=user,data={'rows':sources,'snapshot':{'statement_month':'2026-10'}},warnings=[],today=date.today(),visible_customer_ids=set())
        assert empty['tasks']==[] and empty['customers']==[]


def test_pending_approvals_respect_applicant_customer_boss_action_and_policy(tmp_path: Path):
    from app.api.business_approvals import list_requests
    engine=create_sqlite_engine(tmp_path/'approvals.sqlite3');Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False);ids=_seed_dashboard_data(factory)
    with factory() as db:
        sales=db.get(User,ids['sales']);other_user=db.get(User,ids['finance'])
        sales.permission_overrides.append(UserPermissionOverride(permission_code='business_requests.submit',is_allowed=True))
        customer=db.scalar(select(Customer).where(Customer.name=='Allowed customer'))
        excluded=db.scalar(select(Customer).where(Customer.name=='Excluded customer'))
        def request(applicant,cid,action):
            row=BusinessApproval(applicant_id=applicant,customer_id=cid,action=action,payload_json='{"items":[{"stock_policy_id":8}]}',basis_hash='a'*64,request_hash='b'*64,idempotency_key=f'{applicant}-{cid}-{action}')
            db.add(row);db.flush();return row
        own=request(sales.id,customer.id,'stock_replenishment')
        foreign=request(other_user.id,customer.id,'stock_replenishment')
        request(sales.id,excluded.id,'stock_replenishment');request(sales.id,customer.id,'product_update');db.commit()
        warning={'customer_id':customer.id,'policy_id':8};data={'rows':{},'snapshot':{'statement_month':'2026-10'}}
        result=build_workbench(db,user=sales,data=data,warnings=[warning],today=date.today(),visible_customer_ids={customer.id})
        assert len(result['tasks'])==2 and warning['pending_request_ids']==[own.id]
        assert list_requests(status='',page=1,request_id=foreign.id,db=db,user=sales)['total']==0
        assert list_requests(status='',page=1,request_id=own.id,db=db,user=sales)['total']==1
        sales.role='boss';db.flush()
        result=build_workbench(db,user=sales,data=data,warnings=[],today=date.today(),visible_customer_ids={customer.id})
        assert {r['request_id'] for r in result['tasks']}=={own.id,foreign.id}


def test_home_focus_cannot_select_another_customers_pending_item(tmp_path: Path):
    from app.api.requisition import pending_requisitions
    from app.api.incoming import pending_items
    from fastapi import Response
    engine=create_sqlite_engine(tmp_path/'focus.sqlite3');Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False);ids=_seed_dashboard_data(factory)
    with factory() as db:
        user=db.get(User,ids['sales']);items={}
        for item,order,customer in db.execute(select(OrderItem,Order,Customer).join(Order,Order.id==OrderItem.order_id).join(Customer,Customer.id==Order.customer_id)):
            order.status='pending_production';item.material_status='pending';item.requisition_status='未报料'
            item.snapshot_report_length_mm=500;item.snapshot_report_width_mm=300;item.special_process='一开一'
            items[customer.name]=item
        db.commit()
        own=items['Allowed customer'];foreign=items['Excluded customer']
        result=pending_requisitions(db=db,_user=user,page=1,page_size=25,home_item_id=str(own.id))
        assert len(result['items'])==1 and result['items'][0]['item_id']==own.id
        assert pending_requisitions(db=db,_user=user,page=1,page_size=25,home_item_id=str(foreign.id))['items']==[]
        for item in items.values():item.requisition_status='已报料'
        db.commit()
        result=pending_items(response=Response(),db=db,user=user,page=1,page_size=25,home_item_id=str(own.id))
        assert len(result['items'])==1 and result['items'][0]['item_id']==own.id
        assert pending_items(response=Response(),db=db,user=user,page=1,page_size=25,home_item_id=str(foreign.id))['items']==[]
        from app.models.production import ProductionTask
        from app.api.production import get_production_tasks
        tasks=[]
        for item in items.values():
            item.material_status='received'
            task=ProductionTask(order_item_id=item.id,status='pending',planned_quantity=10,ordered_quantity_snapshot=10,
                material_received_quantity=10,material_input_quantity=10,output_factor=1,printing_plate_mode_snapshot='plate',printing_plate_codes_snapshot='[]',version=1)
            db.add(task);tasks.append(task)
        db.commit()
        own_task=next(t for t in tasks if t.order_item_id==own.id)
        foreign_task=next(t for t in tasks if t.order_item_id==foreign.id)
        assert get_production_tasks(q='',task_status='pending',page=1,page_size=25,home_task_id=own_task.id,db=db,user=user)['total']==1
        assert get_production_tasks(q='',task_status='pending',page=1,page_size=25,home_task_id=foreign_task.id,db=db,user=user)['items']==[]
