from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant.manager import Manager
from desktop_assistant.storage import (database_info, decrypt_file, extract_verified,
                                      pack_tree, read_json, sha, write_json)
from desktop_assistant.windows import protect, unprotect

PASSWORD = 'isolated-recovery-test-password'


class TestManager(Manager):
    __test__ = False
    def _process(self):
        return None
    def stop(self):
        pass
    def start(self):
        if self.manifest()['version'] == 'bad-start':
            raise ValueError('synthetic startup failure')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
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
            for name in ('users', 'customers', 'products', 'sales_orders'):
                db.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY, note TEXT)')
                db.execute(f"INSERT INTO {name} VALUES (1, 'synthetic')")
            db.commit()
        (shared / 'data/drawing.pdf').write_bytes(b'synthetic attachment')
        write_json(shared / 'environment.json', {'ERP_PORT': '18080', 'ERP_SECRET_KEY': 'synthetic-only'})
        write_json(self.manager.root / 'state.json', {'current': release['id'], 'previous': None})

    def tearDown(self):
        self.temp.cleanup()

    def release(self, version, revision='r1'):
        source = self.root / ('source-' + version)
        source.mkdir()
        for name in ('runtime/python.exe', 'main.py', 'app/main.py', 'desktop_assistant/server_entry.py'):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(version)
        target = self.root / (version + '.zip')
        pack_tree(source, target, {'type': 'tianming.release.v1', 'version': version, 'revision': revision}, self.key)
        return target

    def test_complete_encrypted_backup_and_new_machine_restore(self):
        database = self.manager.root / 'shared/data/carton_erp.sqlite3'
        original = sha(database)
        backup = self.manager.backup(PASSWORD, self.nas)
        self.assertNotIn(b'synthetic-only', backup.read_bytes())
        new = TestManager(self.root / 'new-computer', self.public)
        result = new.restore(backup, PASSWORD)
        self.assertEqual(result['version'], 'one')
        self.assertFalse(result['started'])
        self.assertEqual(sha(new.root / 'shared/data/carton_erp.sqlite3'), original)
        self.assertEqual((new.root / 'shared/data/drawing.pdf').read_bytes(), b'synthetic attachment')
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

    def test_windows_secret_storage_round_trip(self):
        token = protect(PASSWORD)
        self.assertNotIn(PASSWORD, token)
        self.assertEqual(unprotect(token), PASSWORD)


if __name__ == '__main__':
    unittest.main()
