from fastapi.testclient import TestClient
from sqlalchemy import select,func
from tests.test_mold_tool_workflow import mold_app,_login,_protected_business_state
from tests.test_p1_58_mold_label_print_status import _printable_mold
from app.models.mold_tool import MoldTool,MoldLabelPrintJob


def test_selection_is_read_only_and_distinguishes_reprint_and_invalid_rows(mold_app):
    app,factory=mold_app
    first,*_=_printable_mold(factory,suffix="1")
    second,*_=_printable_mold(factory,suffix="2")
    with factory() as db:
        bad=MoldTool(mold_code="NO-BINDING",mold_name="待补资料",rack_location="1F-M-R01-L1-G03")
        db.add(bad);db.commit();bad_id=bad.id
    with TestClient(app) as client:
        _login(client,"workshop")
        print_payload={"mold_ids":[first],"source":"batch","template_version":"mold_80x40_v1","idempotency_key":"selection-initial-print"}
        initial=client.post("/api/warehouse/molds/label-prints",json=print_payload)
        assert initial.status_code==200,initial.text
        protected=_protected_business_state(factory)
        result=client.get("/api/warehouse/molds/label-options",params={"mold_ids":f"{second},{first},{bad_id}"})
        assert result.status_code==200,result.text
        assert 'no-store' in result.headers['cache-control']
        items=result.json()['items'];assert [r['id'] for r in items]==[second,first,bad_id]
        assert items[0]['printable'] and not items[0]['label_print_status']['printed']
        assert items[1]['printable'] and items[1]['label_print_status']['print_count']==1
        assert not items[2]['printable'] and '绑定' in items[2]['printability_error']
        with factory() as db: assert db.scalar(select(func.count(MoldLabelPrintJob.id)))==1
        assert _protected_business_state(factory)==protected
        reprint=client.post("/api/warehouse/molds/label-prints",json={**print_payload,"idempotency_key":"selection-explicit-reprint"})
        assert reprint.status_code==200,reprint.text
        assert reprint.json()['print_job_id']!=initial.json()['print_job_id']
        again=client.post("/api/warehouse/molds/label-prints",json={**print_payload,"idempotency_key":"selection-explicit-reprint"})
        assert again.json()['print_job_id']==reprint.json()['print_job_id']
        with factory() as db: assert db.scalar(select(func.count(MoldLabelPrintJob.id)))==2
        assert _protected_business_state(factory)==protected


def test_selection_rejects_invalid_ids_and_unauthorized_access(mold_app):
    app,_=mold_app
    with TestClient(app) as client:
        assert client.get('/api/warehouse/molds/label-options',params={'mold_ids':'1'}).status_code==401
        _login(client,'workshop')
        for ids in ['abc','0','-1','１',','.join(str(i) for i in range(1,502))]:
            assert client.get('/api/warehouse/molds/label-options',params={'mold_ids':ids}).status_code==422
        assert client.get('/api/warehouse/molds/label-options',params={'mold_ids':'99999999'}).status_code==404


def test_customer_scoped_operator_cannot_read_physical_label_selection(mold_app):
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    app,factory=mold_app
    first,*_=_printable_mold(factory,suffix="1")
    with factory() as db:
        user=db.scalar(select(User).where(User.username=="workshop"))
        user.customer_access_mode="selected"
        db.add(UserCustomerScope(user_id=user.id,customer_id=1));db.commit()
    with TestClient(app) as client:
        _login(client,"workshop")
        response=client.get('/api/warehouse/molds/label-options',params={'mold_ids':str(first)})
        assert response.status_code==403,response.text
