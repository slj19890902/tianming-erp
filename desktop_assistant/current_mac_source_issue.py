"""Original Windows publisher signs its stopped source and one Mac request.

This independent v3 entry neither compares Windows business facts with Mac nor
reads a historical backup/NAS. No operational credentials, business SQL, stop,
start, credential export, identity creation or target activation occurs here.
The signature approves the precise Mac request; it does not claim that Windows
has inspected today's Mac business data. Physical isolation is operator-attested.
"""
import argparse
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re

from desktop_assistant import current_mac_cutover_contract as contract
from desktop_assistant import current_mac_cutover as codec
from desktop_assistant import factory_source_audit as original
from desktop_assistant.cleanup import local_path
from desktop_assistant.migration import files
from desktop_assistant.operation_lock import operation_lock
from desktop_assistant.release_request import inspect_package, public_identity
from desktop_assistant.retained_source_issue import _read, _unoccupied
from desktop_assistant.signing import load_dpapi_key
from desktop_assistant.storage import sha
from desktop_assistant.windows_private_workspace import private_directory, read_file_lease

FAILURE = ('签发未确认；输出可能已包含有效授权，保留全部现场，不覆盖重签。'
           '本入口没有启停或隔离任何系统；秘密及异常详情未输出。')
ERROR = '当次原Windows停写现场或批准输入不一致，未确认签发'
STATE_LIMIT = 16 * 1024**2


def require(value):
    if not value:
        raise ValueError(ERROR)


def _current():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json(raw):
    """State may contain finite floats, but not ambiguous or nonfinite JSON."""
    require(type(raw) is bytes and 0 < len(raw) <= STATE_LIMIT)
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result); result[key] = value
        return result
    def invalid(_): raise ValueError(ERROR)
    def number(value):
        result = float(value); require(math.isfinite(result)); return result
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                           parse_constant=invalid, parse_float=number)
        require(type(value) is dict)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(ERROR) from None


def _strict_stopped(manager):
    # All rechecks repeat the raw JSON/pending guards, including absent sidecars
    # represented by dangling links. The old v1/v2 helpers remain unchanged.
    root = manager.root
    raw = _read(root / 'state.json', STATE_LIMIT); state = _json(raw)
    with os.scandir(root) as entries:
        require(not any(entry.name == 'state.json.pending' or
            re.fullmatch(r'state-[0-9a-f]{32}\.pending\.json', entry.name) for entry in entries))
    require(not state.get('backup_work_retained'))
    for suffix in ('-wal', '-shm', '-journal'):
        require(not os.path.lexists(root / ('shared/data/carton_erp.sqlite3' + suffix)))
    process = local_path(root / 'control/process.json')
    if process.exists():
        value = _json(_read(process, 65536))
        require(set(value) == {'pid', 'created', 'exe', 'nonce'}
                and type(value['pid']) is int and 0 < value['pid'] < 2**31
                and type(value['created']) in (int, float) and math.isfinite(value['created']) and value['created'] > 0
                and type(value['exe']) is str and Path(value['exe']).is_absolute()
                and type(value['nonce']) is str and re.fullmatch('[0-9a-f]{32}', value['nonce']))
    # This also rejects live/paused/identity-mismatched/inaccessible managed
    # processes and other application processes still referencing this root.
    require(original._stopped(manager) == state)
    return raw, state


