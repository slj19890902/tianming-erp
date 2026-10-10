"""Source-OS paths must be interpreted without consulting the old machine."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from desktop_assistant.attachments import rebind_pdf_sources
from desktop_assistant.preflight import inspect
from desktop_assistant.storage import sha


class SourcePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.files = self.root / 'payload'
        self.pdf = self.files / 'data/图纸.pdf'
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b'synthetic private attachment')
        self.db = self.files / 'data/carton_erp.sqlite3'
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE pdf_order_training_samples(id INTEGER PRIMARY KEY, file_path TEXT, file_sha256 TEXT)')
        self.final = self.root / 'restored/shared'

    def tearDown(self):
        self.temp.cleanup()

    def add(self, reference):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('INSERT INTO pdf_order_training_samples(file_path,file_sha256) VALUES (?,?)', (reference, sha(self.pdf)))
            db.commit()

    def references(self):
        with closing(sqlite3.connect(self.db)) as db:
            return [r[0] for r in db.execute('SELECT file_path FROM pdf_order_training_samples ORDER BY id')]

    def test_windows_drive_root_on_mac(self):
        self.add(r'd:\TianmingERP\SHARED\data\图纸.pdf')
        self.assertEqual(rebind_pdf_sources(self.db, Path(r'D:\TianmingERP\shared'), self.files, self.final), 1)
        self.assertEqual(self.references(), [str(self.final / 'data/图纸.pdf')])

    def test_unc_source_and_relative_backslashes(self):
        self.add(r'\\server\archive\shared\data\图纸.pdf')
        self.add(r'data\图纸.pdf')
        self.assertEqual(rebind_pdf_sources(self.db, Path(r'\\server\archive\shared'), self.files, self.final), 2)
        self.assertEqual(self.references(), [str(self.final / 'data/图纸.pdf')] * 2)

    def test_bad_later_reference_never_partially_rewrites_database(self):
        self.add(r'D:\TianmingERP\shared\data\图纸.pdf')
        self.add(r'D:\TianmingERP\outside\图纸.pdf')
        before = sha(self.db)
        with self.assertRaises(ValueError):
            rebind_pdf_sources(self.db, Path(r'D:\TianmingERP\shared'), self.files, self.final)
        self.assertEqual(sha(self.db), before)

    def test_drive_relative_other_drive_and_parent_escape_rejected(self):
        for reference in (r'D:data\图纸.pdf', r'E:\data\图纸.pdf', r'..\outside\图纸.pdf', r'\data\图纸.pdf'):
            with self.subTest(reference=reference):
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute('DELETE FROM pdf_order_training_samples'); db.commit()
                self.add(reference)
                before = sha(self.db)
                with self.assertRaises(ValueError):
                    rebind_pdf_sources(self.db, Path(r'D:\TianmingERP\shared'), self.files, self.final)
                self.assertEqual(sha(self.db), before)

    def test_copied_symlink_cannot_escape_payload(self):
        outside = self.root / 'outside.pdf'
        outside.write_bytes(self.pdf.read_bytes())
        link = self.files / 'data/link.pdf'
        link.symlink_to(outside)
        self.add(r'D:\TianmingERP\shared\data\link.pdf')
        before = sha(self.db)
        with self.assertRaises(ValueError):
            rebind_pdf_sources(self.db, Path(r'D:\TianmingERP\shared'), self.files, self.final)
        self.assertEqual(sha(self.db), before)

    def test_preflight_resolves_old_windows_root_without_writing(self):
        self.add(r'D:\TianmingERP\shared\data\图纸.pdf')
        before = sha(self.db)
        result = inspect(self.db, self.files, managed=True, recorded_root=r'D:\TianmingERP\shared')
        self.assertEqual(result['counts'], {'ok': 1, 'missing': 0, 'external': 0, 'hash_mismatch': 0})
        self.assertFalse(result['ready_for_takeover'])
        self.assertEqual(sha(self.db), before)


if __name__ == '__main__':
    unittest.main()
