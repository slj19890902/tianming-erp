from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import zipfile
import pytest
from desktop_assistant import local_storage_policy as p


def test_protection_is_current_role_closure_not_previous_required_union():
    a,b,c,stale=('a'*64,'b'*64,'c'*64,'d'*64)
    assert p.protected_programs({'current':a}, {a:{'migration':{'rollback_package_sha256':b}},b:{}}, additional=[c])=={a,b,c}
    assert stale not in p.protected_programs({'current':a}, {a:{}})


def test_invalid_fallback_stops_program_retirement():
    with pytest.raises(ValueError):
        p.protected_programs({'current':'a'*64},{'a'*64:{'migration':{'rollback_package_sha256':'../escape'}}})


def test_expansions_do_not_accumulate_transitive_signed_fallbacks():
    a,b,c,d,e,f=('a'*64,'b'*64,'c'*64,'d'*64,'e'*64,'f'*64)
    state={'current':a,'previous':b,'schema_authority':c}
    manifests={a:{'migration':{'rollback_package_sha256':b}},
               b:{'migration':{'rollback_package_sha256':d}},
               d:{'migration':{'rollback_package_sha256':f}}}
    assert p.protected_programs(state,manifests,additional=[e])=={a,b,c,d,e,f}
    assert p.expanded_programs(state,additional=[e],point_programs=[d])=={a,b,c,d,e}
    assert f not in p.expanded_programs(state,additional=[e],point_programs=[d])


def test_missing_current_still_refuses_expansion_retirement():
    with pytest.raises(ValueError):p.expanded_programs({'previous':'b'*64})


def test_rehearsal_recovery_requires_self_contained_v2_schema_and_completed_source():
    r=dict(scope='isolated_additive_rehearsal_only',package_sha256='a'*64,status='passed',
           source_unchanged=True,schema_fingerprint_version=2,result_schema='b'*64)
    assert p.rehearsal_payload_scope(r,delivered=False,ancestor=True,later_passed=False)=='release-and-shared'
    assert p.rehearsal_payload_scope(r,delivered=False,ancestor=False,later_passed=False) is None
    assert p.rehearsal_payload_scope(dict(r,schema_fingerprint_version=1),delivered=True,ancestor=False,later_passed=False) is None


def test_failed_copy_only_retires_data_after_later_success_of_delivered_package():
    r=dict(scope='isolated_additive_rehearsal_only',package_sha256='a'*64,status='failed',
           error='复制期间数据变化或附件不一致，请重新演练')
    assert p.rehearsal_payload_scope(r,delivered=True,ancestor=False,later_passed=True)=='shared-only'
    assert p.rehearsal_payload_scope(r,delivered=True,ancestor=False,later_passed=False) is None
    assert p.rehearsal_payload_scope(r,delivered=False,ancestor=False,later_passed=True) is None
    assert p.rehearsal_payload_scope(dict(r,error='structural migration failed'),delivered=True,ancestor=True,later_passed=True) is None


@pytest.mark.parametrize('extra',[{'manual_stop':True},{'onboarding_pending':True},
    {'backup_work_retained':['one']},{'operation':'migration_running'}, {'operation':'update_failed'}])
def test_incomplete_or_failed_operation_has_no_cleanup(extra):
    state={'current':'a'*64,'operation':'update',**extra}
    assert not p.maintenance_admitted(state,running=True,healthy=True)


def test_unhealthy_or_missing_process_has_no_cleanup():
    state={'current':'a'*64,'operation':'update'}
    assert not p.maintenance_admitted(state,running=False,healthy=True)
    assert not p.maintenance_admitted(state,running=True,healthy=False)


def test_failure_never_rotates_last_verified_points():
    rows=[dict(path=str(i),created=f'2026-10-{i+1:02}',verified=True) for i in range(4)]
    assert p.point_retirements(rows,keep=3,new_verified=False)==[]
    assert [x['path'] for x in p.point_retirements(rows,keep=3,new_verified=True)]==['0']
    assert p.point_retirements(rows,keep=0,new_verified=True)==[]


def test_unknown_point_cannot_be_selected_for_rotation():
    rows=[dict(path='unknown',created='2020',verified=False),dict(path='new',created='2026',verified=True)]
    assert p.point_retirements(rows,keep=1,new_verified=True)==[]


