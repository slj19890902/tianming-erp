"""Real signatures/trees and synthetic DBs; OS root/UID metadata is simulated.

These tests do not install a daemon or prove actual privileged publication.
"""
from contextlib import contextmanager, closing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import mac_published_runtime as published
from desktop_assistant import migration, release_verification
from desktop_assistant.manager import Manager
from desktop_assistant.release_request import public_identity
from desktop_assistant.storage import extract_verified, pack_tree, read_json, sha, write_json
from tests.desktop_assistant import test_recovery as recovery


@pytest.fixture
def case(monkeypatch):
    fixture = recovery.RecoveryTests()
    fixture.setUp()
    base = fixture.root / 'synthetic-system'
    base.mkdir()
    manager = fixture.manager
    for name, value in dict(BASE=base, ROOT=manager.root, PUBLISHED=base/'published',
                            REGISTRY=base/'published.json', PUBLIC=base/'publisher.pem').items():
        monkeypatch.setattr(published, name, value)
    context = base/'context.json'
    context.write_bytes(b'{"synthetic_context":true}')
    published.PUBLIC.write_bytes(fixture.public)
    monkeypatch.setattr(published.keychain, 'CONTEXT', context)
    monkeypatch.setattr(published.keychain, 'load_context', lambda: {'uid': os.getuid()})
    @contextmanager
    def checked(path, **_):
        fd = os.open(path, os.O_RDONLY)
        try: yield fd
        finally: os.close(fd)
    monkeypatch.setattr(published.keychain, '_checked_path', checked)
    monkeypatch.setattr(published, 'immutable', lambda *a, **kw: None)
    monkeypatch.setattr(release_verification, 'immutable', lambda *a, **kw: None)
    monkeypatch.setattr(published, 'selected', lambda: True)
    monkeypatch.setattr('desktop_assistant.runtime_platform.sys.platform', 'darwin')
    monkeypatch.setattr('desktop_assistant.runtime_platform.platform.machine', lambda: 'arm64')
    identities = []
    def publish(version='native', revision='r1', *, capability=1, service_capability=None, missing=None, contract=None):
        source = fixture.root/('source-'+version)
        shutil.copytree(fixture.root/'source-one', source)
        (source/'runtime/python.exe').unlink()
        runtime = source/'runtime/bin/python3.12'
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b'synthetic interpreter, never executed')
        (source/'desktop_assistant/migration_entry.py').write_text('# synthetic signed entry')
        (source/'runtime/ocr/model').mkdir(parents=True)
        for name in ('craft_mlt_25k.pth', 'zh_sim_g2.pth'):
            (source/'runtime/ocr/model'/name).write_bytes(b'synthetic model')
        from desktop_assistant.ocr_models import prepare_readonly_layout
        prepare_readonly_layout(source/'runtime/ocr/model')
        if missing: (source/missing).unlink()
        package = fixture.root/(version+'.zip')
        metadata = {'type':'tianming.release.v1', 'version':version, 'revision':revision,
                    'runtime_platform':'macos-arm64',
                    'runtime_capabilities':{'immutable_execution_v1':capability},
                    'executable_files':['runtime/bin/python3.12']}
        if contract: metadata['migration'] = contract
        if service_capability is not None: metadata['runtime_capabilities']['system_service_v1'] = service_capability
        pack_tree(source, package, metadata, fixture.key)
        info = manager.stage_release(package)
        identity = info['id']
        slot = published.PUBLISHED/identity
        slot.mkdir(parents=True)
        shutil.copyfile(package, slot/'package.zip')
        for kind in ('server', 'migration'):
            extract_verified(package, slot/kind, fixture.public)
        for relative, folder in release_verification.MANAGED.items():
            target = manager.root/'shared'/folder
            target.mkdir(exist_ok=True)
            link = slot/'server'/relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
        identities.append(identity)
        write_json(published.REGISTRY, {'type':published.TYPE,
                   'context_sha256':sha(context), 'publisher_sha256':public_identity(fixture.public)[1],
                   'packages':sorted(identities)})
        return SimpleNamespace(id=identity, package=package, slot=slot,
                               manifest=read_json(slot/'migration/manifest.json'))
    initial = publish()
    write_json(manager.root/'state.json', {'current':initial.id})
    fixture.publish, fixture.initial, fixture.context = publish, initial, context
    try: yield fixture
    finally: fixture.tearDown()


def test_server_and_migration_resolve_separate_complete_signed_trees(case):
    for kind in ('server', 'migration'):
        path, manifest = published.resolve(case.manager.root, case.initial.id, case.public, kind=kind)
        assert path == case.initial.slot/kind and manifest == case.initial.manifest
    assert (case.initial.slot/'server/data').is_symlink()
    assert not (case.initial.slot/'migration/data').exists()


