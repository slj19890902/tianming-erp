import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from desktop_assistant import cleanup, retention
from desktop_assistant.storage import sha, write_json, pack_tree
from tests.desktop_assistant import test_recovery as recovery

PASSWORD = recovery.PASSWORD


@pytest.fixture
def installed(monkeypatch):
    fixture = recovery.RecoveryTests()
    fixture.setUp()
    state = fixture.manager.state
    state['operation'] = 'update'
    write_json(fixture.manager.root/'state.json', state)
    monkeypatch.setattr(cleanup, 'assert_idle', lambda path: None)
    monkeypatch.setattr(retention, 'assert_idle', lambda path: None)
    monkeypatch.setattr(retention, 'archive_synced', lambda path: True)
    try:
        yield fixture
    finally:
        fixture.tearDown()


def old_release(fixture, name='old'):
    package = fixture.release(name)
    identity = fixture.manager.stage_release(package)['id']
    feed = fixture.nas/'releases'
    feed.mkdir(exist_ok=True)
    shutil.copyfile(package, feed/(identity+'.zip'))
    return identity, fixture.manager.root/'releases'/identity, fixture.manager.root/'packages'/(identity+'.zip')


def test_removes_reconstructible_old_release_and_archived_package(installed):
    identity, release, package = old_release(installed)
    db = installed.manager.root/'shared/data/carton_erp.sqlite3'
    before = sha(db)
    result = retention.cleanup_releases(installed.manager, installed.nas/'releases')
    assert result['removed_releases'] == result['removed_packages'] == [identity]
    assert not release.exists() and not package.exists()
    assert sha(db) == before
    assert (installed.manager.root/'releases'/installed.manager.state['current']).exists()
    assert retention.cleanup_releases(installed.manager, installed.nas/'releases')['removed_releases'] == []


@pytest.mark.parametrize('key', ['current','previous','schema_authority','migration_target'])
def test_each_protected_state_role_is_retained(installed, key):
    identity, release, package = old_release(installed)
    state = installed.manager.state
    state[key] = identity
    write_json(installed.manager.root/'state.json',state)
    result = retention.cleanup_releases(installed.manager, installed.nas/'releases')
    assert identity in result['protected']
    assert release.exists() and package.exists()


def test_signed_fallback_is_retained(installed):
    identity, release, package = old_release(installed)
    protected = installed.release('protected')
    protected = protected.with_name('protected-signed.zip')
    pack_tree(installed.root/'source-protected',protected,{
        'type':'tianming.release.v1','version':'protected','revision':'r1',
        'migration':{'rollback_package_sha256':identity}},installed.key)
    current = installed.manager.stage_release(protected)['id']
    write_json(installed.manager.root/'state.json', {'current':current,'operation':'update'})
    result = retention.cleanup_releases(installed.manager,installed.nas/'releases')
    assert identity in result['protected'] and release.exists() and package.exists()


@pytest.mark.parametrize('problem',['missing','corrupt','uploading','offline'])
def test_missing_or_unconfirmed_nas_keeps_reconstruction_package(installed, monkeypatch, problem):
    identity, release, package = old_release(installed)
    remote = installed.nas/'releases'/(identity+'.zip')
    if problem=='missing':remote.unlink()
    if problem=='corrupt':remote.write_bytes(b'bad')
    if problem in ('uploading','offline'):monkeypatch.setattr(retention,'archive_synced',lambda path:False)
    result=retention.cleanup_releases(installed.manager,installed.nas/'releases')
    assert not release.exists() and package.exists()
    assert identity in result['retained']


@pytest.mark.parametrize('problem',['changed','extra','missing','active','invalid_package'])
def test_modified_or_active_release_is_not_removed(installed, monkeypatch, problem):
    identity, release, package = old_release(installed)
    if problem=='changed':(release/'main.py').write_text('uncommitted fix')
    if problem=='extra':(release/'personal.txt').write_text('retain')
    if problem=='missing':(release/'main.py').unlink()
    if problem=='active':monkeypatch.setattr(retention,'assert_idle',lambda path: (_ for _ in ()).throw(ValueError('active')))
    if problem=='invalid_package':package.write_bytes(b'bad')
    result=retention.cleanup_releases(installed.manager,installed.nas/'releases')
    assert identity in result['retained'] and release.exists() and package.exists()