def test_concurrent_file_replacement_is_not_unlinked(tmp_path):
    f=tmp_path/'package.zip';f.write_bytes(b'original');identity=p.fingerprint(f)
    gate=threading.Event();done=threading.Event()
    def replace():
        gate.wait();q=tmp_path/'replacement';q.write_bytes(b'unfinished work');q.replace(f);done.set()
    t=threading.Thread(target=replace);t.start()
    def before():gate.set();assert done.wait(5)
    with pytest.raises(ValueError):p.unlink_verified(f,identity,before_unlink=before)
    t.join();assert f.read_bytes()==b'unfinished work'


def test_file_replaced_with_symlink_is_not_followed(tmp_path):
    f=tmp_path/'point';f.write_bytes(b'old');identity=p.fingerprint(f);target=tmp_path/'business';target.write_bytes(b'keep')
    f.unlink();f.symlink_to(target)
    with pytest.raises(ValueError):p.unlink_verified(f,identity)
    assert target.read_bytes()==b'keep' and f.is_symlink()


def test_unknown_links_prevent_tree_cleanup(tmp_path):
    root=tmp_path/'tree';root.mkdir();(root/'escape').symlink_to(tmp_path)
    with pytest.raises(ValueError):p.tree_fingerprint(root,uid=os.getuid(),device=root.stat().st_dev)


def test_tree_modification_changes_completion_fingerprint(tmp_path):
    root=tmp_path/'tree';root.mkdir();f=root/'report';f.write_bytes(b'one')
    a=p.tree_fingerprint(root,uid=os.getuid(),device=root.stat().st_dev);f.write_bytes(b'new work')
    assert p.tree_fingerprint(root,uid=os.getuid(),device=root.stat().st_dev)!=a


def test_success_receipt_cannot_be_inferred_from_name_or_age():
    assert not p.receipt_valid({'status':'published'},'a'*64,'b'*40)
    receipt=dict(type='tianming.runtime-delivery.v1',package_sha256='a'*64,git_sha='b'*40,runtime_verified=True,technical_delivery_complete=True)
    assert p.receipt_valid(receipt,'a'*64,'b'*40)
    assert not p.receipt_valid(receipt,'c'*64,'b'*40)


def test_unsigned_extra_unique_file_is_protected(tmp_path):
    manifest={'files':{'main.py':hashlib.sha256(b'source').hexdigest()}}
    archive=tmp_path/'unsigned.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('manifest.json',json.dumps(manifest));z.writestr('main.py',b'source')
    assert p.unsigned_equivalent(archive,manifest)
    with zipfile.ZipFile(archive,'a') as z:z.writestr('unique.txt','unfinished')
    assert not p.unsigned_equivalent(archive,manifest)


def test_monthly_archive_rule_does_not_override_runtime_roles():
    rows=[dict(identity='old',created='2026-09-01'),dict(identity='early',created='2026-10-01'),dict(identity='late',created='2026-10-15')]
    assert p.monthly_programs(rows,limit=1)=={'late'}
    assert p.monthly_programs(rows,limit=12)=={'late','old'}


def test_backup_due_boundary_and_not_due():
    now=datetime.now(timezone.utc)
    assert p.full_point_due(None,now,86400)
    assert not p.full_point_due(now-timedelta(hours=1),now,86400)
    assert p.full_point_due(now-timedelta(days=1),now,86400)