@pytest.mark.parametrize('mutation', ['context', 'public', 'package', 'registry', 'extra-slot'])
def test_approval_binds_context_publisher_original_package_and_slot(case, mutation):
    if mutation == 'context': case.context.write_bytes(b'{}')
    elif mutation == 'public': published.PUBLIC.write_bytes(b'not a public key')
    elif mutation == 'package': (case.initial.slot/'package.zip').write_bytes(b'changed')
    elif mutation == 'extra-slot': (case.initial.slot/'other').touch()
    else:
        record = read_json(published.REGISTRY)
        record['packages'] = ['0'*64]
        write_json(published.REGISTRY, record)
    with pytest.raises((ValueError, OSError)):
        published.resolve(case.manager.root, case.initial.id, case.public)


@pytest.mark.parametrize('mutation', ['code', 'extra-code', 'manifest', 'wrong-link', 'fifo', 'migration-link'])
def test_exact_tree_refuses_unsigned_tampered_and_special_files(case, mutation):
    kind = 'migration' if mutation == 'migration-link' else 'server'
    tree = case.initial.slot/kind
    if mutation == 'code': (tree/'main.py').write_text('tampered')
    elif mutation == 'extra-code': (tree/'json.py').write_text('shadow')
    elif mutation == 'manifest': write_json(tree/'manifest.json', {'revision':'forged'})
    elif mutation == 'wrong-link':
        (tree/'data').unlink()
        (tree/'data').symlink_to(case.root)
    elif mutation == 'fifo': os.mkfifo(tree/'pipe')
    else: (tree/'data').symlink_to(case.manager.root/'shared/data')
    with pytest.raises(ValueError):
        published.resolve(case.manager.root, case.initial.id, case.public, kind=kind)


@pytest.mark.parametrize('capability', [None, True, '1', 0, 2])
def test_only_exact_new_signed_capability_can_execute(case, capability):
    candidate = case.publish('capability', capability=capability)
    with pytest.raises(ValueError, match='不可变系统执行入口'):
        case.manager._approve_execution(candidate.id)


@pytest.mark.parametrize('capability', [None, True, '1', 0, 2])
def test_installed_service_refuses_programs_without_persistent_disable_gate(case, capability):
    candidate=case.publish('system-capability',service_capability=capability)
    (published.BASE/'system-service.json').write_bytes(b'root control exists')
    with pytest.raises(ValueError,match='停用门禁'):case.manager._approve_execution(candidate.id)


def test_root_disable_gate_blocks_manager_start_before_process_or_data_actions(case,monkeypatch):
    from desktop_assistant import mac_system_service as system
    candidate=case.publish('system-aware',service_capability=1)
    control=published.BASE/'system-service.json'
    write_json(control,{'type':system.TYPE,'package_sha256':candidate.id,'context_sha256':'a'*64,
        'publisher_sha256':'b'*64,'plist_sha256':'c'*64,'enabled':False})
    monkeypatch.setattr(system,'CONTROL',control)
    # This fixture simulates OS ownership for the root publication tree.
    # The bootstrap reader needs the same explicit simulation for its new record.
    monkeypatch.setattr(system.bootstrap,'protected',lambda path,**kw:path.read_bytes())
    write_json(case.manager.root/'state.json',{'current':candidate.id})
    before=(case.manager.root/'state.json').read_bytes()
    monkeypatch.setattr(case.manager,'_process',lambda:pytest.fail('process action before disabled gate'))
    monkeypatch.setattr('desktop_assistant.manager.subprocess.Popen',lambda *a,**kw:pytest.fail('spawned while disabled'))
    with pytest.raises(ValueError,match='已停用'):Manager.start(case.manager)
    assert (case.manager.root/'state.json').read_bytes()==before


@pytest.mark.parametrize('missing', ['desktop_assistant/migration_entry.py',
                                   'runtime/ocr/model/zh_sim_g2.pth', 'runtime/ocr/user_network/.keep'])
def test_missing_required_file_is_rejected_before_stop_or_backup(case, monkeypatch, missing):
    candidate = case.publish('missing-entry', missing=missing)
    monkeypatch.setattr(case.manager, 'stop', lambda: pytest.fail('stopped before approval'))
    monkeypatch.setattr(case.manager, '_backup_stopped', lambda *a: pytest.fail('backup before approval'))
    with pytest.raises(ValueError, match='执行入口'):
        case.manager._update_locked(candidate.package, recovery.PASSWORD, case.nas)


