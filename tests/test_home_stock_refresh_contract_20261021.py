"""Stock-only refresh must preserve approval eligibility and customer scope."""
from copy import deepcopy
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.user import User
from app.models.business_approval import BusinessApproval
from app.models.access_control import UserPermissionOverride
from app.services.dashboard_workbench import build_workbench
from tests.test_n028_dashboard_permissions import _seed_dashboard_data


def test_stock_refresh_preserves_full_home_approval_and_labels_without_foreign_requests(tmp_path,monkeypatch):
 from app.api import dashboard
 engine=create_sqlite_engine(tmp_path/'refresh.sqlite3');Base.metadata.create_all(engine)
 factory=sessionmaker(bind=engine,expire_on_commit=False);ids=_seed_dashboard_data(factory)
 with factory() as db:
  user=db.get(User,ids['sales']);other=db.get(User,ids['finance'])
  for code in ['warehouse.view','requisition.view','business_requests.submit']:
   user.permission_overrides.append(UserPermissionOverride(permission_code=code,is_allowed=True))
  own=db.scalar(select(Customer).where(Customer.name=='Allowed customer'));foreign=db.scalar(select(Customer).where(Customer.name=='Excluded customer'))
  own.chinese_short_name='测试简称'
  def request(actor,customer,key):
   row=BusinessApproval(applicant_id=actor,customer_id=customer,action='stock_replenishment',payload_json='{"items":[{"stock_policy_id":8}]}',basis_hash='a'*64,request_hash='b'*64,idempotency_key=key);db.add(row);db.flush();return row.id
  expected=request(user.id,own.id,'own');request(other.id,own.id,'other-user');request(user.id,foreign.id,'foreign-customer');db.commit()
  original=[dict(customer_id=own.id,customer_name=own.name,policy_id=8)]
  monkeypatch.setattr(dashboard,'_common_box_low_stock_warnings',lambda _db,scope:deepcopy(original) if scope is None or own.id in scope else [])
  full=deepcopy(original);build_workbench(db,user=user,data={'rows':{},'snapshot':{'statement_month':'2026-10'}},warnings=full,today=date.today(),visible_customer_ids={own.id})
  partial=dashboard.stock_warnings(db=db,user=user)['items']
  assert partial==full
  assert partial[0]['pending_request_ids']==[expected]
  assert partial[0]['customer_label']=='测试简称'
  assert not db.dirty and not db.new
 engine.dispose()
