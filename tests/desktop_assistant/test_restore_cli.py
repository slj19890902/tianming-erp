import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from desktop_assistant import restore_cli, credential_transfer
from desktop_assistant.release_request import public_identity
from desktop_assistant.storage import sha
from tests.desktop_assistant import test_recovery as fixtures


@pytest.fixture
def case(monkeypatch):
    instance = fixtures.RecoveryTests()
    instance.setUp()
    try:
        backup = instance.manager.backup(fixtures.PASSWORD, instance.nas)
        public = instance.root / 'public.pem'
        public.write_bytes(instance.public)
        args = SimpleNamespace(root=instance.root / 'restore-target', backup=backup,
            backup_sha256=sha(backup), public_key=public,
            publisher_sha256=public_identity(instance.public)[1], credentials=None,
            backup_password_from_transfer=False)
        monkeypatch.setattr(restore_cli, '_home_directory', lambda: instance.root)
        monkeypatch.setattr('sys.platform', 'darwin')
        yield instance, args
    finally:
        instance.tearDown()


def test_readonly_preflight_and_wrong_identities_never_prompt_or_create_target(case):
    instance, args = case
    assert restore_cli.preflight(args)[0] == args.root
    for field in ('publisher_sha256', 'backup_sha256'):
        original = getattr(args, field)
        setattr(args, field, '0'*64)
        with pytest.raises(ValueError):
            restore_cli.run(args, lambda message: pytest.fail('must not prompt'))
        setattr(args, field, original)
    assert not args.root.exists()


def test_existing_link_and_outside_targets_refused(case):
    instance, args = case
    existing = instance.root / 'existing'
    existing.mkdir()
    linked = instance.root / 'linked'
    linked.symlink_to(existing, target_is_directory=True)
    for root in (existing, linked / 'new', instance.root.parent / 'outside-target'):
        with pytest.raises(ValueError):
            restore_cli.validate_target(root)


def test_authenticated_transfer_supplies_password_in_memory_only(case, monkeypatch):
    instance, args = case
    from desktop_assistant.storage import write_json
    write_json(instance.manager.root / 'preferences.json', {'protected_password':'old-synthetic-cipher'})
    monkeypatch.setattr(credential_transfer, 'unprotect', lambda *a: fixtures.PASSWORD)
    record = credential_transfer.collect(instance.manager.root, bound_backup_sha256=args.backup_sha256)
    args.credentials = instance.root / 'separate.tmencrypted'
    args.credentials.write_bytes(credential_transfer.seal(record, 'synthetic-transfer-only-password'))
    args.backup_password_from_transfer = True
    calls = []
    class Manager:
        def __init__(self, root, public):
            self.root = root
            (root / 'control').mkdir(parents=True)
        def restore(self, backup, password, **kwargs):
            assert password == fixtures.PASSWORD
            assert kwargs['credential_password'] == 'synthetic-transfer-only-password'
            assert kwargs['expected_backup_sha256'] == args.backup_sha256
            assert kwargs['credentials'] == args.credentials.read_bytes()
            calls.append('restore')
            self.state = {'restored_backup_sha256':args.backup_sha256}
            return {'version':'synthetic','started':False,'credentials':{'status':'restored'}}
        def start(self):
            pytest.fail('restore CLI must not start ERP')
    monkeypatch.setattr('desktop_assistant.manager.Manager', Manager)
    monkeypatch.setattr('app.core.home_rehearsal.install_network_guard', lambda: calls.append('guard'))
    result = restore_cli.run(args, lambda message: 'synthetic-transfer-only-password')
    assert calls == ['guard', 'restore']
    encoded = json.dumps(result) + (args.root / 'control/home-restore-receipt.json').read_text()
    assert fixtures.PASSWORD not in encoded and 'synthetic-transfer-only-password' not in encoded
    assert result['started'] is False and result['factory_cutover'] is False


def test_wrong_transfer_password_leaves_target_absent(case):
    instance, args = case
    record = credential_transfer.collect(instance.manager.root, bound_backup_sha256=args.backup_sha256)
    args.credentials = instance.root / 'separate.tmencrypted'
    args.credentials.write_bytes(credential_transfer.seal(record, 'correct-synthetic-transfer-password'))
    with pytest.raises(ValueError, match='口令错误'):
        restore_cli.run(args, lambda message: 'wrong-synthetic-transfer-password')
    assert not args.root.exists()


