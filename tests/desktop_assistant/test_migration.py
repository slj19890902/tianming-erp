from contextlib import closing
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from tests.desktop_assistant import test_recovery as recovery
from desktop_assistant.storage import sha, read_json


class MigrationRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.fixture=recovery.RecoveryTests();self.fixture.setUp()
        self.manager=self.fixture.manager
        self.database=self.manager.root/'shared/data/carton_erp.sqlite3'

    def tearDown(self):
        self.fixture.tearDown()

    def migrate(self, release, shared, revision, log, **_runtime):
        with closing(sqlite3.connect(shared/'data/carton_erp.sqlite3')) as db:
            db.execute('ALTER TABLE sales_orders ADD COLUMN new_field TEXT')
            db.execute('CREATE TABLE future_empty_table(id INTEGER)')
            db.execute('UPDATE alembic_version SET version_num=?',(revision,));db.commit()
        log.write_text('fixture migration')

    def test_additive_upgrade_proves_existing_fields_without_changing_live_copy(self):
        before=sha(self.database);state=self.manager.state
        with patch('desktop_assistant.migration.run_migration',side_effect=self.migrate):
            report=self.manager.preview_update(self.fixture.release('two','r2'))
        self.assertEqual(report['status'],'passed')
        self.assertEqual(report['source_revision'],'r1');self.assertEqual(report['target_revision'],'r2')
        self.assertEqual(sha(self.database),before);self.assertEqual(self.manager.state,state)
        self.assertTrue(report['source_unchanged']);self.assertEqual(report['attachments_verified'],2)
        job=Path(report['report_path']).parent
        self.assertTrue((job/'report.json').is_file())
        self.assertTrue((job/'migration.log').is_file())
        self.assertFalse((job/'shared').exists())
        self.assertFalse((job/'release').exists())
        self.assertIn('deleted_bytes',report['temporary_cleanup']['shared'])

    def test_equal_counts_do_not_hide_changed_business_values(self):
        before=sha(self.database)
        def change(release,shared,revision,log, **_runtime):
            self.migrate(release,shared,revision,log)
            with closing(sqlite3.connect(shared/'data/carton_erp.sqlite3')) as db:
                db.execute("UPDATE sales_orders SET note='changed price or quantity'");db.commit()
        with patch('desktop_assistant.migration.run_migration',side_effect=change):
            with self.assertRaisesRegex(ValueError,'改变既有业务事实'):
                self.manager.preview_update(self.fixture.release('two','r2'))
        self.assertEqual(sha(self.database),before)
        report=next((self.manager.root/'staging').glob('migration-*/report.json'))
        self.assertEqual(read_json(report)['status'],'failed')

    def test_attachment_change_and_source_race_both_block(self):
        def change(release,shared,revision,log, **_runtime):
            self.migrate(release,shared,revision,log)
            (shared/'data/drawing.pdf').write_bytes(b'changed')
        with patch('desktop_assistant.migration.run_migration',side_effect=change):
            with self.assertRaisesRegex(ValueError,'改变附件'):
                self.manager.preview_update(self.fixture.release('two','r2'))
        def race(release,shared,revision,log, **_runtime):
            self.migrate(release,shared,revision,log)
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("INSERT INTO sales_orders VALUES(2,'newly arrived')");db.commit()
        with patch('desktop_assistant.migration.run_migration',side_effect=race):
            with self.assertRaisesRegex(ValueError,'原系统数据已变化'):
                self.manager.preview_update(self.fixture.release('three','r3'))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sales_orders').fetchone()[0],2)

    def test_migration_process_never_inherits_formal_environment(self):
        from desktop_assistant.migration import run_migration
        import os
        from types import SimpleNamespace
        shared=self.manager.root/'staging/test-shared';shared.mkdir()
        staged=self.manager.state['current']
        release=self.manager.root/'releases'/staged
        manifest=self.manager.manifest(staged)
        (shared/'data').mkdir()
        (shared/'data/carton_erp.sqlite3').touch()
        with patch('sys.platform', 'win32'), patch.dict(os.environ,{'ERP_DATABASE_PATH':'D:/formal/db','OPENAI_API_KEY':'secret-fixture','ERP_SECRET_KEY':'formal-secret'}):
            with patch('desktop_assistant.migration.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
                run_migration(release,shared,manifest['revision'],shared/'log.txt', release_manifest=manifest)
        env=run.call_args.kwargs['env']
        self.assertEqual(env['ERP_DATABASE_PATH'],str(shared/'data/carton_erp.sqlite3'))
        self.assertNotIn('OPENAI_API_KEY',env)
        self.assertNotEqual(env['ERP_SECRET_KEY'],'formal-secret')


if __name__=='__main__':unittest.main()
