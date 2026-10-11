from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant.manager import Manager
from desktop_assistant.storage import (archive_path, database_info, decrypt_file, extract_verified,
                                      pack_tree, read_json, sha, write_json)
from desktop_assistant.windows import protect, unprotect

PASSWORD = 'isolated-recovery-test-password'


class TestManager(Manager):
    __test__ = False
    @contextmanager
    def lock(self):
        from tests.desktop_assistant.synthetic_storage import approved_local_storage
        with approved_local_storage(self.root.parent), super().lock():
            yield
    def _backup_stopped(self, *args, **kwargs):
        from tests.desktop_assistant.synthetic_storage import approved_local_storage
        with approved_local_storage(self.root.parent):
            return super()._backup_stopped(*args, **kwargs)
    def _update_locked(self, *args, **kwargs):
        from tests.desktop_assistant.synthetic_storage import approved_local_storage
        with approved_local_storage(self.root.parent):
            return super()._update_locked(*args, **kwargs)
    def _process(self):
        return None
    def stop(self):
        pass
    def start(self):
        if self.manifest()['version'] == 'bad-start':
            raise ValueError('synthetic startup failure')


class RecoveryTests(unittest.TestCase):
    def test_virtual_nas_archive_path_only_handles_volume_error(self):
        from unittest.mock import patch
        error = OSError('virtual volume')
        error.winerror = 1005
        with patch.object(Path, 'resolve', side_effect=error):
            self.assertEqual(archive_path(self.nas / 'new-job'), self.nas / 'new-job')
        with patch.object(Path, 'resolve', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                archive_path(self.nas)

    def test_virtual_nas_archive_path_rejects_reparse_points(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        error = OSError('virtual volume')
        error.winerror = 1005
        with patch.object(Path, 'resolve', side_effect=error), patch.object(
                Path, 'lstat', return_value=SimpleNamespace(st_mode=0, st_file_attributes=0x400)):
            with self.assertRaisesRegex(ValueError, '重解析点'):
                archive_path(self.nas)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.manager = TestManager(self.root / 'installation', self.public)
        self.nas = self.root / 'mock-nas'
        self.nas.mkdir()
        self.package = self.release('one')
        release = self.manager.stage_release(self.package)
        shared = self.manager.root / 'shared'
        (shared / 'data').mkdir()
        with closing(sqlite3.connect(shared / 'data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE alembic_version(version_num TEXT)')
            db.execute("INSERT INTO alembic_version VALUES ('r1')")
            for name in ('users', 'customers', 'products', 'sales_orders', 'sales_order_items'):
                db.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY, note TEXT)')
                db.execute(f"INSERT INTO {name} VALUES (1, 'synthetic')")
            db.commit()
        (shared / 'data/drawing.pdf').write_bytes(b'synthetic attachment')
        write_json(shared / 'environment.json', {'ERP_PORT': '18080', 'ERP_SECRET_KEY': 'synthetic-only'})
        write_json(self.manager.root / 'state.json', {'current': release['id'], 'previous': None})

    def tearDown(self):
        self.temp.cleanup()

    def release(self, version, revision='r1', extra_tables=(), *, inventory_reader=False):
        source = self.root / ('source-' + version)
        source.mkdir()
        for name in ('runtime/python.exe', 'main.py', 'app/main.py', 'desktop_assistant/server_entry.py'):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(version)
        models = source / 'app/models'
        models.mkdir()
        (models / '__init__.py').write_text(
            'from sqlalchemy.orm import DeclarativeBase\n'
            'class Base(DeclarativeBase):\n    pass\n'
            'from app.models.fixture import FixtureUser, FixtureCustomer, FixtureProduct, FixtureOrder, FixtureOrderItem\n',
            encoding='utf-8',
        )
        tables = ('users', 'customers', 'products', 'sales_orders', 'sales_order_items', *extra_tables)
        class_names = ('FixtureUser', 'FixtureCustomer', 'FixtureProduct', 'FixtureOrder', 'FixtureOrderItem')
        definitions = ['from sqlalchemy.orm import Mapped, mapped_column', 'from app.models import Base']
        for index, table in enumerate(tables):
            class_name = class_names[index] if index < len(class_names) else f'FixtureExtra{index}'
            definitions.extend((
                f'class {class_name}(Base):',
                f'    __tablename__ = {table!r}',
                '    id: Mapped[int] = mapped_column(primary_key=True)',
                '    note: Mapped[str]',
            ))
        (models / 'fixture.py').write_text('\n'.join(definitions) + '\n', encoding='utf-8')
        target = self.root / (version + '.zip')
        metadata = {'type': 'tianming.release.v1', 'version': version, 'revision': revision}
        if inventory_reader:
            metadata['reader_capabilities'] = {'order_inventory_v1': 1}
        pack_tree(source, target, metadata, self.key)
        return target

    def test_order_inventory_activation_survives_full_backup_and_restore(self):
        from desktop_assistant.order_inventory_contract import STATE_KEY
        release = self.manager.stage_release(self.release('reader', inventory_reader=True))
        marker = {'reader_capability': 'order_inventory_v1', 'version': 1,
                  'activated_package': release['id'], 'activated_at': '2026-10-10T10:00:00+08:00',
                  'pre_activation_package': self.manager.state['current'],
                  'pre_activation_backup': r'Z:\\old-evidence\\retained.tmbackup',
                  'next_release_must_preserve_reader_contract': True}
        write_json(self.manager.root / 'state.json', {'current': release['id'], STATE_KEY: marker})
        before = sha(self.manager.root / 'shared/data/carton_erp.sqlite3')
        backup = self.manager.backup(PASSWORD, self.nas)
        new = TestManager(self.root / 'new-reader-host', self.public)
        result = new.restore(backup, PASSWORD)
        self.assertFalse(result['started'])
        self.assertEqual(new.state[STATE_KEY], marker)
        self.assertEqual(new.state['restored_backup_sha256'], sha(backup))
        self.assertEqual(sha(new.root / 'shared/data/carton_erp.sqlite3'), before)
        self.assertTrue(new.compatible(release['id'], 'r1'))
        old = new.stage_release(self.package)
        self.assertFalse(new.compatible(old['id'], 'r1'))  # Same schema, missing reader.

    def test_legacy_backup_retains_signed_reader_without_fabricating_history(self):
        from desktop_assistant.order_inventory_contract import STATE_KEY
        from desktop_assistant.storage import pack_recovery, encrypt_file
        release = self.manager.stage_release(self.release('legacy-reader', inventory_reader=True))
        raw = self.root / 'legacy-reader-recovery.zip'
        # Reproduce the original archive format, which has no activation field.
        pack_recovery(self.manager.root / 'shared', {'release.zip': self.root / 'legacy-reader.zip'}, raw,
                      {'type': 'tianming.recovery.v1', 'created': '2026-10-10T10:00:00+08:00',
                       'release': release['id'], 'version': 'legacy-reader',
                       'database': database_info(self.manager.root / 'shared/data/carton_erp.sqlite3'),
                       'source_shared': str(self.manager.root / 'shared'), 'schema_authority': None})
        backup = self.nas / 'legacy-reader.tmbackup'
        encrypt_file(raw, backup, PASSWORD)
        new = TestManager(self.root / 'legacy-reader-host', self.public)
        self.assertFalse(new.restore(backup, PASSWORD)['started'])
        marker = new.state[STATE_KEY]
        self.assertEqual(marker['recovery_evidence']['backup_sha256'], sha(backup))
        self.assertFalse(marker['recovery_evidence']['original_activation_record_included'])
        self.assertNotIn('activated_at', marker)
        self.assertNotIn('pre_activation_package', marker)
        old = new.stage_release(self.package)
        self.assertFalse(new.compatible(old['id'], 'r1'))
        second_backup = new.backup(PASSWORD, self.nas)
        third = TestManager(self.root / 'second-reader-host', self.public)
        third.restore(second_backup, PASSWORD)
        self.assertEqual(third.state[STATE_KEY], marker)

    def test_backup_refuses_inventory_marker_without_authenticated_reader(self):
        from desktop_assistant.order_inventory_contract import STATE_KEY
        state = self.manager.state
        state[STATE_KEY] = {'reader_capability': 'order_inventory_v1', 'version': 1}
        write_json(self.manager.root / 'state.json', state)
        before = sha(self.manager.root / 'shared/data/carton_erp.sqlite3')
        with self.assertRaisesRegex(ValueError, 'reader 保护'):
            self.manager.backup(PASSWORD, self.nas)
        self.assertFalse(list(self.nas.glob('*.tmbackup')))
        actual = self.manager.state
        self.assertIn('reader 保护', actual.pop('backup_error'))
        self.assertEqual(actual, state)
        self.assertEqual(sha(self.manager.root / 'shared/data/carton_erp.sqlite3'), before)

    def test_recovery_rejects_inventory_marker_before_activation(self):
        from desktop_assistant.order_inventory_contract import STATE_KEY
        from desktop_assistant.storage import pack_recovery, encrypt_file
        raw = self.root / 'wrong-reader.zip'
        pack_recovery(self.manager.root / 'shared', {'release.zip': self.package}, raw,
                      {'type': 'tianming.recovery.v1', 'created': '2026-10-10T10:00:00+08:00',
                       'release': self.manager.state['current'], 'version': 'one',
                       'database': database_info(self.manager.root / 'shared/data/carton_erp.sqlite3'),
                       'source_shared': str(self.manager.root / 'shared'), 'schema_authority': None,
                       STATE_KEY: {'reader_capability': 'order_inventory_v1', 'version': 1}})
        backup = self.nas / 'wrong-reader.tmbackup'
        encrypt_file(raw, backup, PASSWORD)
        new = TestManager(self.root / 'wrong-reader-target', self.public)
        with self.assertRaisesRegex(ValueError, 'reader 保护'):
            new.restore(backup, PASSWORD)
        self.assertIsNone(new.state['current'])
        self.assertFalse(list((new.root / 'shared').iterdir()))
        self.assertFalse((new.root / 'preferences.json').exists())

    def test_backup_refuses_mutable_manifest_cache_disagreeing_with_signature(self):
        current = self.manager.state['current']
        path = self.manager.root / 'releases' / current / 'manifest.json'
        manifest = read_json(path)
        manifest['reader_capabilities'] = {'order_inventory_v1': 1}
        write_json(path, manifest)
        with self.assertRaisesRegex(ValueError, '原发布签名'):
            self.manager.backup(PASSWORD, self.nas)
        self.assertFalse(list(self.nas.glob('*.tmbackup')))
        self.assertEqual(self.manager.state['current'], current)

    def test_backup_input_digest_is_from_authenticated_stream_and_replacement_is_rejected(self):
        from unittest.mock import patch
        backup = self.manager.backup(PASSWORD, self.nas)
        raw = self.root / 'input-digest.zip'
        self.assertEqual(decrypt_file(backup, raw, PASSWORD), sha(backup))
        original_bytes = backup.read_bytes()
        def replacing_decrypt(source, destination, password):
            digest = decrypt_file(source, destination, password)
            source.write_bytes(b'synthetic replacement after authentication')
            return digest
        new = TestManager(self.root / 'changed-backup-target', self.public)
        with patch('desktop_assistant.manager.decrypt_file', side_effect=replacing_decrypt):
            with self.assertRaisesRegex(ValueError, '认证期间变化'):
                new.restore(backup, PASSWORD)
        self.assertIsNone(new.state['current'])
        self.assertFalse(list((new.root / 'shared').iterdir()))
        backup.write_bytes(original_bytes)  # Synthetic fixture only.
        later = TestManager(self.root / 'late-changed-backup-target', self.public)
        original_stage = later.stage_release
        def replacing_stage(*args, **kwargs):
            result = original_stage(*args, **kwargs)
            backup.write_bytes(b'synthetic replacement before promotion')
            return result
        with patch.object(later, 'stage_release', side_effect=replacing_stage):
            with self.assertRaisesRegex(ValueError, '提升前变化'):
                later.restore(backup, PASSWORD)
        self.assertIsNone(later.state['current'])
        self.assertFalse(list((later.root / 'shared').iterdir()))
        self.assertFalse((later.root / 'preferences.json').exists())

    def test_complete_encrypted_backup_and_new_machine_restore(self):
        database = self.manager.root / 'shared/data/carton_erp.sqlite3'
        (self.manager.root / 'shared/data/.raw-paper-proof').write_bytes(b'include hidden files')
        original = sha(database)
        backup = self.manager.backup(PASSWORD, self.nas)
        self.assertFalse(list((self.manager.root / 'backups').glob('*.tmbackup')))
        self.assertFalse(list((self.nas / 'tianming-backup-work').iterdir()))
        receipt = read_json(self.manager.root / 'control' / 'backup-receipts' / (backup.stem + '.json'))
        self.assertEqual(receipt['sha256'], sha(backup))
        self.assertEqual(receipt['storage'], 'nas')
        self.assertNotIn(b'synthetic-only', backup.read_bytes())
        new = TestManager(self.root / 'new-computer', self.public)
        result = new.restore(backup, PASSWORD)
        self.assertEqual(result['version'], 'one')
        self.assertFalse(result['started'])
        self.assertEqual(sha(new.root / 'shared/data/carton_erp.sqlite3'), original)
        self.assertEqual((new.root / 'shared/data/drawing.pdf').read_bytes(), b'synthetic attachment')
        self.assertEqual((new.root / 'shared/data/.raw-paper-proof').read_bytes(), b'include hidden files')
        self.assertEqual(sha(database), original)
        with self.assertRaisesRegex(ValueError, '全新安装'):
            new.restore(backup, PASSWORD)

    def test_imported_legacy_pdf_uses_managed_upload_folder(self):
        from desktop_assistant.attachments import rebind_pdf_sources
        old = self.root / 'old-erp'
        copied = self.root / 'copied'
        path = copied / 'legacy_uploads/order.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'pdf example')
        database = copied / 'sample.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            db.execute('create table pdf_order_training_samples(id integer, file_path text, file_sha256 text)')
            db.execute('insert into pdf_order_training_samples values(1,?,?)', (str(old / 'static/uploads/order.pdf'), sha(path)))
            db.commit()
        self.assertEqual(rebind_pdf_sources(database, old, copied, copied, importing=True), 1)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute('select file_path from pdf_order_training_samples').fetchone()[0], str(path))

    def test_import_refuses_running_lan_service_before_copy(self):
        from unittest.mock import patch, MagicMock
        from desktop_assistant.import_existing import import_existing
        source=self.root/'old-service';source.mkdir()
        (source/'.env').write_text('ERP_PORT=18080\nERP_BIND_HOST=192.168.3.80')
        target=TestManager(self.root/'empty',self.public)
        socket_instance=MagicMock();socket_instance.connect_ex.return_value=0
        with patch('desktop_assistant.import_existing.psutil.process_iter',return_value=[]), patch('desktop_assistant.import_existing.socket.socket') as socket_factory:
            socket_factory.return_value.__enter__.return_value=socket_instance
            with self.assertRaisesRegex(ValueError,'端口仍占用'):
                import_existing(target,source,self.package)
        socket_instance.connect_ex.assert_called_once_with(('192.168.3.80',18080))
        self.assertIsNone(target.state['current'])
        self.assertFalse(any((target.root/'shared').iterdir()))

    def test_preflight_understands_upload_references_and_does_not_write(self):
        from desktop_assistant.preflight import inspect
        source=self.root/'preflight';source.mkdir()
        attachment=source/'data/private_uploads/drawings/a.pdf';attachment.parent.mkdir(parents=True);attachment.write_bytes(b'a')
        database=source/'database.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            db.execute('create table product_drawings(id integer, image_path text, thumbnail_path text)')
            db.execute('insert into product_drawings values(1,?,?)',('private:drawings/a.pdf','private:../../../outside'))
            db.commit()
        before=sha(database);result=inspect(database,source)
        self.assertEqual(result['counts']['ok'],1)
        self.assertEqual(result['counts']['external'],1)
        self.assertFalse(result['ready_for_takeover'])
        self.assertEqual(sha(database),before)

    def test_wrong_password_and_corruption_never_activate_data(self):
        backup = self.manager.backup(PASSWORD, self.nas)
        new = TestManager(self.root / 'new-computer', self.public)
        with self.assertRaises(ValueError):
            new.restore(backup, PASSWORD + '-wrong')
        self.assertIsNone(new.state['current'])
        self.assertFalse(any((new.root / 'shared').iterdir()))
        raw = bytearray(backup.read_bytes())
        raw[-20] ^= 1
        corrupt = self.root / 'corrupt.tmbackup'
        corrupt.write_bytes(raw)
        with self.assertRaises(ValueError):
            new.restore(corrupt, PASSWORD)
        self.assertIsNone(new.state['current'])

    def test_pdf_absolute_path_rebound_only_in_restored_copy(self):
        shared = self.manager.root / 'shared'
        pdf = shared / 'data/drawing.pdf'
        database = shared / 'data/carton_erp.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE pdf_order_training_samples(id INTEGER PRIMARY KEY,file_path TEXT,file_sha256 TEXT)')
            db.execute('INSERT INTO pdf_order_training_samples VALUES (1,?,?)', (str(pdf), sha(pdf)))
            db.commit()
        original = sha(database)
        backup = self.manager.backup(PASSWORD, self.nas)
        new = TestManager(self.root / 'restored-with-pdfs', self.public)
        new.restore(backup, PASSWORD)
        with closing(sqlite3.connect(new.root / 'shared/data/carton_erp.sqlite3')) as db:
            rebound = Path(db.execute('SELECT file_path FROM pdf_order_training_samples').fetchone()[0])
        self.assertEqual(rebound, new.root / 'shared/data/drawing.pdf')
        self.assertEqual(rebound.read_bytes(), pdf.read_bytes())
        self.assertEqual(sha(database), original)
        pdf.unlink()
        previous_backup = self.manager.state['last_backup']
        with self.assertRaisesRegex(ValueError, '附件缺失'):
            self.manager.backup(PASSWORD, self.nas)
        self.assertEqual(self.manager.state['last_backup'], previous_backup)

    def test_update_then_rollback_preserves_new_orders(self):
        old = self.manager.state['current']
        self.manager.update(self.release('two'), PASSWORD, self.nas)
        self.assertEqual(self.manager.state['previous'], old)
        dbpath = self.manager.root / 'shared/data/carton_erp.sqlite3'
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute("INSERT INTO sales_orders VALUES (2, 'after update')")
            db.commit()
        before = sha(dbpath)
        self.manager.rollback(PASSWORD, self.nas)
        self.assertEqual(self.manager.state['current'], old)
        self.assertEqual(sha(dbpath), before)
        self.assertEqual(len(list(self.nas.glob('*.tmbackup'))), 2)

    def test_unavailable_nas_blocks_update(self):
        current = self.manager.state['current']
        with self.assertRaisesRegex(ValueError, 'NAS'):
            self.manager.update(self.release('two'), PASSWORD, self.root / 'missing-share')
        self.assertEqual(self.manager.state['current'], current)

    def test_nas_disk_full_never_creates_local_backup(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch('desktop_assistant.manager.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, 'NAS空间不足'):
                self.manager.backup(PASSWORD, self.nas)
        self.assertFalse(list((self.manager.root / 'backups').iterdir()))
        self.assertFalse(list((self.manager.root / 'staging').iterdir()))
        self.assertFalse(self.manager.state.get('last_backup'))

    def test_corrupt_nas_copy_is_not_published(self):
        from unittest.mock import patch
        import shutil
        original = shutil.copyfileobj
        def corrupt(src, dst, length=0):
            original(src, dst, length)
            if str(getattr(dst, 'name', '')).endswith('.tmbackup.pending'):
                dst.write(b'corrupt')
        with patch('desktop_assistant.manager.shutil.copyfileobj', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'NAS副本校验失败'):
                self.manager.backup(PASSWORD, self.nas)
        self.assertFalse(list(self.nas.glob('*.tmbackup')))
        self.assertFalse(self.manager.state.get('last_backup'))

    def test_incompatible_schema_blocks_update_and_keeps_database(self):
        current = self.manager.state['current']
        db = self.manager.root / 'shared/data/carton_erp.sqlite3'
        before = sha(db)
        with self.assertRaisesRegex(ValueError, '迁移'):
            self.manager.update(self.release('future', 'r2'), PASSWORD, self.nas)
        self.assertEqual(sha(db), before)
        self.assertEqual(self.manager.state['current'], current)

    def test_failed_start_returns_to_old_program(self):
        current = self.manager.state['current']
        with self.assertRaisesRegex(ValueError, '已切回原程序'):
            self.manager.update(self.release('bad-start'), PASSWORD, self.nas)
        self.assertEqual(self.manager.state['current'], current)
        self.assertEqual(self.manager.state['operation'], 'update_failed')

    def test_untrusted_publisher_is_rejected(self):
        another_key = Ed25519PrivateKey.generate()
        other = another_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        with self.assertRaises(Exception):
            extract_verified(self.package, self.root / 'untrusted', other)
        self.assertFalse((self.root / 'untrusted').exists())

    def test_path_escape_and_case_collision_are_rejected(self):
        for index, names in enumerate([['../escape'], ['A.txt', 'a.txt'], ['C:/bad'], ['a:stream'], ['CON.txt']]):
            path = self.root / f'bad{index}.zip'
            with zipfile.ZipFile(path, 'w') as zipped:
                for name in names:
                    zipped.writestr(name, 'x')
            with self.assertRaises(ValueError):
                extract_verified(path, self.root / f'bad-target-{index}')

    def test_operation_lock_rejects_concurrent_backup(self):
        with self.manager.lock():
            with self.assertRaisesRegex(ValueError, '另一个'):
                self.manager.backup(PASSWORD, self.nas)

    @unittest.skipUnless(os.name == "nt", "DPAPI round trip requires a Windows user session")
    def test_windows_secret_storage_round_trip(self):
        token = protect(PASSWORD)
        self.assertNotIn(PASSWORD, token)
        self.assertEqual(unprotect(token), PASSWORD)


if __name__ == '__main__':
    unittest.main()
