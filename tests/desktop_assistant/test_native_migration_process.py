"""Execute a real relocated native Python against only synthetic local fixtures."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

from desktop_assistant.migration import run_migration
from desktop_assistant.storage import sha


@unittest.skipUnless(sys.platform == 'darwin', 'Native macOS process validation')
class NativeMigrationProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        cls.release = cls.root / 'release'
        cls.release.mkdir()
        shutil.copytree(sys.base_prefix, cls.release / 'runtime',
                        ignore=shutil.ignore_patterns('site-packages', '__pycache__'))
        repo = Path(__file__).resolve().parents[2]
        for name in ('desktop_assistant/migration_entry.py', 'app/core/home_rehearsal.py'):
            target = cls.release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / name, target)
        module = cls.release / 'alembic'
        module.mkdir()
        (module / '__init__.py').write_text('')
        (module / '__main__.py').write_text('''
import os, sys, socket, sqlite3, json
assert sys.argv[1:] == ['upgrade', 'synthetic-r2']
try:
    with socket.socket() as client:
        client.connect(('192.0.2.1', 443))
except PermissionError:
    blocked = True
else:
    raise AssertionError('migration connected externally')
with sqlite3.connect(os.environ['ERP_DATABASE_PATH']) as db:
    db.execute('ALTER TABLE facts ADD COLUMN extra TEXT')
    assert db.execute('SELECT value FROM facts').fetchone()[0] == 'preserve'
print(json.dumps({'blocked': blocked, 'python': sys.executable,
    'database': os.environ['ERP_DATABASE_PATH'],
    'key_present': 'OPENAI_API_KEY' in os.environ,
    'proxy_present': 'HTTPS_PROXY' in os.environ,
    'mode': os.environ['ERP_HOME_REHEARSAL'],
    'user_site': os.environ['PYTHONNOUSERSITE']}))
''')
        files = {name: sha(cls.release / name) for name in
                 ('runtime/bin/python3.12', 'desktop_assistant/migration_entry.py')}
        cls.manifest = {'runtime_platform': 'macos-arm64', 'revision': 'synthetic-r2', 'files': files}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def shared(self, name):
        shared = self.root / name
        (shared / 'data').mkdir(parents=True)
        database = shared / 'data/carton_erp.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE facts(value TEXT)')
            db.execute("INSERT INTO facts VALUES ('preserve')")
        return shared

    def test_real_child_uses_fixed_database_and_cannot_connect(self):
        shared = self.shared('valid')
        log = self.root / 'valid.log'
        env = {'PYTHONPATH': '/untrusted', 'PYTHONHOME': '/untrusted',
               'ERP_DATABASE_PATH': '/untrusted/formal.sqlite3',
               'OPENAI_API_KEY': 'synthetic', 'HTTPS_PROXY': 'http://127.0.0.1:9',
               'ERP_HOME_REHEARSAL': '0'}
        run_migration(self.release, shared, 'synthetic-r2', log,
                      environment=env, release_manifest=self.manifest)
        report = json.loads(log.read_text())
        self.assertTrue(report['blocked'])
        self.assertEqual(report['database'], str(shared / 'data/carton_erp.sqlite3'))
        self.assertEqual(report['mode'], '1')
        self.assertEqual(report['user_site'], '1')
        self.assertFalse(report['key_present'] or report['proxy_present'])
        self.assertEqual(Path(report['python']), self.release / 'runtime/bin/python3.12')

    def test_wrong_target_and_missing_database_refuse_before_child(self):
        shared = self.shared('wrong-target')
        database = shared / 'data/carton_erp.sqlite3'
        before = sha(database)
        with self.assertRaisesRegex(ValueError, '签名发布包'):
            run_migration(self.release, shared, 'wrong', self.root / 'wrong.log',
                          release_manifest=self.manifest)
        self.assertEqual(sha(database), before)
        with self.assertRaisesRegex(ValueError, '拒绝创建空库'):
            run_migration(self.release, self.root / 'missing', 'synthetic-r2', self.root / 'missing.log',
                          release_manifest=self.manifest)
        self.assertFalse((self.root / 'missing').exists())

    def test_parent_directory_link_cannot_redirect_database(self):
        shared = self.shared('link-source')
        alias = self.root / 'link-alias'
        alias.symlink_to(shared, target_is_directory=True)
        before = sha(shared / 'data/carton_erp.sqlite3')
        with self.assertRaisesRegex(ValueError, '链接'):
            run_migration(self.release, alias, 'synthetic-r2', self.root / 'link.log',
                          release_manifest=self.manifest)
        self.assertEqual(sha(shared / 'data/carton_erp.sqlite3'), before)

    def test_tampered_guard_refused_without_database_change(self):
        shared = self.shared('tampered')
        entry = self.release / 'desktop_assistant/migration_entry.py'
        original = entry.read_bytes()
        before = sha(shared / 'data/carton_erp.sqlite3')
        try:
            entry.write_bytes(original + b'\n# tampered\n')
            with self.assertRaisesRegex(ValueError, '安全入口'):
                run_migration(self.release, shared, 'synthetic-r2', self.root / 'tampered.log',
                              release_manifest=self.manifest)
        finally:
            entry.write_bytes(original)
        self.assertEqual(sha(shared / 'data/carton_erp.sqlite3'), before)


if __name__ == '__main__':
    unittest.main()
