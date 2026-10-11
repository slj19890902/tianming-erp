"""Real publication/signature/filesystem flow with explicit synthetic OS identity."""
from contextlib import contextmanager
from copy import deepcopy
import os
import sys
from types import SimpleNamespace

import pytest

from desktop_assistant import mac_release_publish as m, mac_keychain_install as b
from desktop_assistant import mac_system_service as service
from tests.desktop_assistant.test_mac_release_publish import case, publish, REAL_PREFLIGHT, make_package

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac publication contract')


@pytest.fixture
def pristine(case, monkeypatch):
    # Remove only the empty folders created by this synthetic fixture.
    for name in m.MANAGED.values(): (case.base/'instance/shared'/name).rmdir()
    (case.base/'instance/shared').rmdir()
    actual = (case.base/'instance').stat()
    case.context.update(uid=actual.st_uid, gid=actual.st_gid)
    case.context_raw = b.canonical(case.context)
    (case.base/'keychain-context.json').write_bytes(case.context_raw)
    case.old_package = case.package
    case.package = case.root/'service-native.zip'
    case.manifest_raw, case.manifest = make_package(case.package, case.key,
        metadata={'runtime_capabilities': {'immutable_execution_v1': 1, 'system_service_v1': 1, 'service_restore_v1': 1},
                  'reader_capabilities': {'restore_provenance_v1': 1}},
        additions={name: b'# synthetic signed restore protocol' for name in m.SERVICE_RESTORE_FILES})
    case.plan = m.prepare(case.package, case.public, case.context_raw, case.code,
                         publisher_sha256=b.digest(case.public), pristine_bootstrap=True)
    @contextmanager
    def checked(path, **kw):
        m.guard(path, **kw)  # Root/service UID ownership is explicitly simulated.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try: yield fd
        finally: os.close(fd)
    keychain = SimpleNamespace(CONTEXT=case.base/'keychain-context.json',
        _identity=lambda *a, **k: None, _checked_path=checked, _no_acl=lambda fd: None)
    monkeypatch.setattr(b, 'modules', lambda: (SimpleNamespace(_administrator=lambda: None), keychain))
    monkeypatch.setattr(m, 'preflight', REAL_PREFLIGHT)
    monkeypatch.setattr(service, 'PLIST', case.root/'synthetic.plist')
    monkeypatch.setattr(service, 'present', lambda: False)
    case.keychain = keychain
    return case


def test_old_package_remains_ordinary_only_and_cannot_forge_pristine_plan(pristine):
    c = pristine
    ordinary = m.prepare(c.old_package, c.public, c.context_raw, c.code, publisher_sha256=b.digest(c.public))
    assert ordinary['type'] == m.TYPE
    with pytest.raises(ValueError, match='专用完整恢复'):
        m.prepare(c.old_package, c.public, c.context_raw, c.code,
                  publisher_sha256=b.digest(c.public), pristine_bootstrap=True)
    with pytest.raises(ValueError, match='专用完整恢复'):
        publish(c, {**ordinary, 'type': m.PRISTINE_TYPE}, c.old_package)
    assert not (c.base/'published').exists() and not c.child.called


@pytest.mark.parametrize('capability', [None, True, '1', 0, 2])
def test_pristine_requires_exact_signed_service_restore_version(pristine, capability):
    c = pristine
    path = c.root/'bad-restore-version.zip'
    make_package(path, c.key,
        metadata={'runtime_capabilities': {'immutable_execution_v1': 1, 'system_service_v1': 1, 'service_restore_v1': capability},
                  'reader_capabilities': {'restore_provenance_v1': 1}},
        additions={name: b'# synthetic' for name in m.SERVICE_RESTORE_FILES})
    with pytest.raises(ValueError, match='专用完整恢复'):
        m.prepare(path, c.public, c.context_raw, c.code, publisher_sha256=b.digest(c.public), pristine_bootstrap=True)


