"""Foreground service supervisor; shares the existing maintenance lock."""
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


def nightly_loop(manager, stopping, *, system_identity, interval=60, retry_interval=300):
    """One system-session timer; failures do not take down the ERP supervisor."""
    from desktop_assistant.nightly import run_once
    from desktop_assistant.mac_system_service import nightly_enabled
    while not stopping.is_set():
        delay = interval
        try:
            result = run_once(manager, admission=nightly_enabled, restart_allowed=lambda: not stopping.is_set())
        except Exception:
            result = {'status': 'failed'}
            delay = retry_interval
        # Never persist native exception messages, credential references or
        # arbitrary reply fields. The normal backup receipt carries its proof.
        try:
            write_json(manager.root/'control/nightly-status.json', {
                'status': result['status'], 'at': datetime.now(timezone.utc).isoformat(),
                'requires_user_login': False, 'system_identity': system_identity,
                'home_rehearsal': True})
        except Exception:
            # A broken status directory must not silently terminate the timer.
            print('系统备份状态无法写入；请检查专用实例，原备份保留。', file=sys.stderr)
            delay = retry_interval
        stopping.wait(delay)


def supervise(manager, stopping, *, interval=2, system_identity=None, keychain_canary_verified=False):
    status = None
    cache = {}
    def record(value):
        nonlocal status
        if value != status:
            write_json(manager.root / 'control/service-status.json', {
                'status':value, 'pid':os.getpid(), 'home_rehearsal':True,
                'updated_at':datetime.now(timezone.utc).isoformat(),
                **({'system_identity': system_identity, 'requires_user_login': False,
                    'keychain_canary_verified': keychain_canary_verified}
                   if system_identity is not None else {})})
            status = value
    with operation_lock(manager.root / 'control/service-supervisor.lock'):
        timer = None
        if system_identity is not None:
            timer = threading.Thread(target=nightly_loop, args=(manager, stopping),
                kwargs={'system_identity':system_identity}, name='tianming-system-backup', daemon=False)
            timer.start()
        try:
            while not stopping.is_set():
                record(tick(manager, cache))
                stopping.wait(interval)
            record('stopping')
            # Do not stop/restart a process underneath an in-flight backup.
            # Hard NFS can wait indefinitely; an OS shutdown timeout is not a
            # guarantee of graceful completion or permission to force-kill it.
            if timer is not None: timer.join()
            stop_owned(manager)
            record('stopped')
        except Exception:
            record('attention-required')
            raise
        finally:
            stopping.set()
            if timer is not None: timer.join()


def main():
    parser = argparse.ArgumentParser(description='Mac家庭预演服务监护，不允许工厂接管')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--public-key', required=True, type=Path)
    parser.add_argument('--publisher-sha256', required=True)
    parser.add_argument('--system-action', choices=('run', 'check', 'stop'))
    parser.add_argument('--supervisor-package')
    parser.add_argument('--challenge')
    args = parser.parse_args()
    from desktop_assistant.mac_service import local_root
    from desktop_assistant.release_request import public_identity
    from desktop_assistant.manager import Manager
    system_identity = None
    if args.system_action:
        from desktop_assistant import mac_system_service as system
        context, public, control = system.runtime_configuration(args, script=__file__, executable=sys.executable)
        root = system.ROOT
        manager = Manager(root, public)
        os.environ['ERP_HOME_REHEARSAL'] = '1'
        if args.system_action in {'check', 'stop'}:
            import json
            print(json.dumps(system.runtime_worker(manager, args, context)))
            return
        system_identity = system.bootstrap._actual()
        if not control['enabled']:
            write_json(root/'control/service-status.json', {'status': 'system-disabled',
                'pid': os.getpid(), 'home_rehearsal': True, 'system_identity': system_identity,
                'requires_user_login': False, 'updated_at': datetime.now(timezone.utc).isoformat()})
            return
    else:
        root = local_root(args.root)
        public = args.public_key.read_bytes()
        if public_identity(public)[1] != args.publisher_sha256:
            raise ValueError('服务发布公钥指纹不匹配')
        manager = Manager(root, public)
    if public_identity(public)[1] != args.publisher_sha256:
        raise ValueError('服务发布公钥指纹不匹配')
    os.environ['ERP_HOME_REHEARSAL'] = '1'
    # The child installs its sticky network guard. The manager must be able to
    # probe its loopback port and must not load external operational credentials.
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    supervise(manager, stopping, system_identity=system_identity,
              keychain_canary_verified=system_identity is not None)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Native/keychain errors and process environments are never logged.
        print('ERP服务需要检查；未绕过维护或启动门禁。', file=sys.stderr)
        raise SystemExit(1)
