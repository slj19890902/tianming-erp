"""Real process exclusion tests; no ERP database or network is used."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from desktop_assistant.manager import Manager


CHILD = '''
from pathlib import Path
import sys
from desktop_assistant.manager import Manager
try:
    with Manager(Path(sys.argv[1]), b'').lock():
        print('held', flush=True)
        if len(sys.argv) > 2:
            sys.stdin.readline()
except ValueError as error:
    print(str(error), flush=True)
    raise SystemExit(23)
'''


class OperationLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root, b'')
        self.env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}

    def tearDown(self):
        self.temp.cleanup()

    def child(self):
        return subprocess.run([sys.executable, '-c', CHILD, str(self.root)],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_same_process_second_manager_is_rejected(self):
        with self.manager.lock():
            with self.assertRaisesRegex(ValueError, '另一个'):
                with Manager(self.root, b'').lock():
                    self.fail('maintenance exclusion was bypassed')

    def test_other_process_rejected_then_can_acquire(self):
        with self.manager.lock():
            result = self.child()
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertIn('另一个', result.stdout)
        self.assertEqual(self.child().returncode, 0)

    def test_exception_releases_lock_without_deleting_file(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with self.manager.lock():
                raise RuntimeError('synthetic')
        self.assertTrue((self.root / 'control/operation.lock').is_file())
        self.assertEqual(self.child().returncode, 0)

    def test_owner_exit_releases_kernel_lock(self):
        # A separate observer cannot obtain the lock while owner waits on stdin.
        owner = subprocess.Popen([sys.executable, '-c', CHILD, str(self.root), 'wait'],
                                 env=self.env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import threading
            ready = []
            reader = threading.Thread(target=lambda: ready.append(owner.stdout.readline()), daemon=True)
            reader.start()
            reader.join(10)
            self.assertEqual(ready, ['held\n'])
            self.assertEqual(self.child().returncode, 23)
            owner.terminate()
            owner.wait(timeout=10)
            self.assertEqual(self.child().returncode, 0)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
