"""Full migration inventories must survive dedicated management and backup."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest
from desktop_assistant import mac_service_manage as m
from desktop_assistant import migration
from desktop_assistant.storage import write_json
from test_mac_service_manage import instance

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac private file descriptors and ACL leases')


@pytest.fixture
def large_state(instance):
    a=instance
    data=a.root/'shared'/'data';data.mkdir()
    for index in range(2991):
        (data/f'synthetic-{index:05d}.pdf').write_bytes(b'synthetic attachment only')
    inventory=migration.files(a.root/'shared')
    state={**a.manager.state,'migration_files':inventory,'operation':'update',
           'migration_backup_sha256':'a'*64,'historical_metadata':{'preserve':['unchanged',2991]}}
    write_json(a.root/'state.json',state)
    assert len(inventory)==2991 and (a.root/'state.json').stat().st_size>65536
    return a,deepcopy(state)


def test_full_inventory_status_has_no_io_side_effect_or_inventory_leak(large_state):
    a,state=large_state;before=(a.root/'state.json').read_bytes()
    result=a.run('status')
    assert result['snapshot']['current']==state['current']
    assert (a.root/'state.json').read_bytes()==before
    assert 'migration_files' not in json.dumps(result) and 'synthetic-' not in json.dumps(result)
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()


def test_full_inventory_survives_stop_and_start_without_dropping_history(large_state):
    a,state=large_state
    a.run('start');assert a.manager.state=={**state,'manual_stop':False}
    a.run('stop');assert a.manager.state==state
    assert a.manager.starts==a.manager.stops==1
    assert migration.files(a.root/'shared')==state['migration_files']


def test_full_inventory_configure_and_backup_preserve_all_history(large_state):
    a,state=large_state
    a.run('configure-backup');assert a.manager.state==state
    result=a.run('backup')
    assert result['snapshot']['backup_verified_at']
    actual=a.manager.state
    for key,value in state.items():assert actual[key]==value
    assert migration.files(a.root/'shared')==state['migration_files']


@pytest.mark.parametrize('raw',[
    b'{"current":"a","current":"b"}',b'{"nested":{"same":1,"same":2}}',
    b'{"number":NaN}',b'{"number":Infinity}',b'{"number":-Infinity}',
    b'{"number":1e999}',b'[]',b'null',b'{"bad":"\xff"}',b'{"incomplete":',b'',
])
def test_state_decoder_retains_strict_json_rejection(raw):
    with pytest.raises(ValueError):m.decode_record(raw,'state.json')


def test_only_state_has_larger_bound_small_records_and_requests_unchanged(instance):
    a=instance;raw=m.boot.canonical({'synthetic':'x'*65536})
    assert m.STATE_LIMIT==16*1024**2 and m.record_limit('state.json')==m.STATE_LIMIT
    assert m.decode_record(raw,'state.json')['synthetic']=='x'*65536
    for name in ('preferences.json','service-restore-example.json','state.json.pending'):
        assert m.record_limit(name)==65536
        (a.root/name).write_bytes(raw)
        with m.instance(a.context) as (folders,lock,recheck):
            with pytest.raises(ValueError):m.record(folders['root'],name,a.context)
    with pytest.raises(ValueError):m.boot.decode(raw)


@pytest.mark.parametrize('name',['state.json','preferences.json'])
def test_oversized_proposed_state_or_settings_rejected_before_pending_write(instance,monkeypatch,name):
    a=instance
    if name=='state.json':monkeypatch.setattr(m,'STATE_LIMIT',65536)
    before=(a.root/name).read_bytes();inventory={p.name for p in a.root.iterdir()}
    value=json.loads(before);value['synthetic']='x'*65536
    with m.instance(a.context) as (folders,lock,recheck):
        with m.maintenance(lock),pytest.raises(ValueError):
            m.replace_record(folders['root'],name,value,before,a.context,recheck)
    assert (a.root/name).read_bytes()==before and {p.name for p in a.root.iterdir()}==inventory


def test_actual_state_limit_rejects_sparse_oversized_file_before_read(instance,monkeypatch):
    a=instance;path=a.root/'state.json'
    with path.open('r+b') as f:f.truncate(m.STATE_LIMIT+1)
    import desktop_assistant.mac_service_restore as restore
    original=restore.os.read
    def guarded(fd,size):
        assert os.fstat(fd).st_ino!=path.stat().st_ino,'oversized state must not be read'
        return original(fd,size)
    monkeypatch.setattr(restore.os,'read',guarded)
    with m.instance(a.context) as (folders,lock,recheck):
        with pytest.raises(ValueError,match='超限'):m.record(folders['root'],'state.json',a.context)
    assert path.stat().st_size==m.STATE_LIMIT+1


def test_state_limit_is_measured_in_encoded_bytes(monkeypatch):
    raw=json.dumps({'preserve':'\u4e2d'*200},ensure_ascii=False).encode('utf-8')
    monkeypatch.setattr(m,'STATE_LIMIT',len(raw))
    assert m.decode_record(raw,'state.json')=={'preserve':'\u4e2d'*200}
    with pytest.raises(ValueError):m.decode_record(raw+b' ','state.json')


def test_large_state_concurrent_update_never_overwrites_new_history(large_state):
    a,state=large_state;previous=(a.root/'state.json').read_bytes();calls=[]
    with m.instance(a.context) as (folders,lock,recheck):
        def changed():
            recheck();calls.append(True)
            if len(calls)==2:write_json(a.root/'state.json',{**state,'concurrent_admin_note':'preserve'})
        with m.maintenance(lock),pytest.raises(ValueError,match='并发变化'):
            m.replace_record(folders['root'],'state.json',{**state,'manual_stop':False},previous,a.context,changed)
    assert a.manager.state=={**state,'concurrent_admin_note':'preserve'}
    assert list(a.root.glob('state-*.pending.json'))


def test_large_failed_migration_remains_stopped_and_retains_inventory(large_state):
    a,state=large_state;state['operation']='migration_failed';write_json(a.root/'state.json',state)
    assert a.run('status')['snapshot']['maintenance_attention']
    with pytest.raises(ValueError,match='未决维护'):a.run('start')
    a.run('stop')
    assert a.manager.starts==0 and a.manager.state==state
