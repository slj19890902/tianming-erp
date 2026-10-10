"""Foreground user-service supervisor; shares the existing maintenance lock."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import sys
import threading
import time

# -I ignores CWD/PYTHONPATH. Only the signed script's containing release is added.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop_assistant.operation_lock import BUSY, operation_lock
from desktop_assistant.storage import write_json


def tick(manager, cache=None):
    cache = cache if cache is not None else {}
    try:
        with manager.lock():
            state = manager.state
            if not state.get('current'):
                return 'not-restored'
            if state.get('manual_stop'):
                return 'manually-stopped'
            operation = state.get('operation', '')
            if state.get('onboarding_pending') or operation == 'migration_running' or 'failed' in operation:
                return 'maintenance-attention'
            running = manager._process()
            def identity(item):
                return (state['current'], item[1]['pid'], item[1]['created'], item[1]['exe']) if item else None
            current = identity(running)
            if current is None or cache.get('validated') != current:
                manager.start()
                cache['validated'] = identity(manager._process())
            return 'running'
    except ValueError as error:
        if str(error) == BUSY:
            return 'maintenance-busy'
        raise


def stop_owned(manager, timeout=75):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with manager.lock():
                manager.stop()  # Identity/nonce verified, graceful only; no kill.
                return
        except ValueError as error:
            if str(error) != BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(.5)


def supervise(manager, stopping, *, interval=2):
    status = None
    cache = {}
    def record(value):
        nonlocal status
        if value != status:
            write_json(manager.root / 'control/service-status.json', {
                'status':value, 'pid':os.getpid(), 'home_rehearsal':True,
                'updated_at':datetime.now(timezone.utc).isoformat()})
            status = value
    with operation_lock(manager.root / 'control/service-supervisor.lock'):
        try:
            while not stopping.is_set():
                record(tick(manager, cache))
                stopping.wait(interval)
            record('stopping')
            stop_owned(manager)
            record('stopped')
        except Exception:
            record('attention-required')
            raise


def main():
    parser = argparse.ArgumentParser(description='Mac家庭预演用户服务监护，不允许工厂接管')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--public-key', required=True, type=Path)
    parser.add_argument('--publisher-sha256', required=True)
    args = parser.parse_args()
    from desktop_assistant.mac_service import local_root
    from desktop_assistant.release_request import public_identity
    from desktop_assistant.manager import Manager
    root = local_root(args.root)
    public = args.public_key.read_bytes()
    if public_identity(public)[1] != args.publisher_sha256:
        raise ValueError('服务发布公钥指纹不匹配')
    os.environ['ERP_HOME_REHEARSAL'] = '1'
    # The child installs its sticky network guard. The manager must be able to
    # probe its loopback port and must not load external operational credentials.
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    supervise(Manager(root, public), stopping)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Native/keychain errors and process environments are never logged.
        print('ERP用户服务需要检查；未绕过维护或启动门禁。', file=sys.stderr)
        raise SystemExit(1)