def test_launchd_entry_has_only_fixed_scopes_and_no_shell():
    import importlib.util
    source=Path(__file__).resolve().parents[2]/'scripts/local_maintenance/install.py'
    spec=importlib.util.spec_from_file_location('bounded_installer',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for action,interval in [('cleanup',3600),('backup',900)]:
        definition=module.plist_for(action)
        assert definition['StartInterval']==interval
        assert definition['ProgramArguments']==['/usr/bin/python3','-I','-S','-B','/Library/TianmingERP-LocalMaintenance/launcher.py',action]
        assert definition['LowPriorityIO'] is True


def test_closed_release_report_requires_full_matching_proof():
    import copy
    identity,commit,manifest_hash,backup='a'*64,'b'*40,'c'*64,'d'*64
    manifest=dict(git_sha=commit,version='v692',revision='head')
    record=dict(status='technical_release_and_native_closure_complete',healthy=True,
        manual_stop=False,backup_work_retained=False,operation='update',
        transaction=dict(target=identity,commit=commit,manifest_sha256=manifest_hash,version='v692',revision='head'),
        service_result=dict(package=identity,commit=commit,version='v692',revision='head',backup_sha256=backup),
        closure=dict(status='closed',current=identity,backup_preserved=True,backup_sha256=backup))
    check=lambda r:p.completed_delivery_proof(r,manifest,manifest_sha256=manifest_hash)
    assert check(record)
    for key,value in [('healthy',False),('manual_stop',True),('backup_work_retained',True),
                      ('status','published'),('operation','update_failed')]:
        changed=copy.deepcopy(record);changed[key]=value;assert not check(changed)
    for field,key,value in [('transaction','manifest_sha256','e'*64),('transaction','commit','e'*40),
                            ('service_result','package','e'*64),('service_result','version','v691'),
                            ('closure','backup_preserved',False),('closure','backup_sha256','e'*64)]:
        changed=copy.deepcopy(record);changed[field][key]=value;assert not check(changed)
    assert not check({'status':'passed'})


def test_cleanup_contention_retries_without_changing_backup_cadence():
    assert p.maintenance_exit_code('cleanup',{'status':'maintenance-busy'})==75
    assert p.maintenance_exit_code('backup',{'status':'maintenance-busy'})==0
    assert p.maintenance_exit_code('cleanup',{'status':'completed'})==0
    assert p.maintenance_exit_code('cleanup',{'status':'failed'})==1
    import importlib.util
    source=Path(__file__).resolve().parents[2]/'scripts/local_maintenance/install.py'
    spec=importlib.util.spec_from_file_location('retry_installer',source)
    installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    cleanup=installer.plist_for('cleanup')
    assert cleanup['KeepAlive']=={'SuccessfulExit':False} and cleanup['ThrottleInterval']==300
    assert cleanup['StartInterval']==3600
    assert 'KeepAlive' not in installer.plist_for('backup')


def test_managed_seal_matches_runner_and_rejects_links_or_modified_copies(tmp_path):
    from desktop_assistant.task_retention import copy_identity
    root=tmp_path/'copies';root.mkdir();(root/'data').write_bytes(b'disposable')
    identity=p.managed_copy_identity(root,owner_uid=os.getuid(),device=root.stat().st_dev)
    assert identity==copy_identity(root)
    (root/'data').write_bytes(b'changed')
    assert identity!=p.managed_copy_identity(root,owner_uid=os.getuid(),device=root.stat().st_dev)
    with pytest.raises(ValueError):p.managed_copy_identity(root,owner_uid=os.getuid()+1,device=root.stat().st_dev)
    (root/'link').symlink_to(tmp_path)
    with pytest.raises(ValueError):p.managed_copy_identity(root,owner_uid=os.getuid(),device=root.stat().st_dev)


def test_only_explicit_passed_nonmanual_sealed_test_records_are_eligible(tmp_path):
    job=tmp_path/('task-'+'a'*32)
    record=dict(schema='tianming.test-task.v1',job=str(job),copies=str(job/'copies'),
                status='passed',returncode=0,keep_copy=False,manual_acceptance_pending=False,
                copy_identity=dict(fingerprint='b'*64,logical_bytes=9))
    assert p.sealed_test_record(record,job)
    for key,value in [('status','failed'),('status','running'),('returncode',1),
                      ('keep_copy',True),('manual_acceptance_pending',True),
                      ('copies',str(tmp_path)),('copy_identity',None)]:
        assert not p.sealed_test_record({**record,key:value},job)
    assert not p.sealed_test_record({k:v for k,v in record.items() if k!='keep_copy'},job)


def test_archive_history_does_not_keep_reproducible_expansions_forever():
    current,previous,helper,backup,older,point=('a'*64,'b'*64,'c'*64,'d'*64,'e'*64,'f'*64)
    state=dict(current=current,previous=previous)
    manifests={current:dict(migration=dict(rollback_package_sha256=previous)),
               backup:dict(migration=dict(rollback_package_sha256=older))}
    originals,expansions=p.program_retention_sets(state,manifests,runtime_dependencies=[helper],
                        archive_dependencies=[backup],point_programs=[point])
    assert originals=={current,previous,helper,backup,older,point}
    assert expansions=={current,previous,helper,point}
    assert backup in originals and older in originals
    # A genuine runtime dependency still overrides its historical classification.
    _,expansions=p.program_retention_sets(state,manifests,runtime_dependencies=[backup],archive_dependencies=[backup])
    assert backup in expansions
