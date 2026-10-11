from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path

import pytest

from desktop_assistant import windows_private_workspace as workspace


SID = 'S-1-5-21-100-200-300-1001'


def security(sid=SID):
    return {'owner': sid, 'protected': True,
            'aces': [(sid, 0, 3, workspace.FULL_CONTROL),
                     (workspace.SYSTEM, 0, 3, workspace.FULL_CONTROL)]}


class NativeFixture:
    def __init__(self):
        self.record = security()
        self.parent_record = security()
        self.opened = []
        self.created = []
        self.readbacks = 0
        self.volume_error = False
        self.readback_error = False

    @contextmanager
    def directory(self, path):
        self.opened.append(path)
        try:
            yield path
        finally:
            self.opened.remove(path)

    def require_local_acl_volume(self, parent):
        if self.volume_error:
            raise ValueError('synthetic unsupported volume')

    def current_sid(self):
        return SID

    def create(self, path, sid):
        assert path.parent in self.opened
        path.mkdir(exist_ok=False)
        self.created.append(path)

    def security(self, handle):
        assert handle in self.opened
        self.readbacks += 1
        if self.readback_error:
            raise OSError('synthetic readback failure')
        return deepcopy(self.record if handle in self.created else self.parent_record)


@pytest.fixture
def native(monkeypatch):
    native = NativeFixture()
    monkeypatch.setattr(workspace, '_Windows', lambda: native)
    return native


def test_only_verified_workspace_is_exposed_and_owned_files_are_retained(tmp_path, native):
    with workspace.private_workspace(tmp_path) as job:
        assert native.readbacks == 2
        assert job in native.opened
        assert job.parent == tmp_path.resolve()
        (job / 'synthetic.txt').write_text('synthetic only')
    assert native.readbacks == 3
    assert not native.opened
    assert (job / 'synthetic.txt').read_text() == 'synthetic only'


@pytest.mark.parametrize('change', ['extra', 'inherited', 'deny', 'read-only', 'wrong-owner', 'unprotected'])
def test_unsafe_acl_never_reaches_plaintext_writer(tmp_path, native, change):
    if change == 'extra':
        native.record['aces'].append(('S-1-1-0', 0, 3, workspace.FULL_CONTROL))
    elif change in ('inherited', 'deny', 'read-only'):
        native.record['aces'][0] = (SID, int(change == 'deny'),
                                    19 if change == 'inherited' else 3,
                                    0x120089 if change == 'read-only' else workspace.FULL_CONTROL)
    elif change == 'wrong-owner':
        native.record['owner'] = workspace.SYSTEM
    else:
        native.record['protected'] = False
    with pytest.raises(ValueError, match='禁止写入明文'):
        with workspace.private_workspace(tmp_path):
            pytest.fail('must reject before caller may decrypt')
    assert len(native.created) == 1 and not list(native.created[0].iterdir())
    assert not native.opened


def test_readback_and_volume_errors_do_not_reach_writer(tmp_path, native):
    native.volume_error = True
    with pytest.raises(ValueError):
        with workspace.private_workspace(tmp_path):
            pytest.fail('NAS/FAT staging must not be used')
    assert not native.created and not native.opened
    native.volume_error = False
    native.readback_error = True
    with pytest.raises(OSError):
        with workspace.private_workspace(tmp_path):
            pytest.fail('unverified staging must not be used')
    assert not native.created and not native.opened


@pytest.mark.parametrize('change', ['extra', 'inherited', 'wrong-owner', 'unprotected'])
def test_parent_must_be_private_before_exclusive_creation(tmp_path, native, change):
    if change == 'extra':
        native.parent_record['aces'].append(('S-1-1-0', 0, 3, workspace.FULL_CONTROL))
    elif change == 'inherited':
        native.parent_record['aces'][0] = (SID, 0, 19, workspace.FULL_CONTROL)
    elif change == 'wrong-owner':
        native.parent_record['owner'] = workspace.SYSTEM
    else:
        native.parent_record['protected'] = False
    before = deepcopy(native.parent_record)
    with pytest.raises(ValueError, match='禁止写入明文'):
        with workspace.private_workspace(tmp_path):
            pytest.fail('broad parent could replace a newly created child')
    assert not native.created and not native.opened
    assert native.parent_record == before


def test_caller_failure_keeps_owned_evidence_and_releases_handles(tmp_path, native):
    with pytest.raises(RuntimeError, match='synthetic'):
        with workspace.private_workspace(tmp_path) as job:
            (job / 'owned-fixture').write_text('retain')
            raise RuntimeError('synthetic decrypt failure')
    assert (job / 'owned-fixture').read_text() == 'retain'
    assert not native.opened


def test_final_readback_failure_preserves_evidence_without_repairing_permissions(tmp_path, native):
    with pytest.raises(ValueError, match='禁止写入明文'):
        with workspace.private_workspace(tmp_path) as job:
            (job / 'synthetic.txt').write_text('retain')
            native.record['protected'] = False
    assert (job / 'synthetic.txt').read_text() == 'retain'
    assert native.record['protected'] is False and not native.opened