@contextmanager
def verified_stopped_source(root, public, request):
    """Yield public evidence plus an active-only, immutable-evidence recheck.

    The original lock and Windows read leases span the complete signing scope.
    All data reads are byte hashes, never business SQL or secret decryption.
    """
    original._require_windows()
    request = contract.decode(contract.canonical(contract.validate_request(request)))
    require(public_identity(public)[1] == request['publisher_sha256'])
    root = original._preflight(root)
    started = _current()
    require(contract._time(request['created_at']) <= started < contract._time(request['expires_at']))
    with operation_lock(root / 'control/operation.lock', existing_only=True), ExitStack() as held:
        pinned = {}
        def pin(path):
            path = local_path(path)
            if path not in pinned:
                held.enter_context(read_file_lease(path)); pinned[path] = sha(path)
            return pinned[path]
        database = root / 'shared/data/carton_erp.sqlite3'
        database_sha = pin(database); state_sha = pin(root / 'state.json')
        manager = original._SourceReader(root, public)
        state_raw, state = _strict_stopped(manager)
        require(hashlib.sha256(state_raw).hexdigest() == state_sha)
        process = local_path(root / 'control/process.json')
        process_sha = pin(process) if process.exists() else None
        environment_sha = pin(root / 'shared/environment.json')
        # Only hash existing protected configuration; no credentials.collect,
        # unseal, export, NAS metadata or keychain lookup is permitted.
        for name, _ in original.credentials.CONFIGS.values():
            path = local_path(root / name)
            if path.exists(): pin(path)
        configs = original._config_hashes(root)
        package = root / 'packages' / (state['current'] + '.zip')
        require(pin(package) == state['current'])
        manifest_path = root / 'releases' / state['current'] / 'manifest.json'
        pin(manifest_path)
        _, manifest = inspect_package(package, public)
        require(manifest.get('runtime_platform', 'windows') == 'windows'
                and manifest == _json(_read(manifest_path, STATE_LIMIT)))
        complete_files = files(root / 'shared')
        for name, expected in complete_files.items():
            require(pin(root / 'shared' / name) == expected)
        evidence = None; evidence_bytes = None; active = True

        def recheck():
            require(active)
            current_raw, _ = _strict_stopped(manager)
            require(current_raw == state_raw
                and (sha(process) if process.exists() else None) == process_sha
                and original._config_hashes(root) == configs
                and files(root / 'shared') == complete_files
                and all(sha(path) == expected for path, expected in pinned.items()))
            current = _current()
            minimum = started if evidence is None else contract._time(evidence['verified_at'])
            require(minimum <= current < contract._time(request['expires_at']))
            if evidence is not None:
                require(contract.canonical(evidence) == evidence_bytes)
                return deepcopy(evidence)
            return current

        try:
            verified = recheck()
            evidence = {'type': contract.SOURCE_STOP,
                'source_sha256': contract.digest({'root': str(root), 'state_sha256': state_sha,
                    'publisher_sha256': request['publisher_sha256'], 'source_package_sha256': state['current']}),
                'source_package_sha256': state['current'], 'request_sha256': contract.digest(request),
                'state_sha256': state_sha, 'database_sha256': database_sha,
                # A full private inventory can exceed the public 64KiB codec.
                # Keep every member and use the existing unbounded canonical
                # inventory hash; only the 64-character digest enters v3.
                'files_sha256': original.protocol.digest(complete_files), 'environment_sha256': environment_sha,
                'credential_config_sha256': contract.digest(configs), 'process_record_sha256': process_sha,
                'audit_started_at': contract._stamp(started), 'verified_at': contract._stamp(verified),
                'manual_stop': True, 'process_stopped': True, 'maintenance_lock_held': True,
                'database_read_lease_held': True, 'journals_absent': True}
            contract.validate_source_stop(evidence, request=request)
            evidence_bytes = contract.canonical(evidence)
            yield evidence, recheck
            recheck()
        finally:
            active = False


def _review(request_bytes, preparation_bytes, public, request_sha256, preparation_sha256,
            publisher_sha256, authorize_cutover, confirm_source_isolated):
    original._require_windows()
    require(authorize_cutover is True and confirm_source_isolated is True)
    for raw, expected, limit in ((request_bytes, request_sha256, contract.LIMIT),
            (preparation_bytes, preparation_sha256, contract.LIMIT), (public, publisher_sha256, 8192)):
        require(type(expected) is str and re.fullmatch('[0-9a-f]{64}', expected)
                and type(raw) is bytes and 0 < len(raw) <= limit
                and hashlib.sha256(raw).hexdigest() == expected)
    preparation = contract.validate_preparation(contract.decode(preparation_bytes))
    request = contract.validate_request(contract.decode(request_bytes), preparation=preparation)
    require(public_identity(public)[1] == publisher_sha256 == request['publisher_sha256'])
    require(contract._time(request['created_at']) <= _current() < contract._time(request['expires_at']))
    return request, preparation