@pytest.mark.parametrize('boundary', ['start', 'compatible', 'update', 'chain'])
def test_cached_revision_cannot_skip_signed_schema_migration(case, monkeypatch, boundary):
    candidate = case.publish('new-schema', revision='r2')
    cached = case.manager.root/'releases'/candidate.id/'manifest.json'
    forged = read_json(cached)
    forged['revision'] = 'r1'
    write_json(cached, forged)
    monkeypatch.setattr(case.manager, 'stop', lambda: pytest.fail('service stopped'))
    monkeypatch.setattr('desktop_assistant.manager.subprocess.Popen', lambda *a, **k: pytest.fail('child started'))
    if boundary == 'start':
        write_json(case.manager.root/'state.json', {'current':candidate.id})
        action = lambda: Manager.start(case.manager)
    elif boundary == 'compatible': action = lambda: case.manager.compatible(candidate.id, 'r1')
    elif boundary == 'chain': action = lambda: case.manager._plan_update_chain(candidate.package, case.root)
    else: action = lambda: case.manager._update_locked(candidate.package, recovery.PASSWORD, case.nas)
    with pytest.raises(ValueError, match='缓存版本信息'):
        action()


def test_unapproved_rehearsal_refuses_before_creating_job(case):
    write_json(published.REGISTRY, {**read_json(published.REGISTRY), 'packages':['0'*64]})
    before = set((case.manager.root/'staging').iterdir())
    with pytest.raises(ValueError, match='发布批准'):
        migration.rehearse(case.manager, case.initial.package)
    assert set((case.manager.root/'staging').iterdir()) == before


def test_service_environment_pins_code_model_and_keeps_home_guards(case, monkeypatch):
    saved = read_json(case.manager.root/'shared/environment.json')
    saved.update(PYTHONHOME='/bad', DYLD_INSERT_LIBRARIES='/bad', HOME='/bad', PATH='/bad',
                 EASYOCR_MODULE_PATH='/bad', ERP_HOME_REHEARSAL='0', ERP_BIND_HOST='0.0.0.0',
                 HTTPS_PROXY='http://bad', OPENAI_API_KEY='synthetic')
    write_json(case.manager.root/'shared/environment.json', saved)
    env = case.manager._environment(case.initial.slot/'server')
    assert not any(k.startswith(('PYTHON', 'DYLD_', 'LD_')) for k in env)
    assert 'HTTPS_PROXY' not in env and 'OPENAI_API_KEY' not in env
    assert env['HOME'] == str(published.BASE/'home')
    assert env['PATH'] == '/usr/bin:/bin:/usr/sbin:/sbin'
    assert env['ERP_HOME_REHEARSAL'] == '1' and env['ERP_BIND_HOST'] == '127.0.0.1'
    assert env['ERP_AI_INVENTORY_PROVIDER'] == 'disabled'
    assert env['EASYOCR_MODULE_PATH'] == str(case.initial.slot/'server/runtime/ocr')


def test_start_executes_isolated_published_script(case, monkeypatch):
    captured = {}
    def spawn(argv, **kw):
        captured.update(argv=argv, **kw)
        (case.manager.root/'control'/(kw['env']['TM_ERP_NONCE']+'.ready')).touch()
        return SimpleNamespace(pid=1234, poll=lambda: None)
    monkeypatch.setattr('desktop_assistant.manager.subprocess.Popen', spawn)
    monkeypatch.setattr('desktop_assistant.manager.psutil.Process', lambda pid: SimpleNamespace(create_time=lambda:1.0))
    monkeypatch.setattr('desktop_assistant.home_process.command', lambda argv, env: argv)
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def connect_ex(self, address): return 1
    monkeypatch.setattr('desktop_assistant.manager.socket.socket', Probe)
    Manager.start(case.manager)
    release = case.initial.slot/'server'
    assert captured['argv'] == [str(release/'runtime/bin/python3.12'), '-I', '-B',
                               str(release/'desktop_assistant/server_entry.py')]
    assert captured['cwd'] == release and captured['env']['ERP_HOME_REHEARSAL'] == '1'


@pytest.mark.parametrize('interrupt', [False, True])
def test_migration_uses_approved_tree_and_observes_child_until_exit(case, monkeypatch, interrupt):
    captured = {}
    wait = Mock(side_effect=[KeyboardInterrupt(), 0] if interrupt else [0])
    def spawn(argv, **kw):
        captured.update(argv=argv, **kw)
        return SimpleNamespace(wait=wait)
    monkeypatch.setattr(migration.subprocess, 'Popen', spawn)
    monkeypatch.setattr(migration, 'prepare_runtime', lambda *a: pytest.fail('chmod root tree'))
    monkeypatch.setattr('desktop_assistant.home_process.command', lambda argv, env: argv)
    release = case.initial.slot/'migration'
    def run():
        migration.run_migration(release, case.manager.root/'shared', 'r1', case.root/'migration.log',
            {'PYTHONHOME':'/bad', 'DYLD_LIBRARY_PATH':'/bad', 'OPENAI_API_KEY':'synthetic',
             'ERP_HOME_REHEARSAL':'0', 'HTTPS_PROXY':'http://bad'}, release_manifest=case.initial.manifest)
    if interrupt:
        with pytest.raises(ValueError, match='等候中断'): run()
    else: run()
    assert wait.call_count == (2 if interrupt else 1)
    assert all(not call.args and not call.kwargs for call in wait.call_args_list)
    assert captured['argv'] == [str(release/'runtime/bin/python3.12'), '-I', '-B',
        str(release/'desktop_assistant/migration_entry.py'), 'upgrade', 'r1']
    assert captured['cwd'] == release and captured['env']['ERP_HOME_REHEARSAL'] == '1'
    assert not {'PYTHONHOME', 'DYLD_LIBRARY_PATH', 'OPENAI_API_KEY', 'HTTPS_PROXY'} & captured['env'].keys()


