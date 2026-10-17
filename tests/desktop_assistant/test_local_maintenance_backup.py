import importlib.util
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from tests.desktop_assistant import test_recovery as recovery
from desktop_assistant import manager as manager_module,credential_store,data_recovery


@pytest.fixture
def scene(monkeypatch):
    case=recovery.RecoveryTests();case.setUp()
    source=Path(__file__).resolve().parents[2]/'scripts/local_maintenance/native.py'
    spec=importlib.util.spec_from_file_location('isolated_native_maintenance',source)
    native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
    base=case.root/'base';base.mkdir();(base/'publisher-public.pem').write_bytes(case.public)
    points=case.root/'online-points';points.mkdir()
    monkeypatch.setattr(native,'_runtime_tree',case.root/case.manager.state['current']/'migration');monkeypatch.setattr(native,'BASE',base);monkeypatch.setattr(native,'ROOT',case.manager.root);monkeypatch.setattr(native,'POINTS',points)
    monkeypatch.setattr(native.os,'geteuid',lambda:499)
    monkeypatch.setattr(native,'check_point_directory',lambda:None)
    monkeypatch.setattr(native,'health',lambda:True)
    monkeypatch.setattr(native.shutil,'disk_usage',lambda p:SimpleNamespace(free=500*10**9))
    monkeypatch.setattr(native,'config',lambda:dict(backup_enabled=True,database_interval_seconds=900,data_interval_seconds=86400,minimum_free_bytes=150*10**9))
    monkeypatch.setattr(manager_module,'Manager',lambda root,public:case.manager)
    monkeypatch.setattr(case.manager,'_process',lambda:('synthetic',{'pid':123}))
    from desktop_assistant.storage import write_json
    state=case.manager.state;state.update(operation='update',manual_stop=False);write_json(case.manager.root/'state.json',state)
    monkeypatch.setattr(credential_store,'unprotect_backup',lambda reference:recovery.PASSWORD)
    write_json(case.manager.root/'preferences.json',{'protected_password':'synthetic-reference'})
    real_stable=native.policy.stable_file
    monkeypatch.setattr(native.policy,'stable_file',lambda path,uid:real_stable(path,uid=501))
    now=[datetime(2026,10,17,0,tzinfo=timezone.utc)]
    monkeypatch.setattr(native,'utc_now',lambda:now[0])
    try:yield case,native,now
    finally:case.tearDown()


def test_success_creates_both_verified_formats_and_no_service_stop(scene):
    case,native,now=scene
    result=native.service_action('service-backup')
    assert result['status']=='checked' and result['service_stopped'] is False
    points=native.completed_points();assert {p['kind'] for p in points}=={'database','data'}
    assert {p['format'] for p in points}=={data_recovery.TYPE,data_recovery.DATABASE_ONLY_TYPE}
    assert all(p['program_embedded'] is False for p in points)
    assert native.service_action('service-backup')['results']==[{'kind':'database','status':'up-to-date'},{'kind':'data','status':'up-to-date'}]


def test_backup_failure_keeps_every_last_verified_point(scene,monkeypatch):
    case,native,now=scene
    native.service_action('service-backup');before={p['path']:p['sha256'] for p in native.completed_points()}
    now[0]+=timedelta(days=1)
    def failed(*a,**k):raise OSError('synthetic pack failure')
    monkeypatch.setattr(data_recovery,'create',failed)
    result=native.service_action('service-backup')
    assert result['status']=='failed'
    assert {p['path']:p['sha256'] for p in native.completed_points()}==before


def test_verified_rotation_keeps_three_database_points_and_one_data_point(scene):
    case,native,now=scene
    for _ in range(5):
        native.service_action('service-backup');now[0]+=timedelta(days=1)
    points=native.completed_points()
    assert sum(p['kind']=='database' for p in points)==3
    assert sum(p['kind']=='data' for p in points)==1


def test_concurrent_maintenance_produces_no_point(scene):
    case,native,now=scene
    import threading
    entered=threading.Event();release=threading.Event()
    def hold():
        with case.manager.lock():entered.set();release.wait(5)
    t=threading.Thread(target=hold);t.start();assert entered.wait(3)
    try:
        result=native.service_action('service-backup')
        assert result['status']=='maintenance-busy' and native.completed_points()==[]
    finally:release.set();t.join()


