"""Synthetic archives and user-owned files only; no installed service access."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import zipfile

import pytest

s = None
NAMES = {'app/__init__.py', 'desktop_assistant/__init__.py', 'desktop_assistant/mac_keychain_install.py'}


@pytest.fixture(autouse=True, params=['controlled_uat_admission_v717.py',
                                     'controlled_uat_env_compat_admission_v717.py'])
def fixed_admission(request, monkeypatch):
    entry = Path(__file__).resolve().parents[2] / 'scripts/admin' / request.param
    spec = importlib.util.spec_from_file_location('admission_under_test', entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(globals(), 's', module)


def package(monkeypatch, *, change=None):
    contents = {name: b'# synthetic code\n' for name in NAMES}
    manifest = {'type': 'tianming.keychain-code.v1', 'source': s.TOOL_SOURCE,
                'files': {n: s.digest(v) for n, v in contents.items()}}
    entries = [(n, v, 0o100444) for n, v in contents.items()]
    if change:
        change(manifest, entries)
    raw_manifest = json.dumps(manifest).encode()
    entries.append(('install-manifest.json', raw_manifest, 0o100444))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        for name, raw, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    raw = buf.getvalue()
    # Authenticate each malformed fixture so the inner schema defenses are tested.
    monkeypatch.setattr(s, 'ADMIN_SHA', s.digest(raw))
    monkeypatch.setattr(s, 'ADMIN_MANIFEST', s.digest(raw_manifest))
    return raw


def test_valid_archive_has_exact_content(monkeypatch):
    values = s.admin_contents(package(monkeypatch), NAMES)
    assert set(values) == NAMES | {'install-manifest.json'}
    assert all(values[n] == b'# synthetic code\n' for n in NAMES)


@pytest.mark.parametrize('extra', ['../escape.py', '/tmp/escape.py', 'app/../../escape',
                                  'app\\escape.py', 'extra.py', 'app/'])
def test_traversal_and_extra_members_rejected(monkeypatch, extra):
    raw = package(monkeypatch, change=lambda m, e: e.append((extra, b'bad', 0o100444)))
    with pytest.raises(ValueError, match='inventory'):
        s.admin_contents(raw, NAMES)


def test_duplicate_member_rejected(monkeypatch):
    with pytest.warns(UserWarning):
        raw = package(monkeypatch, change=lambda m, e: e.append(e[0]))
    with pytest.raises(ValueError, match='inventory'):
        s.admin_contents(raw, NAMES)


def test_missing_member_rejected(monkeypatch):
    raw = package(monkeypatch, change=lambda m, e: e.pop())
    with pytest.raises(ValueError, match='inventory'):
        s.admin_contents(raw, NAMES)


@pytest.mark.parametrize('mode', [0o120444, 0o040755, 0o100644, 0o100666, 0o010444])
def test_symlink_directory_writable_or_special_member_rejected(monkeypatch, mode):
    def change(m, entries):
        n, raw, _ = entries[0]
        entries[0] = (n, raw, mode)
    with pytest.raises(ValueError, match='member'):
        s.admin_contents(package(monkeypatch, change=change), NAMES)


@pytest.mark.parametrize('field,value', [('source', '0'*40), ('type', 'other'), ('files', {})])
def test_authenticated_wrong_manifest_rejected(monkeypatch, field, value):
    with pytest.raises(ValueError, match='manifest'):
        s.admin_contents(package(monkeypatch, change=lambda m, e: m.update({field: value})), NAMES)


def test_member_hash_mismatch_rejected(monkeypatch):
    def change(m, e):
        m['files'][e[0][0]] = '0'*64
    with pytest.raises(ValueError, match='member digest'):
        s.admin_contents(package(monkeypatch, change=change), NAMES)


def test_outer_digest_is_required(monkeypatch):
    raw = package(monkeypatch)
    monkeypatch.setattr(s, 'ADMIN_SHA', '0'*64)
    with pytest.raises(ValueError, match='ZIP digest'):
        s.admin_contents(raw, NAMES)


def test_manifest_digest_is_required(monkeypatch):
    raw = package(monkeypatch)
    monkeypatch.setattr(s, 'ADMIN_MANIFEST', '0'*64)
    with pytest.raises(ValueError, match='manifest'):
        s.admin_contents(raw, NAMES)


def test_duplicate_json_fields_rejected():
    with pytest.raises(ValueError, match='Duplicate'):
        s.decode(b'{"source":"one","source":"two"}')


@pytest.mark.parametrize('field', ['application_package_sha256', 'application_git_sha', 'revision'])
def test_drifted_live_application_rejected(field):
    source = {'application_package_sha256': s.LIVE_PACKAGE, 'application_git_sha': s.LIVE_SOURCE,
              'revision': 'fa1023oc'}
    s.approved_source(source)
    source[field] = 'other'
    with pytest.raises(ValueError, match='Live application changed'):
        s.approved_source(source)


def test_copy_bound_retains_failed_copy_and_never_overwrites(tmp_path):
    source, output = tmp_path / 'public.zip', tmp_path / 'copied.zip'
    source.write_bytes(b'public program fixture')
    source.chmod(0o600)
    boot = SimpleNamespace(no_acl=lambda fd: None)
    with pytest.raises(ValueError, match='Input changed'):
        s.copy_bound(source, output, '0'*64, source.stat().st_size, boot)
    assert output.read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        s.copy_bound(source, output, s.digest(source.read_bytes()), source.stat().st_size, boot)
    assert output.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'writable', 'wrong_size'])
def test_copy_bound_rejects_bad_source_before_output(tmp_path, kind):
    source, actual, output = tmp_path / 'source', tmp_path / 'actual', tmp_path / 'out'
    actual.write_bytes(b'fixture'); actual.chmod(0o600)
    size = 7
    if kind == 'symlink':
        source.symlink_to(actual)
    elif kind == 'hardlink':
        os.link(actual, source)
    else:
        source.write_bytes(b'fixture'); source.chmod(0o666 if kind == 'writable' else 0o600)
        if kind == 'wrong_size':
            size = 8
    with pytest.raises(ValueError):
        s.copy_bound(source, output, s.digest(b'fixture'), size, SimpleNamespace(no_acl=lambda fd: None))
    assert not output.exists()


def test_existing_stage_is_not_repaired_or_deleted(tmp_path):
    destination = tmp_path / 'existing'; destination.mkdir(mode=0o700)
    before = destination.stat().st_mode
    with pytest.raises(FileExistsError):
        s.new_directory(destination, SimpleNamespace())
    assert destination.stat().st_mode == before


@pytest.mark.parametrize('arguments', [['apply'], ['uat-export-apply'], ['--path', '/tmp'],
                                      ['_finalize', 'extra'], ['service-apply']])
def test_stage_b_and_configurable_arguments_rejected_before_access(monkeypatch, arguments):
    for name in ('getuid', 'geteuid', 'getgid', 'getegid'):
        monkeypatch.setattr(s.os, name, lambda: 0)
    monkeypatch.setattr(s.sys, 'flags', SimpleNamespace(isolated=1))
    monkeypatch.setattr(s.sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(s.sys, 'argv', ['stage-a.py', *arguments])
    monkeypatch.setattr(s, '__file__', str(s.TRANSACTION / 'stage-a.py'))
    monkeypatch.setattr(s, 'trusted_boot', lambda **kwargs: pytest.fail('must reject before installed access'))
    with pytest.raises(ValueError, match='No configurable'):
        s.main()


def test_user_tree_cannot_execute_without_sha_launcher(monkeypatch):
    for name in ('getuid', 'geteuid', 'getgid', 'getegid'):
        monkeypatch.setattr(s.os, name, lambda: 0)
    monkeypatch.setattr(s.sys, 'flags', SimpleNamespace(isolated=1))
    monkeypatch.setattr(s.sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(s, 'trusted_boot', lambda **kwargs: pytest.fail('user tree must not execute'))
    with pytest.raises(ValueError, match='Only protected admission'):
        s.main()