def issue(*, root, request_bytes, request_sha256, preparation_bytes, preparation_sha256,
          public, publisher_sha256, identity, output_directory, authorize_cutover=False,
          confirm_source_isolated=False):
    """No caller evidence/key, historical backup, password or NAS input exists."""
    request, preparation = _review(request_bytes, preparation_bytes, public, request_sha256,
        preparation_sha256, publisher_sha256, authorize_cutover, confirm_source_isolated)
    require(all(Path(path).is_absolute() for path in (root, identity, output_directory)))
    root, identity, output_directory = map(local_path, (root, identity, output_directory))
    require(identity.is_file() and output_directory != root and root not in output_directory.parents)
    destination = output_directory / ('cutover-current-mac-' + request['request_id'] + '.json')
    _unoccupied(destination)
    # These leases outlive both source context exit and final output DACL check.
    with ExitStack() as held, private_directory(output_directory) as check_output:
        _unoccupied(destination)
        with verified_stopped_source(root, public, request) as (evidence, recheck):
            fixed = contract.canonical(contract.validate_source_stop(evidence, request=request))
            evidence = contract.decode(fixed)
            def checked():
                actual = recheck()
                contract.validate_source_stop(actual, request=request)
                require(contract.canonical(actual) == fixed)
            # Full unsigned body and fresh source before any DPAPI key access.
            contract.make_body(request, preparation, evidence, now=_current())
            checked(); held.enter_context(read_file_lease(identity))
            checked(); check_output(); _unoccupied(destination)
            key = load_dpapi_key(identity, public)
            try:
                checked()
                raw = codec.sign_authorization(request, evidence, preparation, key, now=_current())
            finally: del key
            def verify(value):
                body = codec.verify_authorization(value, public, request)
                require(contract.canonical(body['source_stop']) == fixed
                        and contract.canonical(body['preparation']) == contract.canonical(preparation))
                codec.require_install_window(body, now=_current())
                return body
            verify(raw); checked(); check_output(); _unoccupied(destination)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            held.enter_context(read_file_lease(destination))
            def readback():
                actual = _read(destination, contract.LIMIT)
                require(actual == raw)
                return verify(actual)
            body = readback(); checked(); check_output(); checked(); readback()
            receipt = {'type': 'tianming.current-mac-source-issue-receipt.v3',
                'request_file_sha256': request_sha256, 'request_sha256': contract.digest(request),
                'preparation_file_sha256': preparation_sha256, 'preparation_sha256': contract.digest(preparation),
                'authorization_sha256': hashlib.sha256(raw).hexdigest(), 'authorization_path': str(destination),
                'source_sha256': evidence['source_sha256'], 'publisher_sha256': publisher_sha256,
                'issued_at': body['issued_at'], 'install_before': body['install_before'],
                'operator_isolation_attested': True, 'physical_isolation_proved': False,
                'mac_business_audited_by_source': False, 'source_started': False, 'target_activated': False}
        check_output(); readback()
    return receipt


def run(args):
    original._require_windows()
    with ExitStack() as inputs:
        for path in (args.public_key, args.request, args.preparation):
            inputs.enter_context(read_file_lease(path))
        return issue(root=args.root, request_bytes=_read(args.request, contract.LIMIT),
            request_sha256=args.request_sha256, preparation_bytes=_read(args.preparation, contract.LIMIT),
            preparation_sha256=args.preparation_sha256, public=_read(args.public_key, 8192),
            publisher_sha256=args.publisher_sha256, identity=args.identity, output_directory=args.output_directory,
            authorize_cutover=args.authorize_cutover, confirm_source_isolated=args.confirm_source_isolated)


def parser():
    class SafeParser(argparse.ArgumentParser):
        def error(self, _): raise SystemExit('签发参数缺失或不支持；不得输入任何密码参数')
    result = SafeParser(description='原Windows用户签发当次停写事实和已批准的当前Mac请求；不启停系统', allow_abbrev=False)
    for name in ('root', 'request', 'preparation', 'public-key', 'identity', 'output-directory'):
        result.add_argument('--' + name, type=Path, required=True)
    for name in ('request-sha256', 'preparation-sha256', 'publisher-sha256'):
        result.add_argument('--' + name, required=True)
    result.add_argument('--authorize-cutover', action='store_true')
    result.add_argument('--confirm-source-isolated', action='store_true')
    return result


def main():
    args = parser().parse_args()
    try: print(json.dumps(run(args), ensure_ascii=False))
    except (Exception, KeyboardInterrupt): raise SystemExit(FAILURE) from None


if __name__ == '__main__':
    main()