def test_migration_writable_cache_is_never_executed(case, monkeypatch):
    monkeypatch.setattr(migration.subprocess, 'Popen', lambda *a, **k: pytest.fail('child'))
    with pytest.raises(ValueError):
        migration.run_migration(case.manager.root/'releases'/case.initial.id, case.manager.root/'shared',
            'r1', case.root/'rejected.log', release_manifest=case.initial.manifest)
    assert not (case.root/'rejected.log').exists()


@pytest.mark.parametrize('kind', ['server', 'migration'])
def test_interrupted_recovery_refuses_live_either_runtime(case, monkeypatch, kind):
    candidate = case.publish('new-schema', revision='r2', contract={
        'policy':'preserve_existing_facts_v1', 'from_revision':'r1',
        'rollback_package_sha256':case.initial.id})
    state = {**case.manager.state, 'operation':'migration_running', 'migration_target':candidate.id}
    write_json(case.manager.root/'state.json', state)
    monkeypatch.setattr('desktop_assistant.manager.psutil.process_iter', lambda fields: [
        SimpleNamespace(info={'exe':str(candidate.slot/kind/'runtime/bin/python3.12')})])
    before = sha(case.manager.root/'shared/data/carton_erp.sqlite3')
    with pytest.raises(ValueError, match='仍有进程'):
        case.manager.recover_interrupted_update()
    assert sha(case.manager.root/'shared/data/carton_erp.sqlite3') == before
    assert case.manager.state == state


def test_rehearsal_runs_immutable_migration_tree_only_against_copy(case, monkeypatch):
    candidate = case.publish('new-schema', revision='r2')
    database = case.manager.root/'shared/data/carton_erp.sqlite3'
    before, state = sha(database), case.manager.state
    def migrate(release, shared, revision, log, **kw):
        assert release == candidate.slot/'migration'
        assert shared != case.manager.root/'shared'
        assert not (release/'data').exists()
        with closing(sqlite3.connect(shared/'data/carton_erp.sqlite3')) as db:
            db.execute('ALTER TABLE sales_orders ADD COLUMN new_field TEXT')
            db.execute('UPDATE alembic_version SET version_num=?', (revision,))
            db.commit()
        log.write_text('synthetic migration')
    monkeypatch.setattr(migration, 'run_migration', migrate)
    report = migration.rehearse(case.manager, candidate.package)
    assert report['status'] == 'passed' and report['source_unchanged']
    assert sha(database) == before and case.manager.state == state
    assert candidate.slot.is_dir()


@pytest.mark.parametrize('dedicated', [False, True])
def test_full_restore_remains_stopped_for_service_identity(tmp_path, monkeypatch, dedicated):
    fixture = recovery.RecoveryTests()
    fixture.setUp()
    try:
        backup = fixture.manager.backup(recovery.PASSWORD, fixture.nas)
        monkeypatch.setattr(published, 'selected', lambda: dedicated)
        restored = recovery.TestManager(fixture.root/'new-host', fixture.public)
        result = restored.restore(backup, recovery.PASSWORD)
        assert result['started'] is False
        assert restored.state.get('manual_stop') is (True if dedicated else None)
        assert sha(restored.root/'shared/data/carton_erp.sqlite3') == sha(fixture.manager.root/'shared/data/carton_erp.sqlite3')
    finally: fixture.tearDown()


@pytest.mark.skipif(sys.platform != 'darwin' or os.getuid() == 0, reason='unprivileged Mac native check')
def test_real_root_guard_rejects_current_user_owned_code(tmp_path):
    path = tmp_path/'not-root-code'
    path.write_bytes(b'synthetic')
    path.chmod(0o644)
    with pytest.raises(ValueError): release_verification.immutable(path)


@pytest.mark.parametrize('raw', [b'{"type":1,"type":2}', b'[]', b'{}', b'null'])
def test_malformed_or_ambiguous_registry_is_rejected(raw):
    with pytest.raises(ValueError): published._record(raw)
