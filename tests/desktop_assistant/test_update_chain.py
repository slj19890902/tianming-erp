from contextlib import closing
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from tests.desktop_assistant import test_recovery as recovery
PASSWORD = recovery.PASSWORD
from desktop_assistant.storage import pack_tree,sha,database_info


class UpdateChainTests(unittest.TestCase):
    def setUp(self):
        self.fixture=recovery.RecoveryTests();self.fixture.setUp()
        self.manager=self.fixture.manager
        self.feed=self.fixture.nas/'releases';self.feed.mkdir()
        self.database=self.manager.root/'shared/data/carton_erp.sqlite3'

    def tearDown(self):self.fixture.tearDown()

    def package(self,name,revision,previous=None,origin=None):
        extras=() if previous is None else ('feature_'+revision,)
        source=self.fixture.release(name,revision,extra_tables=extras)
        target=source.with_name(name+'-signed.zip')
        contract=None if previous is None else {'policy':'preserve_existing_facts_v1','from_revision':origin,'rollback_package_sha256':sha(previous)}
        pack_tree(self.fixture.root/('source-'+name),target,{'type':'tianming.release.v1','version':name,'revision':revision,'migration':contract},self.fixture.key)
        published=self.feed/(sha(target)+'.zip');shutil.copyfile(target,published)
        return published

    def chain(self):
        bridge=self.package('bridge','r1')
        second=self.package('second','r2',bridge,'r1')
        latest=self.package('latest','r3',second,'r2')
        return bridge,second,latest

    def migrate(self,release,shared,revision,log,environment=None, **_runtime):
        with closing(sqlite3.connect(shared/'data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE feature_'+revision+'(id INTEGER PRIMARY KEY,value TEXT)')
            db.execute('UPDATE alembic_version SET version_num=?',(revision,));db.commit()
        log.write_text('fixture')

    def test_skipped_releases_use_exact_signed_bridges_and_keep_facts(self):
        bridge,second,latest=self.chain()
        with self.assertRaisesRegex(ValueError,'兼容契约'):
            self.manager.update(latest,PASSWORD,self.fixture.nas)
        with patch('desktop_assistant.migration.run_migration',side_effect=self.migrate) as run:
            self.assertEqual(self.manager.update_from_feed(latest,PASSWORD,self.fixture.nas),'latest')
            self.assertEqual(run.call_count,4)
        self.assertEqual(self.manager.state['current'],sha(latest))
        self.assertEqual(database_info(self.database)['revision'],'r3')
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT * FROM sales_orders').fetchall(),[(1,'synthetic')])
        self.assertEqual(len(list(self.fixture.nas.glob('*.tmbackup'))),3)
        with patch.object(self.manager,'stop') as stop:
            self.assertEqual(self.manager.update_from_feed(latest,PASSWORD,self.fixture.nas),'已经是该版本')
            stop.assert_not_called()

    def test_missing_intermediate_rejects_before_stop(self):
        bridge,second,latest=self.chain();second.unlink()
        with patch.object(self.manager,'stop') as stop:
            with self.assertRaisesRegex(ValueError,'缺少'):
                self.manager.update_from_feed(latest,PASSWORD,self.fixture.nas)
            stop.assert_not_called()
        self.assertEqual(database_info(self.database)['revision'],'r1')

    def test_wrong_hash_rejects_before_stop(self):
        bridge,second,latest=self.chain();second.write_bytes(bridge.read_bytes())
        with patch.object(self.manager,'stop') as stop:
            with self.assertRaisesRegex(ValueError,'哈希'):
                self.manager.update_from_feed(latest,PASSWORD,self.fixture.nas)
            stop.assert_not_called()

    def test_second_step_failure_stops_chain_without_reverting_prior_upgrade(self):
        bridge,second,latest=self.chain()
        def fail(release,shared,revision,log,environment=None, **_runtime):
            if revision=='r3':raise RuntimeError('isolated rehearsal failure')
            return self.migrate(release,shared,revision,log,environment)
        with patch('desktop_assistant.migration.run_migration',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'rehearsal failure'):
                self.manager.update_from_feed(latest,PASSWORD,self.fixture.nas)
        self.assertEqual(self.manager.state['current'],sha(second))
        self.assertEqual(database_info(self.database)['revision'],'r2')