def test_pre_release_frequency_pairs_db_with_incremental_files_and_restores(scene,monkeypatch):
    case,native,now=scene
    monkeypatch.setattr(native,'config',lambda:dict(backup_enabled=True,database_interval_seconds=900,
        data_interval_seconds=0,minimum_free_bytes=150*10**9))
    assert native.service_action('service-backup')['status']=='checked'
    points=native.completed_points()
    assert len(points)==1 and points[0]['kind']=='database' and points[0]['attachment_set']
    target=case.root/'isolated-paired-shared'
    answer=native.file_points.restore_isolated(points[0],native.POINTS,recovery.PASSWORD,
        target,case.package,case.public)
    assert answer['verified'] and answer['service_started'] is False
    assert (target/'data/drawing.pdf').read_bytes()==b'synthetic attachment'
    before={p.name for p in (native.POINTS/'objects').iterdir()}
    now[0]+=timedelta(minutes=16)
    report=native.service_action('service-backup')
    assert report['results'][0]['incremental']['new_objects']==0
    assert {p.name for p in (native.POINTS/'objects').iterdir()}==before


def test_changed_attachment_is_retained_with_its_old_database_until_verified_rotation(scene,monkeypatch):
    case,native,now=scene
    monkeypatch.setattr(native,'config',lambda:dict(backup_enabled=True,database_interval_seconds=900,
        data_interval_seconds=0,minimum_free_bytes=150*10**9))
    native.service_action('service-backup')
    oldest=native.completed_points()[0]
    (case.manager.root/'shared/data/drawing.pdf').write_bytes(b'new synthetic attachment')
    now[0]+=timedelta(minutes=16)
    native.service_action('service-backup')
    target=case.root/'old-isolated-shared'
    native.file_points.restore_isolated(oldest,native.POINTS,recovery.PASSWORD,target,case.package,case.public)
    assert (target/'data/drawing.pdf').read_bytes()==b'synthetic attachment'
    for _ in range(3):
        now[0]+=timedelta(minutes=16);native.service_action('service-backup')
    assert len(native.completed_points())==3
    assert not (native.POINTS/oldest['attachment_set']['name']).exists()


def test_file_change_during_database_snapshot_keeps_previous_pair(scene,monkeypatch):
    case,native,now=scene
    native.service_action('service-backup')
    before={p['path']:p['sha256'] for p in native.completed_points()}
    original=data_recovery.create
    def changed(*args,**kwargs):
        answer=original(*args,**kwargs)
        (case.manager.root/'shared/data/drawing.pdf').write_bytes(b'concurrent edit')
        return answer
    monkeypatch.setattr(data_recovery,'create',changed)
    now[0]+=timedelta(minutes=16)
    result=native.service_action('service-backup')
    assert result['status']=='failed'
    assert {p['path']:p['sha256'] for p in native.completed_points()}==before


def test_tampered_object_refuses_restore_and_preserves_last_point(scene):
    case,native,now=scene
    native.service_action('service-backup')
    point=next(p for p in native.completed_points() if p['kind']=='database')
    obj=next((native.POINTS/'objects').glob('*.tmobj'))
    obj.write_bytes(obj.read_bytes()[:-16]+b'x'*16)
    target=case.root/'refused-shared'
    with pytest.raises(ValueError):
        native.file_points.restore_isolated(point,native.POINTS,recovery.PASSWORD,target,case.package,case.public)
    assert not target.exists() and (native.POINTS/point['name']).exists()


def test_failed_capture_unindexed_object_is_authenticated_before_reuse(scene,monkeypatch):
    case,native,now=scene
    original=native.file_points.fingerprint
    calls=[0]
    def unstable(shared):
        value=original(shared);calls[0]+=1
        if calls[0]==2:value['synthetic-change']=[1]
        return value
    monkeypatch.setattr(native.file_points,'fingerprint',unstable)
    with pytest.raises(ValueError):
        native.file_points.capture(case.manager.root/'shared',native.POINTS,recovery.PASSWORD)
    assert list((native.POINTS/'objects').glob('*.tmobj'))
    monkeypatch.setattr(native.file_points,'fingerprint',original)
    descriptor,_,result=native.file_points.capture(case.manager.root/'shared',native.POINTS,recovery.PASSWORD)
    assert descriptor['name'].endswith('.tma') and result['new_objects']==0


def test_installed_pair_proof_is_bound_and_contains_no_business_counts(scene):
    case,native,now=scene
    native.service_action('service-backup')
    answer=native.service_action('service-verify-pair')
    assert answer['verified'] and answer['service_started'] is False
    assert answer['point_name'].endswith('.tmdb')
    assert answer['program_sha256']==case.manager.state['current']
    assert 'database' not in answer


def test_public_plan_remains_readable_under_launchd_umask_and_private_receipt_stays_private(scene):
    import os,stat
    case,native,now=scene
    old=os.umask(0o027)
    try:
        native.write(case.root/'public-plan.json',{'metadata':True},mode=0o644)
        native.write(case.root/'private-receipt.json',{'metadata':True})
    finally:os.umask(old)
    assert stat.S_IMODE((case.root/'public-plan.json').stat().st_mode)==0o644
    assert stat.S_IMODE((case.root/'private-receipt.json').stat().st_mode)==0o600
