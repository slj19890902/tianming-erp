from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psutil
from desktop_assistant.cleanup import assert_idle, is_application_process


class CleanupProcessIdentityTests(unittest.TestCase):
    def test_names_distinguish_enterprise_vpn_from_application(self):
        self.assertFalse(is_application_process('pgyvpn-enterpris'))
        self.assertFalse(is_application_process('enterprise-service'))
        for name in ('python3.12', 'pythonw.exe', 'TianmingERP', 'ERP.exe',
                     'erp-worker', '天明助手', 'local-erp-service'):
            self.assertTrue(is_application_process(name), name)

    def test_inaccessible_actual_application_still_blocks(self):
        for name in ('Python', 'TianmingERP', 'erp-worker'):
            process = Mock(pid=999999, info={'name': name})
            process.exe.side_effect = psutil.AccessDenied(999999)
            with patch('desktop_assistant.cleanup.psutil.process_iter', return_value=[process]):
                with self.assertRaisesRegex(ValueError, '无法确认'):
                    assert_idle(Path('/synthetic-owned-directory'))

    def test_unrelated_vpn_does_not_block_but_any_actual_path_reference_does(self):
        process = Mock(pid=999999, info={'name': 'pgyvpn-enterpris'})
        process.exe.side_effect = psutil.AccessDenied(999999)
        with patch('desktop_assistant.cleanup.psutil.process_iter', return_value=[process]):
            assert_idle(Path('/synthetic-owned-directory'))
        process.exe.side_effect = None
        process.exe.return_value = '/unrelated/program'
        process.cwd.return_value = '/synthetic-owned-directory/nested'
        process.cmdline.return_value = []
        with patch('desktop_assistant.cleanup.psutil.process_iter', return_value=[process]):
            with self.assertRaisesRegex(ValueError, '仍被运行进程引用'):
                assert_idle(Path('/synthetic-owned-directory'))

    def test_verified_cipher_allows_visible_unrelated_privileged_parent(self):
        with self.subTest('hidden parent fields do not poison immutable-file retention'):
            from tempfile import TemporaryDirectory
            with TemporaryDirectory() as folder:
                target = Path(folder) / 'verified.tmbackup'
                target.write_bytes(b'verified cipher')
                process = Mock(pid=999999, info={'name': 'Python'})
                process.exe.return_value = '/usr/bin/python3'
                process.cwd.side_effect = psutil.AccessDenied(999999)
                process.cmdline.side_effect = psutil.AccessDenied(999999)
                process.environ.side_effect = psutil.AccessDenied(999999)
                with patch('desktop_assistant.cleanup.psutil.process_iter', return_value=[process]):
                    assert_idle(target, allow_unrelated_inaccessible_file=True)

    def test_verified_cipher_still_blocks_visible_reference(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            target = Path(folder) / 'verified.tmbackup'
            target.write_bytes(b'verified cipher')
            process = Mock(pid=999999, info={'name': 'Python'})
            process.exe.return_value = '/usr/bin/python3'
            process.cwd.return_value = '/'
            process.cmdline.return_value = ['/usr/bin/python3', str(target)]
            process.environ.side_effect = psutil.AccessDenied(999999)
            with patch('desktop_assistant.cleanup.psutil.process_iter', return_value=[process]):
                with self.assertRaisesRegex(ValueError, '运行进程'):
                    assert_idle(target, allow_unrelated_inaccessible_file=True)

    def test_verified_cipher_still_blocks_wholly_opaque_application(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            target = Path(folder) / 'verified.tmbackup'
            target.write_bytes(b'verified cipher')
            process = Mock(pid=999999, info={'name': 'Python'})
            process.exe.side_effect = psutil.AccessDenied(999999)
            process.cwd.side_effect = psutil.AccessDenied(999999)
            process.cmdline.side_effect = psutil.AccessDenied(999999)
            process.environ.side_effect = psutil.AccessDenied(999999)
            with patch('desktop_assistant.cleanup.psutil.process_iter', return_value=[process]):
                with self.assertRaisesRegex(ValueError, '无法确认'):
                    assert_idle(target, allow_unrelated_inaccessible_file=True)


if __name__ == '__main__':
    unittest.main()
