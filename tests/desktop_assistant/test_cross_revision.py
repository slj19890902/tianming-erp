from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from tests.desktop_assistant import test_recovery as recovery
from desktop_assistant.storage import pack_tree,sha
from desktop_assistant.manager import Manager


class CrossRevisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=recovery.RecoveryTests();self.fixture.setUp()
        self.manager=self.fixture.manager
        self.database=self.manager.root/'shared/data/carton_erp.sqlite3'

    def tearDown(self):self.fixture.tearDown()

    def package(self,name='next',old=None):
        package=self.fixture.release(name,'r2').with_name(name+'-signed.zip')
        pack_tree(self.fixture.root/('source-'+name),package,{'type':'tianming.release.v1',
            'version':name,'revision':'r2','migration':{'policy':'preserve_existing_facts_v1',
            'from_revision':'r1','rollback_package_sha256':old or self.manager.state['current']}},self.fixture.key)
        return package

    def migrate(self,release,shared,revision,log,environment=None):
        with closing(sqlite3.connect(shared/'data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE new_feature(id INTEGER PRIMARY KEY, value TEXT)')
            db.execute('UPDATE alembic_version SET version_num=?',(revision,));db.commit()
        log.write_text('fixture migration')

    def test_upgrade_rollback_and_restore_keep_new_business_facts(self):
        old=self.manager.state['current'];package=self.package()
        with patch('desktop_assistant.migration.run_migration',side_effect=self.migrate) as run:
            self.manager.update(package,recovery.PASSWORD,self.fixture.nas)
            self.assertEqual(run.call_count,2) # isolated rehearsal and then managed fixture
        self.assertTrue(self.manager.compatible(old,'r2'))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO sales_orders VALUES(2,'after upgrade')")
            db.execute("INSERT INTO new_feature VALUES(1,'new version fact')");db.commit()
        before=sha(self.database)
        self.manager.rollback(recovery.PASSWORD,self.fixture.nas)
        self.assertEqual(self.manager.state['current'],old);self.assertEqual(sha(self.database),before)
        backup=self.manager.backup(recovery.PASSWORD,self.fixture.nas)
        restored=recovery.TestManager(self.fixture.root/'restored-cross-revision',self.fixture.public)
        restored.restore(backup,recovery.PASSWORD)
        self.assertTrue(restored.compatible(restored.state['current'],'r2'))
        with closing(sqlite3.connect(restored.root/'shared/data/carton_erp.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sales_orders').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT value FROM new_feature').fetchone()[0],'new version fact')
            self.assertEqual(db.execute('SELECT version_num FROM alembic_version').fetchone()[0],'r2')

    def test_registered_invoice_attachment_is_required_for_complete_backup(self):
        folder=self.manager.root/'shared/data/invoice_attachments';folder.mkdir()
        invoice=folder/'invoice.pdf';invoice.write_bytes(b'%PDF-fixture')
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE finance_invoice_attachments(id INTEGER, stored_name TEXT, content_hash TEXT)')
            db.execute('INSERT INTO finance_invoice_attachments VALUES(1,?,?)',('invoice.pdf',sha(invoice).upper()));db.commit()
        backup=self.manager.backup(recovery.PASSWORD,self.fixture.nas)
        restored=recovery.TestManager(self.fixture.root/'restored-invoice',self.fixture.public)
        restored.restore(backup,recovery.PASSWORD)
        self.assertEqual((restored.root/'shared/data/invoice_attachments/invoice.pdf').read_bytes(),invoice.read_bytes())
        invoice.unlink()
        with self.assertRaisesRegex(ValueError,'附件缺失'):
            self.manager.backup(recovery.PASSWORD,self.fixture.nas)

    def test_signed_contract_must_bind_exact_previous_package(self):
        with self.assertRaisesRegex(ValueError,'兼容契约'):
            self.manager.update(self.package(old='a'*64),recovery.PASSWORD,self.fixture.nas)
        self.assertEqual(len(list(self.fixture.nas.glob('*.tmbackup'))),0)

    def test_failed_new_start_uses_compatible_old_program_without_downgrade(self):
        old=self.manager.state['current']
        with patch('desktop_assistant.migration.run_migration',side_effect=self.migrate):
            with self.assertRaisesRegex(ValueError,'已切回原程序'):
                self.manager.update(self.package('bad-start'),recovery.PASSWORD,self.fixture.nas)
        self.assertEqual(self.manager.state['current'],old)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT version_num FROM alembic_version').fetchone()[0],'r2')

    def test_partial_actual_migration_never_starts_unproven_database(self):
        count=0
        def fail(release,shared,revision,log,environment=None):
            nonlocal count
            count+=1
            self.migrate(release,shared,revision,log,environment)
            if count==2:raise RuntimeError('actual migration interrupted')
        with patch('desktop_assistant.migration.run_migration',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'服务保持停止'):
                self.manager.update(self.package(),recovery.PASSWORD,self.fixture.nas)
        self.assertEqual(self.manager.state['operation'],'migration_failed')
        self.assertTrue(list(self.fixture.nas.glob('*.tmbackup')))
        with self.assertRaisesRegex(ValueError,'不能直接启动'):Manager.start(self.manager)
        before=sha(self.database)
        self.manager.recover_interrupted_update()
        self.assertEqual(self.manager.state['operation'],'migration_recovered')
        self.assertEqual(sha(self.database),before)

    def interrupted(self):
        count=0
        def stop_after(release,shared,revision,log,environment=None):
            nonlocal count
            count+=1
            self.migrate(release,shared,revision,log,environment)
            if count==2:raise RuntimeError('interrupted after commit')
        with patch('desktop_assistant.migration.run_migration',side_effect=stop_after):
            with self.assertRaises(ValueError):
                self.manager.update(self.package(),recovery.PASSWORD,self.fixture.nas)

    def test_recovery_rejects_changed_business_facts(self):
        self.interrupted()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE sales_orders SET note='changed'");db.commit()
        before=sha(self.database)
        with self.assertRaisesRegex(ValueError,'完整升级结果'):
            self.manager.recover_interrupted_update()
        self.assertEqual(sha(self.database),before)
        self.assertEqual(self.manager.state['operation'],'migration_failed')

    def test_recovery_rejects_changed_report(self):
        self.interrupted()
        from pathlib import Path
        Path(self.manager.state['migration_report']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'校验证据'):
            self.manager.recover_interrupted_update()

    def test_recovery_rejects_incomplete_schema_with_same_rows_and_head(self):
        self.interrupted()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('ALTER TABLE new_feature ADD COLUMN unexpected TEXT');db.commit()
        with self.assertRaisesRegex(ValueError,'完整升级结果'):
            self.manager.recover_interrupted_update()

    def test_recovery_rejects_changed_attachment(self):
        self.interrupted()
        (self.manager.root/'shared/unexpected.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'完整升级结果'):
            self.manager.recover_interrupted_update()

    def test_recovery_rejects_live_migration_runtime(self):
        self.interrupted()
        from types import SimpleNamespace
        exe=self.manager.root/'releases'/self.manager.state['migration_target']/'runtime/python.exe'
        with patch('psutil.process_iter',return_value=[SimpleNamespace(info={'exe':str(exe)})]):
            with self.assertRaisesRegex(ValueError,'仍有进程'):
                self.manager.recover_interrupted_update()


if __name__=='__main__':unittest.main()