@pytest.mark.parametrize('missing', sorted(m.SERVICE_RESTORE_FILES))
def test_pristine_requires_both_signed_restore_modules(pristine, missing):
    c = pristine
    path = c.root/'missing-restore-entry.zip'
    make_package(path, c.key, metadata={'runtime_capabilities': c.manifest['runtime_capabilities'],
                                     'reader_capabilities': c.manifest['reader_capabilities']},
                 additions={name: b'# synthetic' for name in m.SERVICE_RESTORE_FILES - {missing}})
    with pytest.raises(ValueError, match='专用完整恢复'):
        m.prepare(path, c.public, c.context_raw, c.code, publisher_sha256=b.digest(c.public), pristine_bootstrap=True)


def test_pristine_mode_is_bound_to_plan_and_does_not_initialize_instance(pristine):
    c = pristine
    result = publish(c)
    assert c.plan['type'] == m.PRISTINE_TYPE
    assert result['pristine_bootstrap'] and not result['data_restored']
    assert not result['system_daemon_installed'] and not result['factory_activated']
    assert list((c.base/'instance').iterdir()) == []
    slot = c.base/'published'/c.plan['package_sha256']
    assert {p.name for p in slot.iterdir()} == {'package.zip', 'migration', 'server'}
    for relative, name in m.MANAGED.items():
        link = slot/'server'/relative
        assert link.is_symlink() and not link.exists()
        assert link.resolve() == c.base/'instance/shared'/name


def test_pristine_migration_resolves_but_server_is_still_rejected(pristine, monkeypatch):
    from desktop_assistant import mac_published_runtime as resolver, release_verification
    c = pristine
    publish(c)
    for name, value in {'BASE':c.base, 'ROOT':c.base/'instance', 'PUBLISHED':c.base/'published',
                        'PUBLIC':c.base/'publisher-public.pem', 'REGISTRY':c.base/'published.json'}.items():
        monkeypatch.setattr(resolver, name, value)
    monkeypatch.setattr(resolver.keychain, 'CONTEXT', c.base/'keychain-context.json')
    monkeypatch.setattr(resolver.keychain, 'load_context', lambda: c.context)
    @contextmanager
    def checked(path, **kw):
        m.guard(path, **kw)
        fd = os.open(path, os.O_RDONLY)
        try: yield fd
        finally: os.close(fd)
    monkeypatch.setattr(resolver.keychain, '_checked_path', checked)
    def immutable(path, *, directory=False, link=False):
        m.guard(path, directory=directory, link=link,
                mode=0o755 if directory or path.stat().st_mode & 0o111 else 0o644)
    monkeypatch.setattr(resolver, 'immutable', immutable)
    monkeypatch.setattr(release_verification, 'immutable', immutable)
    tree, manifest = resolver.resolve(c.base/'instance', c.plan['package_sha256'], c.public, kind='migration')
    assert tree.name == 'migration' and manifest == c.manifest
    with pytest.raises(ValueError):
        resolver.resolve(c.base/'instance', c.plan['package_sha256'], c.public, kind='server')
    assert list((c.base/'instance').iterdir()) == []


def test_ordinary_publication_still_requires_real_data_directories(pristine):
    plan = {**pristine.plan, 'type':m.TYPE}
    with pytest.raises((ValueError, OSError)): publish(pristine, plan)
    assert not (pristine.base/'published').exists()
    assert not pristine.child.called


def test_base_and_instance_on_same_foreign_device_are_not_local_data_volume(pristine, monkeypatch):
    device = pristine.base.stat().st_dev
    assert (pristine.base/'instance').stat().st_dev == device
    monkeypatch.setattr(m, 'DATA_VOLUME', SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=device + 1)))
    with pytest.raises(ValueError, match='固定'): publish(pristine)
    assert not (pristine.base/'published').exists() and not pristine.child.called


@pytest.mark.parametrize('entry', ['.hidden', 'shared', 'state.json', 'control', 'packages'])
def test_any_instance_entry_refuses_bootstrap_before_publication(pristine, entry):
    (pristine.base/'instance'/entry).mkdir()
    with pytest.raises(ValueError, match='完全为空'): publish(pristine)
    assert not (pristine.base/'published').exists() and not pristine.child.called


