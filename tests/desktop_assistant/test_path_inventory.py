from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from desktop_assistant.path_inventory import inventory
from desktop_assistant.storage import sha


class PathInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'offline.sqlite3'
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE arbitrary(id INTEGER PRIMARY KEY, note TEXT, payload TEXT, file_path TEXT, opaque BLOB)')
            db.execute('INSERT INTO arbitrary VALUES(?,?,?,?,?)', (7, 'historical D:\\original\\file.pdf remains evidence',
                       json.dumps({'deep': [{'stored_name': 'invoice.pdf'}, {'value': r'Z:\NAS\drawing.pdf'}, {'value': r'\\nas\share\seal.png'}]}),
                       '/private/local/map.json', b'opaque synthetic bytes'))
            db.execute('CREATE TABLE empty_table(value TEXT)')
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_discovers_unknown_tables_and_nested_json_without_writes(self):
        before = sha(self.database)
        result = inventory(self.database)
        self.assertEqual(sha(self.database), before)
        self.assertEqual(result['tables_scanned'], {'arbitrary': 1, 'empty_table': 0})
        self.assertEqual(result['binary_values_not_interpreted'], 1)
        self.assertEqual(len(result['candidates']), 5)
        kinds = {r['kind'] for r in result['candidates']}
        self.assertIn('windows_absolute', kinds)
        self.assertIn('windows_unc', kinds)
        self.assertIn('embedded_windows_path_requires_review', kinds)
        nested = next(r for r in result['candidates'] if r['kind'] == 'windows_absolute')
        self.assertEqual(nested['json_pointer'], '/deep/1/value')
        self.assertEqual(nested['primary_key'], {'id': 7})
        self.assertTrue(all(r['automatic_rewrite_allowed'] is False for r in result['candidates']))
        self.assertFalse(result['ready_for_takeover'])

    def test_wal_source_is_rejected_not_silently_ignored(self):
        Path(str(self.database) + '-wal').write_bytes(b'synthetic pending WAL')
        with self.assertRaisesRegex(ValueError, 'WAL'):
            inventory(self.database)

    def test_missing_database_is_not_created(self):
        absent = self.database.parent / 'missing.sqlite3'
        with self.assertRaises(FileNotFoundError):
            inventory(absent)
        self.assertFalse(absent.exists())

    def test_cli_writes_private_exclusive_report_and_only_prints_counts(self):
        output = self.database.parent / 'report.json'
        command = [sys.executable, '-m', 'desktop_assistant.path_inventory',
                   '--database', str(self.database), '--output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('drawing.pdf', result.stdout)
        self.assertEqual(json.loads(result.stdout)['path_candidates'], 5)
        if sys.platform != 'win32':
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        before = sha(output)
        repeated = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(sha(output), before)


if __name__ == '__main__':
    unittest.main()