def test_mismatched_completed_state_never_publishes_cli_success_receipt(case, monkeypatch):
    _, args = case
    class Manager:
        def __init__(self, root, public):
            (root/'control').mkdir(parents=True)
            self.state = {'restored_backup_sha256':'0'*64}
        def restore(self, *a, **kw):
            return {'version':'synthetic', 'started':False}
    monkeypatch.setattr('desktop_assistant.manager.Manager', Manager)
    monkeypatch.setattr('app.core.home_rehearsal.install_network_guard', lambda: None)
    with pytest.raises(ValueError, match='恢复结果'):
        restore_cli.run(args, lambda _: fixtures.PASSWORD)
    assert not (args.root/'control/home-restore-receipt.json').exists()


def test_valid_backup_replaced_during_password_prompt_is_not_restored(case, monkeypatch):
    import shutil
    instance, args = case
    alternate = instance.manager.backup(fixtures.PASSWORD, instance.nas)
    assert sha(alternate) != args.backup_sha256
    def prompt(message):
        shutil.copyfile(alternate, args.backup)
        return fixtures.PASSWORD
    monkeypatch.setattr('app.core.home_rehearsal.install_network_guard', lambda: None)
    monkeypatch.setattr('desktop_assistant.manager.extract_verified', lambda *a: pytest.fail('must reject before extraction'))
    monkeypatch.setattr('desktop_assistant.credential_restore.prepare', lambda *a: pytest.fail('must not rewrap'))
    with pytest.raises(ValueError, match='批准摘要'):
        restore_cli.run(args, prompt)
    assert not (args.root/'state.json').exists()
    assert not (args.root/'control/home-restore-receipt.json').exists()
    assert list((args.root/'shared').iterdir()) == []
    assert list((args.root/'staging').glob('*/recovery.zip'))  # Failure evidence retained.


def test_transfer_replaced_at_prompt_uses_original_authenticated_bytes_for_full_restore(case, monkeypatch):
    import hashlib
    from copy import deepcopy
    from desktop_assistant import credential_store
    from desktop_assistant.storage import write_json, pack_recovery, encrypt_file, database_info
    instance, args = case
    # External transfers belong to the old full-backup format without an
    # embedded credential archive; never relax the one-credential-source gate.
    shared = instance.manager.root/'shared'
    raw = instance.root/'legacy-external.zip'
    pack_recovery(shared, {'release.zip':instance.package}, raw,
        {'type':'tianming.recovery.v1', 'created':'2026-10-10T10:00:00+08:00',
         'release':sha(instance.package), 'version':'one',
         'database':database_info(shared/'data/carton_erp.sqlite3'),
         'source_shared':str(shared), 'schema_authority':None})
    args.backup = instance.root/'legacy-external.tmbackup'
    encrypt_file(raw, args.backup, fixtures.PASSWORD)
    args.backup_sha256 = sha(args.backup)
    write_json(instance.manager.root/'preferences.json', {'protected_password':'old-synthetic-cipher'})
    monkeypatch.setattr(credential_transfer, 'unprotect', lambda *a: fixtures.PASSWORD)
    record = credential_transfer.collect(instance.manager.root, bound_backup_sha256=args.backup_sha256)
    original = credential_transfer.seal(record, 'synthetic-transfer-password')
    changed = deepcopy(record)
    changed['entries']['backup']['value'] = 'different-synthetic-backup-password'
    alternate = credential_transfer.seal(changed, 'synthetic-transfer-password')
    args.credentials = instance.root/'replaceable-transfer.tmencrypted'
    args.credentials.write_bytes(original)
    args.backup_password_from_transfer = True
    stored = {}
    def protect(value, purpose):
        reference = 'synthetic-new-' + purpose
        stored[reference] = value
        return reference
    monkeypatch.setattr(credential_store, 'protect', protect)
    monkeypatch.setattr(credential_store, 'unprotect', lambda value, purpose: stored[value])
    monkeypatch.setattr('app.core.home_rehearsal.install_network_guard', lambda: None)
    def prompt(message):
        args.credentials.write_bytes(alternate)
        return 'synthetic-transfer-password'
    result = restore_cli.run(args, prompt)
    assert result['credential_package_sha256'] == hashlib.sha256(original).hexdigest()
    assert result['credential_package_sha256'] != sha(args.credentials)
    assert stored == {'synthetic-new-backup': fixtures.PASSWORD}
    assert result['result']['credentials']['status'] == 'restored'
    assert result['backup_sha256'] == args.backup_sha256 and not result['started']
    assert not (args.root/'control/process.json').exists()


