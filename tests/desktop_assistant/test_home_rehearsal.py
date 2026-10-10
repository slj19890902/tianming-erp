"""No real database, credentials, DNS lookup, or remote connection used."""
import os
import subprocess
import sys
import json
import socket
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

from app.core.home_rehearsal import enabled
from app.services.ai.providers import (
    OpenAIInventoryInsightProvider, DeepSeekInventoryInsightProvider, ProviderUnavailable,
)


class HomeRehearsalTests(unittest.TestCase):
    def test_platform_default_and_invalid_mode(self):
        with patch.dict(os.environ, {}, clear=True), patch('sys.platform', 'darwin'):
            self.assertTrue(enabled())
        with patch.dict(os.environ, {}, clear=True), patch('sys.platform', 'win32'):
            self.assertFalse(enabled())
        with patch.dict(os.environ, {'ERP_HOME_REHEARSAL': 'yes'}):
            with self.assertRaises(ValueError):
                enabled()

    def test_both_ai_backends_stop_before_opener(self):
        with patch.dict(os.environ, {'ERP_HOME_REHEARSAL': '1'}):
            for cls in (OpenAIInventoryInsightProvider, DeepSeekInventoryInsightProvider):
                opener = Mock(side_effect=AssertionError('network attempted'))
                provider = cls('sk-' + 'synthetic' * 4, opener=opener)
                with self.assertRaises(ProviderUnavailable) as caught:
                    provider._send({})
                self.assertEqual(caught.exception.code, 'home_rehearsal')
                self.assertFalse(caught.exception.request_attempted)
                opener.assert_not_called()

    def test_explicit_non_rehearsal_keeps_existing_provider_request(self):
        from urllib.error import URLError
        opener = Mock(side_effect=URLError('synthetic transport failure'))
        with patch.dict(os.environ, {'ERP_HOME_REHEARSAL': '0'}):
            provider = OpenAIInventoryInsightProvider('sk-' + 'synthetic' * 4, opener=opener)
            with self.assertRaises(ProviderUnavailable) as caught:
                provider._send({})
        self.assertTrue(caught.exception.request_attempted)
        opener.assert_called_once()

    def test_socket_guard_is_sticky_and_allows_only_local_listener(self):
        script = '''
import os, socket
from app.core.home_rehearsal import install_network_guard
assert install_network_guard()
assert 'HTTPS_PROXY' not in os.environ
os.environ['ERP_HOME_REHEARSAL'] = '0'
assert install_network_guard()
def blocked(fn):
    try:
        fn()
    except PermissionError:
        return
    raise AssertionError('operation not blocked')
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    sock.listen()
with socket.socket() as sock:
    blocked(lambda: sock.bind(('0.0.0.0', 0)))
    blocked(lambda: sock.connect(('192.0.2.1', 443)))
    blocked(lambda: sock.connect(('127.0.0.1', 21081)))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    blocked(lambda: sock.sendto(b'x', ('192.0.2.1', 53)))
blocked(lambda: socket.getaddrinfo('example.invalid', 443))
'''
        env = dict(os.environ, ERP_HOME_REHEARSAL='1', HTTPS_PROXY='http://127.0.0.1:21081')
        result = subprocess.run([sys.executable, '-c', script], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guarded_service_can_answer_local_browser(self):
        script = """
import socket
from app.core.home_rehearsal import install_network_guard
install_network_guard()
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    print(listener.getsockname()[1], flush=True)
    connection, _ = listener.accept()
    with connection:
        assert connection.recv(100) == b'local-only'
        connection.sendall(b'ok')
"""
        process = subprocess.Popen([sys.executable, '-c', script],
            env=dict(os.environ, ERP_HOME_REHEARSAL='1'),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready = []
            reader = threading.Thread(target=lambda: ready.append(process.stdout.readline()), daemon=True)
            reader.start()
            reader.join(10)
            self.assertTrue(ready and ready[0].strip(), 'listener did not become ready')
            with socket.create_connection(('127.0.0.1', int(ready[0])), timeout=5) as client:
                client.sendall(b'local-only')
                self.assertEqual(client.recv(10), b'ok')
            _, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, errors)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_restored_configuration_cannot_activate_mac_or_load_keys(self):
        from desktop_assistant.manager import Manager
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'shared').mkdir()
            saved = {'ERP_HOME_REHEARSAL': '0', 'ERP_BIND_HOST': '0.0.0.0',
                     'ERP_ENVIRONMENT': 'production', 'OPENAI_API_KEY': 'synthetic',
                     'DEEPSEEK_API_KEY': 'synthetic', 'HTTPS_PROXY': 'http://127.0.0.1:9'}
            (root / 'shared/environment.json').write_text(json.dumps(saved))
            with patch('sys.platform', 'darwin'), \
                    patch('desktop_assistant.ai_config.load_openai_api_key') as first, \
                    patch('desktop_assistant.ai_config.load_deepseek_api_key') as second:
                env = Manager(root, b'')._environment(root / 'release')
            first.assert_not_called()
            second.assert_not_called()
            self.assertEqual(env['ERP_HOME_REHEARSAL'], '1')
            self.assertEqual(env['ERP_BIND_HOST'], '127.0.0.1')
            self.assertEqual(env['ERP_ENVIRONMENT'], 'production')
            self.assertEqual(env['ERP_AI_INVENTORY_PROVIDER'], 'disabled')
            self.assertFalse({'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'HTTPS_PROXY'} & env.keys())
            self.assertEqual(json.loads((root / 'shared/environment.json').read_text()), saved)

    def test_manual_and_automatic_inbox_stop_before_database_or_secret(self):
        from app.services.email_intake import sync_inbox, run_automatic_cycle
        sentinel = Mock(side_effect=AssertionError('database/network attempted'))
        with patch.dict(os.environ, {'ERP_HOME_REHEARSAL': '1'}):
            with self.assertRaisesRegex(ValueError, '家庭预演'):
                sync_inbox(sentinel, None, sentinel)
            with self.assertRaisesRegex(ValueError, '家庭预演'):
                run_automatic_cycle(sentinel, sentinel)
        self.assertEqual(sentinel.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
