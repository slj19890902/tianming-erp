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
            calls.append('restore')
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