def test_unknown_junction_does_not_delete_shared(installed):
    import subprocess
    identity, release, package = old_release(installed)
    target = installed.manager.root/'shared'
    link = release/'unexpected'
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                    f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null"],check=True)
    try:
        result=retention.cleanup_releases(installed.manager,installed.nas/'releases')
        assert identity in result['retained'] and (target/'data/carton_erp.sqlite3').exists()
    finally:
        link.rmdir()


def test_managed_junction_is_unlinked_without_following(installed):
    identity,release,package=old_release(installed)
    installed.manager._link_data(release)
    before=sha(installed.manager.root/'shared/data/carton_erp.sqlite3')
    result=retention.cleanup_releases(installed.manager,installed.nas/'releases')
    assert result['removed_releases']==[identity]
    assert sha(installed.manager.root/'shared/data/carton_erp.sqlite3')==before


def test_failed_migration_and_bad_state_fail_closed(installed):
    identity,release,package=old_release(installed)
    for state in ({'operation':'migration_failed','current':identity}, {'operation':'update','current':'../shared'}):
        write_json(installed.manager.root/'state.json',state)
        with pytest.raises(ValueError):retention.cleanup_releases(installed.manager,installed.nas/'releases')
        assert release.exists() and package.exists()


def test_cleanup_failure_cannot_fail_successful_update(installed, monkeypatch):
    monkeypatch.setattr(retention,'cleanup_releases',lambda *args: (_ for _ in ()).throw(OSError('locked')))
    package=installed.release('two')
    assert installed.manager.update(package,PASSWORD,installed.nas)=='two'
    assert installed.manager.state['current']==sha(package)
    report=json.loads((installed.manager.root/'control/retention-latest.json').read_text())
    assert report['error']=='locked'


def test_failed_start_does_not_call_retention(installed, monkeypatch):
    from unittest.mock import Mock
    run=Mock();monkeypatch.setattr(retention,'after_update',run)
    with pytest.raises(ValueError,match='启动失败'):
        installed.manager.update(installed.release('bad-start'),PASSWORD,installed.nas)
    run.assert_not_called()


def test_idle_guard_detects_database_in_child_environment(monkeypatch,tmp_path):
    fake=SimpleNamespace(pid=123,info={'name':'python.exe'},exe=lambda:'python.exe',cwd=lambda:'C:/other',
                         cmdline=lambda:['python.exe','main.py'],environ=lambda:{'ERP_DATABASE_PATH':str(tmp_path/'copy.db')})
    monkeypatch.setattr(cleanup.psutil,'process_iter',lambda attrs:[fake])
    with pytest.raises(ValueError,match='运行进程'):cleanup.assert_idle(tmp_path)


def test_owned_tree_rejects_escape_git_and_links(tmp_path,monkeypatch):
    monkeypatch.setattr(cleanup,'assert_idle',lambda p:None)
    parent=tmp_path/'owned';parent.mkdir()
    elsewhere=tmp_path/'outside';elsewhere.mkdir()
    with pytest.raises(ValueError):cleanup.remove_owned_tree(elsewhere,parent)
    child=parent/'copies';child.mkdir();(child/'.git').write_text('gitdir: elsewhere')
    with pytest.raises(ValueError):cleanup.remove_owned_tree(child,parent)
    assert child.exists()


def test_fuse_upload_queue_never_counts_as_synced(monkeypatch):
    import io
    class Kernel:
        def GetVolumeInformationW(self,*args):args[6].value='FUSE-rclone';return 1
        def GetDriveTypeW(self,*args):return 3
    monkeypatch.setattr(retention.ctypes,'windll',SimpleNamespace(kernel32=Kernel()))
    result={'fs':'uzmount:','diskCache':{'uploadsQueued':1,'uploadsInProgress':0,'erroredFiles':0}}
    monkeypatch.setattr(retention,'build_opener',lambda *_:SimpleNamespace(open=lambda *a,**k:io.BytesIO(json.dumps(result).encode())))
    assert not retention.archive_synced(Path('Z:/folder/file.zip'))
    result['diskCache']['uploadsQueued']=0
    assert retention.archive_synced(Path('Z:/folder/file.zip'))