@pytest.mark.parametrize('invalid', ['', True, 'g'*64, 'A'*64, '0'*63])
def test_core_rejects_malformed_expected_hash_before_staging(case, invalid):
    instance, args = case
    manager = fixtures.TestManager(args.root, instance.public)
    with pytest.raises(ValueError, match='摘要格式'):
        manager.restore(args.backup, fixtures.PASSWORD, expected_backup_sha256=invalid)
    assert list((args.root/'staging').iterdir()) == []


def test_getpass_cannot_fall_back_to_echoed_input(case):
    import getpass
    import warnings
    _, args = case
    def prompt(message):
        warnings.warn('synthetic hidden input unavailable', getpass.GetPassWarning)
        pytest.fail('must not reach getpass fallback')
    with pytest.raises(getpass.GetPassWarning): restore_cli.run(args, prompt)
    assert not args.root.exists()


def test_parser_never_echoes_secret_or_accepts_ambiguous_flags(case, capsys):
    _, args = case
    parser = restore_cli.parser()
    assert not parser.allow_abbrev
    for prefix in ([], command(args)[3:]):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args([*prefix, '--password=synthetic-secret-not-an-argument'])
        captured = capsys.readouterr()
        assert 'synthetic-secret' not in str(caught.value) + captured.out + captured.err


@pytest.mark.parametrize('redirected', ['stdin', 'stdout', 'stderr'])
def test_all_three_terminal_streams_required_before_any_restore(case, monkeypatch, redirected):
    _, args = case
    monkeypatch.setattr(restore_cli, 'parser', lambda: SimpleNamespace(parse_args=lambda: args))
    for name in ('stdin', 'stdout', 'stderr'):
        monkeypatch.setattr(getattr(restore_cli.sys, name), 'isatty', lambda n=name: n != redirected)
    monkeypatch.setattr(restore_cli, 'run', lambda *a: pytest.fail('must refuse before prompt'))
    with pytest.raises(SystemExit, match='交互终端'): restore_cli.main()
    assert not args.root.exists()


def test_native_exception_is_not_disclosed(case, monkeypatch, capsys):
    _, args = case
    monkeypatch.setattr(restore_cli, 'parser', lambda: SimpleNamespace(parse_args=lambda: args))
    for name in ('stdin', 'stdout', 'stderr'):
        monkeypatch.setattr(getattr(restore_cli.sys, name), 'isatty', lambda: True)
    def failed(*a): raise ValueError('synthetic-secret-from-keychain')
    monkeypatch.setattr(restore_cli, 'run', failed)
    with pytest.raises(SystemExit) as caught: restore_cli.main()
    captured = capsys.readouterr()
    assert 'synthetic-secret' not in str(caught.value) + captured.out + captured.err


def command(args):
    return [sys.executable, '-m', 'desktop_assistant.restore_cli', '--root', str(args.root),
        '--backup', str(args.backup), '--backup-sha256', args.backup_sha256,
        '--public-key', str(args.public_key), '--publisher-sha256', args.publisher_sha256]


def test_cli_rejects_pipe_without_creating_target(case):
    _instance, args = case
    result = subprocess.run(command(args), input=fixtures.PASSWORD, text=True, capture_output=True)
    assert result.returncode != 0 and '交互终端' in result.stderr
    assert fixtures.PASSWORD not in result.stdout + result.stderr
    assert not args.root.exists()


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual Mac pseudo-terminal restore')
def test_real_hidden_terminal_input_and_verified_restore_without_start(case):
    import pty
    instance, args = case
    # Child uses the actual account home, not the unit-test home override.
    with tempfile.TemporaryDirectory(prefix='tianming-restore-cli-', dir=Path.home() / 'Library/Caches') as owned:
        args.root = Path(owned) / 'new-installation'
        master, slave = pty.openpty()
        process = subprocess.Popen(command(args), stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        transcript, sent = b'', False
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .2)
                if ready:
                    try:
                        block = os.read(master, 65536)
                    except OSError:
                        break
                    if not block:
                        break
                    transcript += block
                    if not sent and '隐藏输入'.encode() in transcript:
                        os.write(master, (fixtures.PASSWORD+'\n').encode())
                        sent = True
                elif process.poll() is not None:
                    break
            assert process.wait(timeout=2) == 0, transcript.decode(errors='replace')
            assert sent and fixtures.PASSWORD.encode() not in transcript
            receipt = json.loads((args.root / 'control/home-restore-receipt.json').read_text())
            assert not receipt['started'] and not receipt['factory_cutover']
            assert not (args.root / 'control/process.json').exists()
            assert sha(args.root / 'shared/data/carton_erp.sqlite3') == sha(instance.manager.root / 'shared/data/carton_erp.sqlite3')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)