def test_existing_target_is_never_reused(tmp_path, native, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(workspace.uuid, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    existing = tmp_path / ('cutover-' + 'a' * 32)
    existing.mkdir()
    (existing / 'retain').write_text('existing')
    with pytest.raises(FileExistsError):
        with workspace.private_workspace(tmp_path):
            pytest.fail('existing directory must be rejected')
    assert (existing / 'retain').read_text() == 'existing'
    assert not native.opened and native.readbacks == 1


def test_missing_parent_is_not_created(tmp_path, native):
    with pytest.raises(ValueError, match='不存在'):
        with workspace.private_workspace(tmp_path / 'missing'):
            pytest.fail('must not create an install root')
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink fixture; Windows reparse coverage below')
def test_symlink_parent_is_rejected_before_creation(tmp_path, native):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match='链接'):
        with workspace.private_workspace(link):
            pytest.fail('link parent cannot be used')
    assert not native.created


@pytest.mark.skipif(os.name == 'nt', reason='non-Windows platform guard')
def test_other_platform_cannot_create_a_fake_private_windows_directory(tmp_path):
    with pytest.raises(ValueError, match='只支持Windows'):
        with workspace.private_workspace(tmp_path):
            pytest.fail('platform must be rejected')
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != 'nt', reason='real Windows ACL, inheritance and handle sharing')
def test_windows_private_acl_inheritance_and_no_rename(tmp_path):
    import json
    from desktop_assistant.windows import run_maintenance_powershell
    native = workspace._Windows()
    sid = native.current_sid()
    parent = tmp_path / '原用户暂存'
    native.create(parent, sid)  # Synthetic fixture only, never an existing ACL.
    with workspace.private_workspace(parent) as job:
        with native.directory(job) as handle:
            workspace._validate_security(native.security(handle), sid)
        child = job / 'child'
        child.mkdir()
        with native.directory(child) as handle:
            inherited = native.security(handle)
        assert {entry[0] for entry in inherited['aces']} == {sid, workspace.SYSTEM}
        assert all(entry[1] == 0 and entry[2] == 19 and entry[3] == workspace.FULL_CONTROL
                   for entry in inherited['aces'])
        file = child / 'synthetic.txt'
        file.write_text('synthetic only')
        path = str(file).replace("'", "''")
        # Independent .NET readback checks a real created file, not only its
        # directory or a simulated security descriptor.
        command = (f"$ErrorActionPreference='Stop';$a=Get-Acl -LiteralPath '{path}';"
                   "@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | "
                   "ForEach-Object { @{sid=$_.IdentityReference.Value;inherited=$_.IsInherited;"
                   "type=[int]$_.AccessControlType;rights=[int]$_.FileSystemRights} }) | ConvertTo-Json -Compress")
        result = run_maintenance_powershell(command, tmp_path)
        assert result.returncode == 0
        entries = json.loads(result.stdout.decode('ascii'))
        assert len(entries) == 2 and {e['sid'] for e in entries} == {sid, workspace.SYSTEM}
        assert all(e['inherited'] is True and e['type'] == 0 and e['rights'] == workspace.FULL_CONTROL
                   for e in entries)
        with pytest.raises(OSError):
            job.rename(parent / 'renamed')
        with pytest.raises(OSError):
            parent.rename(tmp_path / 'renamed-parent')
    assert (child / 'synthetic.txt').read_text() == 'synthetic only'


@pytest.mark.skipif(os.name != 'nt', reason='real Windows readback rejects extra ACE')
def test_windows_extra_acl_is_rejected_without_modification(tmp_path):
    from desktop_assistant.windows import run_maintenance_powershell
    native = workspace._Windows()
    parent = tmp_path / 'private-parent'
    native.create(parent, native.current_sid())
    with pytest.raises(ValueError, match='禁止写入明文'):
        with workspace.private_workspace(parent) as job:
            path = str(job).replace("'", "''")
            command = (f"$p='{path}';$a=Get-Acl -LiteralPath $p;"
                       "$s=New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0');"
                       "$r=New-Object System.Security.AccessControl.FileSystemAccessRule($s,'Read','Allow');"
                       "$a.AddAccessRule($r);Set-Acl -LiteralPath $p -AclObject $a;"
                       "(Get-Acl -LiteralPath $p).Sddl")
            result = run_maintenance_powershell(command, tmp_path)
            assert result.returncode == 0
            # Final readback rejects, without trying to repair the ACL.
    after = run_maintenance_powershell(f"(Get-Acl -LiteralPath '{path}').Sddl", tmp_path)
    assert after.returncode == 0 and after.stdout == result.stdout


@pytest.mark.skipif(os.name != 'nt', reason='real Windows junction rejection; synthetic paths only')
def test_windows_junction_is_not_followed(tmp_path):
    from desktop_assistant.windows import run_maintenance_powershell
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'junction'
    target, alias = (str(p).replace("'", "''") for p in (real, link))
    result = run_maintenance_powershell(
        f"$ErrorActionPreference='Stop';New-Item -ItemType Junction -Path '{alias}' -Target '{target}' | Out-Null",
        tmp_path)
    assert result.returncode == 0
    with pytest.raises(ValueError, match='链接|重解析'):
        with workspace.private_workspace(link):
            pytest.fail('junction must not reach caller')
    assert not list(real.iterdir())
