"""Create-only provisioning simulations and native read-only OD probes.

No test creates a real OS user/group or modifies any original directory.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import uuid

import pytest

from desktop_assistant import mac_service_identity as identity


class DirectoryFixture:
    def __init__(self):
        self.records = {}
        self.created = []
        self.corrupt = None
        self.fail = None

    def read(self, kind, name, attributes):
        assert name == identity.ACCOUNT
        value = deepcopy(self.records.get(kind))
        if value and self.corrupt == kind:
            value[identity.ATTR + 'PrimaryGroupID'] = ['0']
        return value

    def create(self, kind, attributes):
        if self.fail == kind:
            raise ValueError('synthetic OD refusal')
        if kind in self.records:
            raise ValueError('synthetic existing record')
        assert all(isinstance(v, list) and all(isinstance(s, str) for s in v) for v in attributes.values())
        self.records[kind] = deepcopy(attributes) | {identity.ATTR + 'RecordName': [identity.ACCOUNT]}
        self.created.append(kind)


@pytest.fixture
def case(tmp_path, monkeypatch):
    directory = DirectoryFixture()
    base = tmp_path.resolve() / 'TianmingERP'
    monkeypatch.setattr(identity, 'BASE', base)
    monkeypatch.setattr(identity, '_platform', lambda: None)
    monkeypatch.setattr(identity, '_administrator', lambda: None)
    monkeypatch.setattr(identity, '_parent', lambda: None)
    monkeypatch.setattr(identity, '_host', lambda: 'a' * 64)
    monkeypatch.setattr(identity, '_code_sha', lambda: 'b' * 64)
    monkeypatch.setattr(identity, '_ids', lambda: ({0, 498, 501}, {0, 20, 80, 498}))
    monkeypatch.setattr(identity, 'Directory', lambda: directory)
    checks = []
    monkeypatch.setattr(identity, '_check_identity', lambda value: checks.append('identity'))
    ownership = []
    def owned(path, uid, gid):
        path.mkdir(mode=0o700)
        ownership.append((path.name, uid, gid))
    monkeypatch.setattr(identity, '_owned_directory', owned)
    # Keep OS account lookups read-only but deterministic for the reserved name.
    import pwd
    import grp
    def missing(name):
        raise KeyError(name)
    monkeypatch.setattr(pwd, 'getpwnam', missing)
    monkeypatch.setattr(grp, 'getgrnam', missing)
    value = identity.plan()
    return SimpleNamespace(base=base, directory=directory, value=value,
                           checks=checks, ownership=ownership)


def run(case, value=None):
    raw = identity.canonical(case.value if value is None else value)
    return identity.provision(raw, hashlib.sha256(raw).hexdigest())


def test_readonly_plan_and_create_only_flow_preserve_no_login_and_private_homes(case):
    assert not case.base.exists() and not case.directory.created
    assert case.value['uid'] == case.value['gid'] == 499
    result = run(case)
    assert case.directory.created == ['Groups', 'Users']
    assert case.directory.records['Users'][identity.ATTR + 'AuthenticationAuthority'] == [';DisabledUser;']
    assert case.directory.records['Users'][identity.ATTR + 'UserShell'] == ['/usr/bin/false']
    assert case.ownership == [('home', 499, 499), ('instance', 499, 499)]
    assert case.checks == ['identity', 'identity']
    assert result['account_created'] and not result['keychain_created']
    assert not result['factory_activated'] and not result['service_started']
    assert stat.S_IMODE(case.base.stat().st_mode) == 0o755
    assert (case.base / 'provisioned.json').read_bytes() == identity.canonical(result)


@pytest.mark.parametrize('field,value', [
    ('account', 'root'), ('base', '/Users/another'), ('uid', 0), ('gid', 20), ('uid', True),
    ('type', 'unknown'), ('host_sha256', '0' * 64), ('installer_sha256', '0' * 64),
    ('user_guid', 'invalid'), ('installation_id', 'not-an-id'),
    ('created_at', 0), ('expires_at', 0), ('expires_at', True),
])
def test_changed_approved_plan_cannot_create_resources(case, field, value):
    with pytest.raises((ValueError, TypeError)):
        run(case, case.value | {field: value})
    assert not case.base.exists() and not case.directory.created


def test_wrong_raw_digest_and_duplicate_fields_cannot_create_resources(case):
    raw = identity.canonical(case.value)
    with pytest.raises(ValueError, match='摘要'):
        identity.provision(raw, '0' * 64)
    raw = raw[:-1] + b',"uid":499}'
    with pytest.raises(ValueError, match='重复'):
        identity.provision(raw, hashlib.sha256(raw).hexdigest())
    assert not case.base.exists() and not case.directory.created


@pytest.mark.parametrize('existing', ['Users', 'Groups', 'directory', 'symlink'])
def test_existing_resources_are_not_adopted_repaired_or_deleted(case, existing):
    if existing in ('Users', 'Groups'):
        case.directory.records[existing] = {'unrelated': ['keep']}
    elif existing == 'directory':
        case.base.mkdir(); (case.base / 'keep').write_bytes(b'untouched')
    else:
        case.base.symlink_to(case.base.parent / 'missing-target')
    before = deepcopy(case.directory.records)
    with pytest.raises(ValueError):
        run(case)
    assert case.directory.records == before and not case.directory.created
    if existing == 'directory':
        assert (case.base / 'keep').read_bytes() == b'untouched'
    if existing == 'symlink':
        assert case.base.is_symlink()


@pytest.mark.parametrize('which', ['uid', 'gid'])
def test_id_collision_after_review_refused(case, monkeypatch, which):
    monkeypatch.setattr(identity, '_ids', lambda: ({499} if which == 'uid' else set(),
                                                  {499} if which == 'gid' else set()))
    with pytest.raises(ValueError, match='占用'):
        run(case)
    assert not case.base.exists() and not case.directory.created


def test_nonadministrator_and_unsafe_parent_fail_before_creation(case, monkeypatch):
    def no():
        raise ValueError('synthetic administrator unavailable')
    monkeypatch.setattr(identity, '_administrator', no)
    with pytest.raises(ValueError):
        run(case)
    monkeypatch.setattr(identity, '_administrator', lambda: None)
    monkeypatch.setattr(identity, '_parent', no)
    with pytest.raises(ValueError):
        run(case)
    assert not case.base.exists() and not case.directory.created


@pytest.mark.parametrize('failure', ['group', 'user', 'group-readback', 'user-readback', 'identity', 'home'])
def test_partial_failure_retains_owned_scene_and_never_reports_complete(case, monkeypatch, failure):
    if failure == 'group':
        case.directory.fail = 'Groups'
    elif failure == 'user':
        case.directory.fail = 'Users'
    elif failure in ('group-readback', 'user-readback'):
        case.directory.corrupt = 'Groups' if failure == 'group-readback' else 'Users'
    else:
        def no(*args):
            raise ValueError('synthetic post-create refusal')
        monkeypatch.setattr(identity, '_check_identity' if failure == 'identity' else '_owned_directory', no)
    with pytest.raises(ValueError):
        run(case)
    assert case.base.is_dir() and (case.base / '00-plan.json').exists()
    assert not (case.base / 'provisioned.json').exists()
    assert stat.S_IMODE(case.base.stat().st_mode) == 0o700
    if failure != 'group':
        assert 'Groups' in case.directory.records
    before = deepcopy(case.directory.records)
    with pytest.raises(ValueError, match='已存在'):
        run(case)
    assert case.directory.records == before


def test_record_create_race_does_not_merge_into_existing_record(case):
    original = case.directory.create
    def race(kind, attrs):
        case.directory.records[kind] = {'prior': ['unrelated-racing-record']}
        original(kind, attrs)
    case.directory.create = race
    with pytest.raises(ValueError, match='existing record'):
        run(case)
    assert case.directory.records == {'Groups': {'prior': ['unrelated-racing-record']}}
    assert not (case.base / 'provisioned.json').exists()


@pytest.mark.parametrize('failure', ['identity-recheck', 'receipt'])
def test_late_failure_preserves_traversable_root_and_private_children(case, monkeypatch, failure):
    if failure == 'identity-recheck':
        calls = []
        def check(value):
            calls.append(value)
            if len(calls) == 2:
                raise ValueError('synthetic late identity refusal')
        monkeypatch.setattr(identity, '_check_identity', check)
    else:
        write = identity._write
        def write_except_receipt(path, raw):
            if path.name == 'provisioned.json':
                raise ValueError('synthetic receipt refusal')
            write(path, raw)
        monkeypatch.setattr(identity, '_write', write_except_receipt)
    with pytest.raises(ValueError):
        run(case)
    assert stat.S_IMODE(case.base.stat().st_mode) == 0o755
    assert all(stat.S_IMODE((case.base / name).stat().st_mode) == 0o700
               for name in ('home', 'instance'))
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in case.base.glob('*.json'))
    assert not (case.base / 'provisioned.json').exists()
    before = deepcopy(case.directory.records)
    with pytest.raises(ValueError, match='已存在'):
        run(case)
    assert case.directory.records == before


@pytest.mark.parametrize('change', [
    None, 'uid', 'primary-gid', 'home', 'shell', 'group-gid', 'group-member',
    'duplicate-uid', 'duplicate-gid', 'admin-group', 'other-group',
])
def test_actual_identity_check_rejects_inconsistent_or_privileged_identity(monkeypatch, change):
    import pwd
    import grp
    user = SimpleNamespace(pw_uid=499, pw_gid=499,
                           pw_dir=str(identity.BASE / 'home'), pw_shell='/usr/bin/false')
    group = SimpleNamespace(gr_gid=499, gr_mem=[])
    users, groups, memberships = [user], [group], [499, 12, 61]
    if change in {'uid', 'primary-gid', 'home', 'shell'}:
        field, value = {'uid': ('pw_uid', 0), 'primary-gid': ('pw_gid', 80),
                        'home': ('pw_dir', '/Users/other'), 'shell': ('pw_shell', '/bin/zsh')}[change]
        setattr(user, field, value)
    elif change == 'group-gid':
        group.gr_gid = 80
    elif change == 'group-member':
        group.gr_mem = ['other-user']
    elif change == 'duplicate-uid':
        users.append(SimpleNamespace(pw_uid=499))
    elif change == 'duplicate-gid':
        groups.append(SimpleNamespace(gr_gid=499))
    elif change in {'admin-group', 'other-group'}:
        memberships.append(80 if change == 'admin-group' else 20)
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: user)
    monkeypatch.setattr(grp, 'getgrnam', lambda name: group)
    monkeypatch.setattr(pwd, 'getpwall', lambda: users)
    monkeypatch.setattr(grp, 'getgrall', lambda: groups)
    monkeypatch.setattr(os, 'getgrouplist', lambda name, gid: memberships)
    if change is None:
        identity._check_identity({'uid': 499, 'gid': 499})
    else:
        with pytest.raises(ValueError, match='实际UID/GID'):
            identity._check_identity({'uid': 499, 'gid': 499})


@pytest.mark.skipif(sys.platform != 'darwin', reason='native read-only OpenDirectory')
def test_native_readonly_directory_existing_missing_and_attribute_filter():
    api = identity.Directory()
    requested = [identity.ATTR + 'RecordName', identity.ATTR + 'UniqueID']
    record = api.read('Users', 'root', requested)
    assert record[identity.ATTR + 'UniqueID'] == ['0']
    assert set(record) == set(requested)
    assert api.read('Users', '_tm_absent_' + uuid.uuid4().hex, requested) is None


@pytest.mark.skipif(sys.platform != 'darwin' or os.getuid() == 0,
                    reason='native unprivileged refusal, no OS create attempted')
def test_native_creation_requires_real_administrator_before_od_write():
    with pytest.raises(ValueError, match='管理员'):
        identity.Directory().create('Users', {})


@pytest.mark.skipif(sys.platform != 'darwin' or os.getuid() == 0,
                    reason='native refusal of user-writable elevated installer')
def test_user_owned_script_is_not_an_approved_root_execution_location(tmp_path, monkeypatch):
    script = tmp_path.resolve() / 'script.py'; script.write_text('# synthetic only')
    monkeypatch.setattr(identity, '__file__', str(script))
    with pytest.raises(ValueError, match='root保护'):
        identity._trusted_installer()
    assert script.read_text() == '# synthetic only'


@pytest.mark.skipif(sys.platform != 'darwin', reason='native private disposable directory ACL probe')
def test_native_acl_check_accepts_no_acl_and_rejects_task_owned_acl(tmp_path):
    import subprocess
    path = tmp_path.resolve() / 'acl-probe'; path.mkdir(mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity._no_acl(fd)
        subprocess.run(['/bin/chmod', '+a', 'everyone allow read', str(path)], check=True, capture_output=True)
        before = subprocess.check_output(['/bin/ls', '-lde', str(path)])
        with pytest.raises(ValueError, match='ACL'):
            identity._no_acl(fd)
        assert subprocess.check_output(['/bin/ls', '-lde', str(path)]) == before
    finally:
        os.close(fd)
