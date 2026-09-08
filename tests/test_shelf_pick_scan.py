import pytest
from sqlalchemy import select
from fastapi import HTTPException, FastAPI
from fastapi.testclient import TestClient
from app.api import shelf_pick_scan as scan
from app.api.deps import get_db
from app.api.deliveries import _pick_task_response, complete_delivery_pick_task_as_planned, PickStagingBatch
from app.models.user import User
from app.models.fixed_shelf import ShelfProfile, ShelfMutation
from app.models.warehouse_inventory import WarehouseLocation, InventoryLot
from app.services import fixed_shelf as shelf, fixed_shelf_staging as staging
from test_fixed_shelf import setup, rack_factory
from test_fixed_shelf_staging import ground, case


def args(db,pid,lid):
    return dict(l=lid,p=pid,v=db.get(ShelfProfile,pid).version,a=db.get(WarehouseLocation,lid).address_version)


@pytest.mark.parametrize('unordered',[False,True])
def test_scan_marks_picked_once_without_any_inventory_move_then_collects(setup,monkeypatch,unordered):
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target,unordered=unordered)
        user=db.get(User,1)
        label=args(db,pid,ids[0])
        context=scan.scan_context(**label,db=db,user=user)
        assert len(context['tasks'])==1 and context['tasks'][0]['problem'] is None
        payload=scan.ScanPayload(**label,task_id=task.id,print_version=context['tasks'][0]['print_version'],idempotency_key='scan-1')
        before=(lot.warehouse_location_id,lot.quantity_available,lot.quantity_reserved,lot.version)
        result=scan.confirm_scan(payload,db,user)
        assert result['quantity']==50 and result['inventory_moved'] is False
        assert item.status=='picked' and item.picked_quantity==50 and task.status=='pushed'
        assert before==(lot.warehouse_location_id,lot.quantity_available,lot.quantity_reserved,lot.version)
        assert not staging.staged_lots(db,line.id)
        from app.services.warehouse_inventory import WarehouseInventoryError
        with pytest.raises(WarehouseInventoryError):
            staging.require_staged_dispatch(db,[line])
        assert scan.confirm_scan(payload,db,user)==result
        payload.idempotency_key='another-camera-scan'
        assert scan.confirm_scan(payload,db,user)['already_picked']
        assert len(db.scalars(select(InventoryLot)).all())==1
        response=_pick_task_response(db,task)
        complete_delivery_pick_task_as_planned(task.id,PickStagingBatch(print_version=response['print_version'],
            targets={item.id:shelf.staging_info(db,target)}),db,user)
        assert sum(shelf.physical_quantity(x) for x in staging.staged_lots(db,line.id))==50


def test_old_label_stale_task_wrong_shelf_and_other_employee_fail_closed(setup,monkeypatch):
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target)
        user=db.get(User,1);label=args(db,pid,ids[0])
        with pytest.raises(HTTPException) as e:
            scan.scan_context(**{**label,'v':label['v']+1},db=db,user=user)
        assert e.value.status_code==409
        payload=scan.ScanPayload(**label,task_id=task.id,print_version='stale',idempotency_key='old-task')
        with pytest.raises(HTTPException):scan.confirm_scan(payload,db,user)
        assert item.status=='pending' and item.picked_quantity==0
        worker=User(username='other-driver',password_hash='unused',role='delivery_picker',real_name='其他员工')
        db.add(worker);db.commit()
        assert scan.scan_context(**label,db=db,user=worker)['tasks']==[]
        with pytest.raises(HTTPException) as e:scan.confirm_scan(payload,db,worker)
        assert e.value.status_code==404
        # A different valid shelf binding does not prove goods were taken from the allocated shelf.
        from test_fixed_shelf import configure
        configure(db,pid,ids[:2],version=1)
        db.commit()
        wrong=args(db,pid,ids[1])
        context=scan.scan_context(**wrong,db=db,user=user)
        assert context['tasks'][0]['problem']
        payload=scan.ScanPayload(**wrong,task_id=task.id,print_version=context['tasks'][0]['print_version'],idempotency_key='wrong-shelf')
        with pytest.raises(HTTPException):scan.confirm_scan(payload,db,user)
        assert item.picked_quantity==0 and lot.warehouse_location_id==ids[0]


def test_scan_get_and_anonymous_request_cannot_mark_goods(setup,monkeypatch):
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target)
        label=args(db,pid,ids[0]);user=db.get(User,1)
        scan.scan_context(**label,db=db,user=user)
        assert item.status=='pending' and not db.scalars(select(ShelfMutation)).all()
    app=FastAPI();app.include_router(scan.router,prefix='/api/shelf-pick-scan')
    def session():
        with factory() as db:yield db
    app.dependency_overrides[get_db]=session
    with TestClient(app) as client:
        assert client.get('/api/shelf-pick-scan/context',params=label).status_code==401
        assert client.post('/api/shelf-pick-scan/confirm',json={**label,'task_id':task.id,'print_version':'x','idempotency_key':'unauthorized'}).status_code==401


def test_camera_page_requires_task_selection_when_ambiguous_or_saved_task_mismatches(tmp_path):
    from pathlib import Path
    import subprocess
    source=(Path(__file__).resolve().parents[1]/'static/shelf-pick-scan.html').read_text(encoding='utf-8')
    boot=source[source.index('async function boot()'):source.index("$('retry').onclick")]
    script='''
const nodes=new Map(); const $=id=>{if(!nodes.has(id))nodes.set(id,{querySelectorAll:()=>[]});return nodes.get(id)};
const label={l:2,p:1,v:1,a:1}; let context=null,busy=false,saved=0,tasks=[],confirmed=[];
const esc=x=>x; const remembered=()=>saved; const request=async()=>({tasks});
const confirm=async t=>confirmed.push(t.id); const failure=e=>{$('result').textContent=e.message};
'''+boot+'''
(async()=>{
tasks=[{id:1}];await boot();if(confirmed.join()!=='1')throw Error('unique task should auto-confirm');
confirmed=[];tasks=[{id:1},{id:2}];await boot();if(confirmed.length)throw Error('ambiguous tasks must not auto-confirm');
saved=2;await boot();if(confirmed.join()!=='2')throw Error('explicit current task must be respected');
confirmed=[];tasks=[{id:1}];await boot();if(confirmed.length)throw Error('saved task mismatch must not switch automatically');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    path=tmp_path/'scan-task-selection.js';path.write_text(script,encoding='utf-8')
    result=subprocess.run(['node',str(path)],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
