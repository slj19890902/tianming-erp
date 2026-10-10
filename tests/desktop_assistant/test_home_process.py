"""Real kernel checks use only synthetic loopback sockets, no external peers."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from desktop_assistant.home_process import command


NATIVE = r'''
import ctypes, errno, json, socket, struct, sys
lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
lib.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
lib.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
lib.sendto.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                      ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
lib.sendto.restype = ctypes.c_ssize_t
address = ctypes.create_string_buffer(struct.pack('BBH4s8x',
    16, socket.AF_INET, socket.htons(int(sys.argv[1])), socket.inet_aton('127.0.0.1')))
results = {}
for name, kind in [('tcp', socket.SOCK_STREAM), ('udp', socket.SOCK_DGRAM)]:
    descriptor = lib.socket(socket.AF_INET, kind, 0)
    assert descriptor >= 0
    ctypes.set_errno(0)
    result = (lib.connect(descriptor, address, 16) if name == 'tcp' else
              lib.sendto(descriptor, b'synthetic', 9, 0, address, 16))
    results[name] = [result, ctypes.get_errno()]
    lib.close(descriptor)
print(json.dumps(results))
'''


class HomeProcessPolicyTests(unittest.TestCase):
    def test_windows_unchanged_and_mac_missing_policy_fails_closed(self):
        args = ['signed-python', '-m', 'entry']
        with patch('sys.platform', 'win32'):
            self.assertEqual(command(args, {}), args)
        with patch('sys.platform', 'darwin'):
            self.assertEqual(command(args, {'ERP_HOME_REHEARSAL':'0'}), args)
            with self.assertRaises(ValueError):
                command(args, {'ERP_HOME_REHEARSAL':'typo'})
            with patch('desktop_assistant.home_process.SANDBOX', Path('/missing-sandbox')):
                with self.assertRaisesRegex(ValueError, '拒绝降级'):
                    command(args, {'ERP_HOME_REHEARSAL':'1'})

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS kernel policy')
    def test_native_tcp_udp_and_descendants_cannot_egress(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            args = [sys.executable, '-c', NATIVE, str(listener.getsockname()[1])]
            control = subprocess.run(args, capture_output=True, text=True, timeout=10, check=True)
            baseline = json.loads(control.stdout)
            self.assertEqual(baseline['tcp'][0], 0)
            self.assertEqual(baseline['udp'][0], 9)
            # There is deliberately no Python audit hook in either child.
            for child in (args, [sys.executable, '-c',
                    'import subprocess,sys;sys.exit(subprocess.call(sys.argv[1:]))', *args]):
                result = subprocess.run(command(child, {'ERP_HOME_REHEARSAL':'1'}),
                    capture_output=True, text=True, timeout=10, check=True)
                values = json.loads(result.stdout)
                self.assertEqual(values, {'tcp':[-1, 1], 'udp':[-1, 1]})

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS kernel policy')
    def test_local_browser_response_and_process_identity_preserved(self):
        script = '''
import os,socket
with socket.socket() as listener:
    listener.bind(('127.0.0.1',0));listener.listen()
    print(str(os.getpid())+' '+str(listener.getsockname()[1]),flush=True)
    connection,_=listener.accept()
    with connection:
        assert connection.recv(100)==b'synthetic-browser'
        connection.sendall(b'local-response')
'''
        process = subprocess.Popen(command([sys.executable, '-c', script], {}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready = []
            thread = threading.Thread(target=lambda: ready.append(process.stdout.readline()), daemon=True)
            thread.start();thread.join(10)
            self.assertTrue(ready and ready[0].strip(), 'sandbox listener not ready')
            pid, port = map(int, ready[0].split())
            self.assertEqual(pid, process.pid)
            with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
                client.sendall(b'synthetic-browser')
                self.assertEqual(client.recv(100), b'local-response')
            _, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, errors)
        finally:
            if process.poll() is None:
                process.kill()  # This test's synthetic process only, never ERP.
            process.communicate(timeout=5)
