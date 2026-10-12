"""Original-Windows-user issuer for the explicit retained-snapshot v2 protocol.

No precomputed audit or caller-owned signing key is accepted. This entry never
stops, starts, retries, exports credentials, creates an identity or activates an
ERP. Operator authorization/isolation assertions are not physical proof.
Public request/preparation file hashes authenticate their original bytes;
request.retained_snapshot.preparation_sha256 separately binds canonical JSON.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import warnings

from desktop_assistant import retained_snapshot_contract as contract
from desktop_assistant import retained_cutover as codec
from desktop_assistant.cleanup import local_path
from desktop_assistant.release_request import public_identity
from desktop_assistant.signing import load_dpapi_key
from desktop_assistant.windows_private_workspace import private_directory, read_file_lease


FAILURE = ('签发未确认完成，保持原端停止与隔离。输出可能已经包含有效切换凭据；'
           '保留现场核对，不覆盖重试或重新启动原端。秘密及异常详情未输出。')


def _windows():
    if sys.platform != 'win32':
        raise ValueError('保留副本签发只能由工厂Windows原用户执行')


def _current():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _audit(*args):
    # Keep import available before its independently tested source component;
    # this lazy boundary never substitutes caller-provided audit evidence.
    from desktop_assistant.retained_source_audit import verified_stopped_retained_backup
    return verified_stopped_retained_backup(*args)


def _read(path, limit):
    with local_path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError('公开签发文件大小无效')
    return raw


def _review(request_bytes, preparation_bytes, public, request_sha256,
            preparation_sha256, publisher_sha256, authorize_cutover, confirm_source_isolated):
    _windows()
    if authorize_cutover is not True or confirm_source_isolated is not True:
        raise ValueError('必须明确工厂当次授权与来源隔离确认；它们不是物理隔离证明')
    for raw, expected, limit in ((request_bytes, request_sha256, contract.LIMIT),
            (preparation_bytes, preparation_sha256, contract.LIMIT), (public, publisher_sha256, 8192)):
        contract._hash(expected)
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= limit
                or hashlib.sha256(raw).hexdigest() != expected):
            raise ValueError('实际申请、准备证明或公钥原字节与独立批准摘要不一致')
    preparation = contract.decode(preparation_bytes)
    request = contract.validate_request(contract.decode(request_bytes), preparation=preparation)
    if public_identity(public)[1] != publisher_sha256 or request['publisher_sha256'] != publisher_sha256:
        raise ValueError('目标申请未绑定批准的原发布公钥')
    if not contract._time(request['created_at']) <= _current() < contract._time(request['expires_at']):
        raise ValueError('目标申请尚未生效或已经过期')
    # Isolate nested data from callers and retain the separately checked file hashes.
    return contract.decode(contract.canonical(request)), contract.decode(contract.canonical(preparation))


def _unoccupied(destination):
    if os.path.lexists(destination):
        raise ValueError('切换输出已经存在，保留原文件，不覆盖或重签')


def _checked(recheck, evidence_bytes):
    actual = recheck()
    contract.validate_source(actual)
    if contract.canonical(actual) != evidence_bytes:
        raise ValueError('来源复验没有保留本次固定证据')


def _verify(raw, public, request, preparation, evidence_bytes):
    body = codec.verify_authorization(raw, public, request)
    if (contract.canonical(body['source']) != evidence_bytes
            or contract.canonical(body['preparation']) != contract.canonical(preparation)):
        raise ValueError('签名凭据未绑定刚完成的来源与准备证明')
    codec.require_install_window(body, now=_current())
    return body


def _ready(request, preparation, evidence):
    """Validate the complete unsigned binding before opening the DPAPI identity."""
    current = _current()
    before = min(current + timedelta(hours=8), contract._time(request['expires_at']))
    contract.validate_body({'type': contract.AUTHORIZATION, 'request': request,
        'preparation': preparation, 'source': evidence, 'issued_at': contract._stamp(current),
        'install_before': contract._stamp(before)})


def issue(*, root, backup, credentials_path, transfer_password, request_bytes,
          request_sha256, preparation_bytes, preparation_sha256, target_package,
          public, publisher_sha256, identity, private_parent, output_directory,
          authorize_cutover=False, confirm_source_isolated=False):
    """Audit live stopped source, then load its existing DPAPI key and publish.

    transfer_password is supplied only by the local hidden-input adapter in run;
    it is not the historical backup password. The source audit authenticates
    the external envelope and obtains that password from its backup entry.
    Failure after signing can leave a valid output, which is always retained.
    """
    request, preparation = _review(request_bytes, preparation_bytes, public, request_sha256,
        preparation_sha256, publisher_sha256, authorize_cutover, confirm_source_isolated)
    if not isinstance(transfer_password, str) or not 12 <= len(transfer_password) <= 1024:
        raise ValueError('隐藏移交口令格式无效')
    paths = (root, backup, credentials_path, target_package, identity, private_parent, output_directory)
    if any(not Path(path).is_absolute() for path in paths):
        raise ValueError('签发输入和输出必须为明确绝对路径')
    root, identity, output_directory = map(local_path, (root, identity, output_directory))
    if not identity.is_file() or output_directory == root or root in output_directory.parents:
        raise ValueError('原身份必须存在，输出必须在来源安装之外')
    destination = output_directory / ('cutover-retained-' + request['request_id'] + '.json')
    _unoccupied(destination)
    backup_sha = request['retained_snapshot']['backup_sha256']
    # File leases outlive both the source audit's exit checks and the output
    # directory's final ACL check. No readback-to-final-recheck write gap.
    with ExitStack() as held, private_directory(output_directory) as check_output:
        _unoccupied(destination)
        with _audit(root, backup, backup_sha, credentials_path, transfer_password,
                    request, target_package, public, private_parent) as (evidence, recheck):
            evidence_bytes = contract.canonical(contract.validate_source(evidence))
            evidence = contract.decode(evidence_bytes)
            _ready(request, preparation, evidence)
            _checked(recheck, evidence_bytes)
            held.enter_context(read_file_lease(identity))
            # Yield is permitted only after the private plaintext final DACL
            # succeeds. Recheck the source and output before DPAPI access.
            _checked(recheck, evidence_bytes)
            _unoccupied(destination); check_output()
            key = load_dpapi_key(identity, public)
            try:
                _checked(recheck, evidence_bytes)
                raw = codec.sign_authorization(request, evidence, preparation, key, now=_current())
            finally:
                del key
            _verify(raw, public, request, preparation, evidence_bytes)
            _checked(recheck, evidence_bytes)
            check_output(); _unoccupied(destination)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            held.enter_context(read_file_lease(destination))

            def readback():
                actual = _read(destination, contract.LIMIT)
                if actual != raw:
                    raise ValueError('签发输出回读原字节不一致')
                return _verify(actual, public, request, preparation, evidence_bytes)

            body = readback()
            _checked(recheck, evidence_bytes); check_output()
            _checked(recheck, evidence_bytes); check_output()
            receipt = {'type': 'tianming.retained-source-issue-receipt.v2',
                'request_sha256': request_sha256, 'preparation_file_sha256': preparation_sha256,
                'preparation_sha256': contract.digest(preparation),
                'authorization_sha256': hashlib.sha256(raw).hexdigest(), 'authorization_path': str(destination),
                'backup_sha256': backup_sha, 'credentials_sha256': request['retained_snapshot']['credentials_sha256'],
                'publisher_sha256': publisher_sha256, 'issued_at': body['issued_at'], 'install_before': body['install_before'],
                'operator_isolation_attested': True, 'physical_isolation_proved': False,
                'source_started': False, 'target_activated': False}
            _checked(recheck, evidence_bytes)
            readback()
        check_output()
        readback()
    return receipt


def _console(stream):
    """Require a real attached Windows console handle, not redirected stdin.

    This checks console transport, not operator location or GUI keyboard focus.
    No attempt is made to take focus or replace a terminal owned by the caller.
    """
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.GetConsoleMode.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    kernel.GetConsoleMode.restype = W.BOOL
    kernel.GetFileType.argtypes = [W.HANDLE]; kernel.GetFileType.restype = W.DWORD
    handle = msvcrt.get_osfhandle(stream.fileno()); mode = W.DWORD()
    if kernel.GetFileType(handle) != 2 or not kernel.GetConsoleMode(handle, C.byref(mode)):
        raise ValueError('隐藏输入要求原Windows用户的真实交互控制台')


def _terminal():
    _windows()
    for current, original in ((sys.stdin, sys.__stdin__), (sys.stdout, sys.__stdout__), (sys.stderr, sys.__stderr__)):
        if current is not original or current is None or not current.isatty():
            raise ValueError('必须使用原用户本机交互终端，不接受口令参数、环境或管道')
        _console(current)


def run(args, prompt=getpass.getpass):
    _terminal()
    with ExitStack() as inputs:
        for path in (args.public_key, args.request, args.preparation):
            inputs.enter_context(read_file_lease(path))
        public = _read(args.public_key, 8192)
        request_bytes = _read(args.request, contract.LIMIT)
        preparation_bytes = _read(args.preparation, contract.LIMIT)
        _review(request_bytes, preparation_bytes, public, args.request_sha256, args.preparation_sha256,
                args.publisher_sha256, args.authorize_cutover, args.confirm_source_isolated)
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            transfer_password = prompt('原外部凭据移交口令（本机隐藏输入）：')
        try:
            _terminal()
            return issue(root=args.root, backup=args.backup, credentials_path=args.credentials,
                transfer_password=transfer_password, request_bytes=request_bytes, request_sha256=args.request_sha256,
                preparation_bytes=preparation_bytes, preparation_sha256=args.preparation_sha256,
                target_package=args.target_package, public=public, publisher_sha256=args.publisher_sha256,
                identity=args.identity, private_parent=args.private_parent, output_directory=args.output_directory,
                authorize_cutover=args.authorize_cutover, confirm_source_isolated=args.confirm_source_isolated)
        finally:
            del transfer_password


def parser():
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise SystemExit('签发参数缺失或不支持；使用--help核对，不要在参数中填写秘密')
    result = SafeParser(description='原Windows用户认证保留的完整备份与停写来源后签发v2；不启停或激活ERP')
    for name in ('root', 'backup', 'credentials', 'request', 'preparation', 'target-package',
                 'public-key', 'identity', 'private-parent', 'output-directory'):
        result.add_argument('--' + name, type=Path, required=True)
    for name in ('request-sha256', 'preparation-sha256', 'publisher-sha256'):
        result.add_argument('--' + name, required=True)
    result.add_argument('--authorize-cutover', action='store_true', help='仅在取得工厂当次切换授权后指定')
    result.add_argument('--confirm-source-isolated', action='store_true', help='旧端隔离确认，仍需独立现场证明')
    return result


def main():
    args = parser().parse_args()
    try:
        receipt = run(args)
        print(json.dumps(receipt, ensure_ascii=False))
    except (Exception, KeyboardInterrupt):
        raise SystemExit(FAILURE) from None


if __name__ == '__main__':
    main()