@pytest.mark.parametrize('entry', ['published', 'published.json', 'publication-old.pending.json',
    'publication-old.verified.json', 'published-old.json', 'system-service.json',
    'system-service-old.pending.json', 'system-service-old.json', 'system-service.stdout.log',
    'system-service.stderr.log', 'plist'])
def test_prior_publication_or_service_residue_including_dangling_links_is_preserved(pristine, entry):
    path = service.PLIST if entry == 'plist' else pristine.base/entry
    path.symlink_to(pristine.root/'nonexistent-synthetic-target')
    with pytest.raises(ValueError, match='现场'): publish(pristine)
    assert path.is_symlink() and not pristine.child.called


@pytest.mark.parametrize('fault', ['registered', 'query-error'])
def test_actual_service_query_must_confirm_absence(pristine, monkeypatch, fault):
    def present():
        if fault == 'query-error': raise OSError('synthetic query failure')
        return True
    monkeypatch.setattr(service, 'present', present)
    with pytest.raises((ValueError, OSError)): publish(pristine)
    assert not (pristine.base/'published').exists() and not pristine.child.called


@pytest.mark.parametrize('fault', ['content', 'transient-content', 'replacement', 'gid', 'mode', 'acl', 'service'])
def test_mutation_during_signature_check_never_publishes_approval(pristine, monkeypatch, fault):
    c = pristine
    root = c.base/'instance'
    def child(*args):
        if fault in ('content', 'transient-content'):
            (root/'.changed').write_bytes(b'synthetic')
            if fault == 'transient-content': (root/'.changed').unlink()
        elif fault == 'replacement':
            root.rename(c.base/'original-instance')
            root.mkdir(mode=0o700)
        elif fault == 'gid':
            # Simulate a changed kernel GID, not the detached fixture context.
            actual_fstat = os.fstat
            inode = root.stat().st_ino
            def changed_gid(fd):
                info = actual_fstat(fd)
                if info.st_ino != inode: return info
                fields = {name:getattr(info, name) for name in dir(info) if name.startswith('st_')}
                return SimpleNamespace(**{**fields, 'st_gid':info.st_gid + 1})
            monkeypatch.setattr(os, 'fstat', changed_gid)
        elif fault == 'mode': root.chmod(0o755)
        elif fault == 'acl':
            def refusal(fd): raise ValueError('synthetic extra ACL')
            monkeypatch.setattr(c.keychain, '_no_acl', refusal)
        elif fault == 'service': service.PLIST.write_bytes(b'synthetic')
    c.child.side_effect = child
    with pytest.raises((ValueError, OSError)): publish(c)
    assert not (c.base/'published.json').exists()
    assert (c.base/'published'/c.plan['package_sha256']).is_dir()
    assert list(c.base.glob('publication-*.pending.json'))


def test_repeat_bootstrap_refuses_even_empty_instance(pristine):
    publish(pristine)
    before = (pristine.base/'published.json').read_bytes()
    with pytest.raises(ValueError, match='现场'): publish(pristine)
    assert (pristine.base/'published.json').read_bytes() == before
    assert list((pristine.base/'instance').iterdir()) == []


@pytest.mark.parametrize('fault', ['changed-type-sha', 'unknown-type', 'extra-field'])
def test_mode_is_not_an_unbound_runtime_override(pristine, fault):
    plan = deepcopy(pristine.plan)
    approved = b.digest(b.canonical(plan))
    if fault == 'changed-type-sha': plan['type'] = m.TYPE
    elif fault == 'unknown-type': plan['type'] = 'unapproved'
    else: plan['pristine_bootstrap'] = True
    raw = b.canonical(plan)
    with pytest.raises(ValueError):
        m.publish(raw, approved if fault == 'changed-type-sha' else b.digest(raw), pristine.package)
    assert not (pristine.base/'published').exists()
