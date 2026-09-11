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


if __name__=='__main__':unittest.main()
