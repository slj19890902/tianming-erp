"""Original-Windows-user cutover issuer; never stops, starts or activates ERP.

This entry requires current operator authorization and isolation attestation.
Those attestations are not proof of physical isolation. Mac activation must
independently enforce its deployment and final acceptance requirements.
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import warnings

from desktop_assistant import factory_cutover as protocol
from desktop_assistant.cleanup import local_path
from desktop_assistant.factory_source_audit import verified_stopped_backup
from desktop_assistant.release_request import public_identity
from desktop_assistant.signing import load_dpapi_key
from desktop_assistant.windows_private_workspace import private_directory, read_file_lease


FAILURE = ('签发未确认完成，保持原端停止与隔离。输出可能已经包含有效切换凭据；'
           '保留现场核对，不覆盖重试或重新启动原端。秘密及异常详情未输出。')


def _windows():
    if sys.platform != 'win32':
        raise ValueError('签发只能由工厂Windows原用户执行')


def _read(path, limit):
    path = local_path(path)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError('公开签发输入大小不符')
    return raw


def _review(request_bytes, public, request_sha256, publisher_sha256,
            authorize_cutover, confirm_source_isolated):
    _windows()
    if authorize_cutover is not True or confirm_source_isolated is not True:
        raise ValueError('必须有工厂当次切换授权及旧端隔离确认，不能由审计自动推导')
    protocol._hash(request_sha256)
    protocol._hash(publisher_sha256)
    if (not isinstance(request_bytes, bytes) or not 0 < len(request_bytes) <= protocol.LIMIT
            or hashlib.sha256(request_bytes).hexdigest() != request_sha256
            or not isinstance(public, bytes) or not 0 < len(public) <= 8192
            or hashlib.sha256(public).hexdigest() != publisher_sha256):
        raise ValueError('实际目标申请或公钥字节与独立批准摘要不一致')
    request = json.loads(request_bytes.decode('utf-8'), object_pairs_hook=protocol._unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非标准JSON数值')))
    request = json.loads(protocol.canonical(protocol.validate_request(request)))
    if public_identity(public)[1] != publisher_sha256 or request['publisher_sha256'] != publisher_sha256:
        raise ValueError('目标申请不是已批准的原发布身份')
    current = protocol._now()
    if not protocol._time(request['created_at']) <= current < protocol._time(request['expires_at']):
        raise ValueError('目标申请未生效或已过期')
    return request


def _verify(raw, public, request, evidence):
    body = protocol.verify_authorization(raw, public, request)
    if protocol.canonical(body['source']) != protocol.canonical(evidence):
        raise ValueError('切换凭据没有绑定刚完成的来源核验')
    protocol.require_install_window(body)
    return body


def issue(*, root, backup, expected_backup_sha256, password, request_bytes,
          request_sha256, target_package, public, publisher_sha256, identity,
          private_parent, output_directory, authorize_cutover=False,
          confirm_source_isolated=False):
    """Authenticate, sign and read back under the same source lock and leases.

    Never accept precomputed evidence or a caller-supplied key. A failure after
    signing/writing may leave a valid ticket; preserve it for investigation.
    """
    request = _review(request_bytes, public, request_sha256, publisher_sha256,
                      authorize_cutover, confirm_source_isolated)
    if any(not Path(path).is_absolute() for path in
           (root, backup, target_package, identity, private_parent, output_directory)):
        raise ValueError('签发输入和输出必须使用明确绝对路径')
    root = local_path(root)
    identity = local_path(identity)
    output_directory = local_path(output_directory)
    if (not identity.is_file() or output_directory == root or root in output_directory.parents):
        raise ValueError('签发身份必须已存在，输出必须在来源安装之外')
    destination = output_directory / ('cutover-' + request['request_id'] + '.json')
    with private_directory(output_directory) as check_output:
        if destination.exists() or destination.is_symlink():
            raise ValueError('切换输出已经存在，不覆盖或重签')
        with verified_stopped_backup(root, backup, expected_backup_sha256, password,
                                     request, target_package, public, private_parent) as evidence:
            with read_file_lease(identity):
                # No identity is loaded before final plaintext ACL validation.
                check_output()
                if destination.exists() or destination.is_symlink():
                    raise ValueError('审计期间输出已出现，不覆盖或重签')
                key = load_dpapi_key(identity, public)
                try:
                    raw = protocol.sign_authorization(request, evidence, key)
                finally:
                    del key
                _verify(raw, public, request, evidence)
                check_output()
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                     getattr(os, 'O_BINARY', 0), 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                with read_file_lease(destination):
                    actual = _read(destination, protocol.LIMIT)
                    if actual != raw:
                        raise ValueError('切换输出回读字节不一致')
                    body = _verify(actual, public, request, evidence)
                    check_output()
                receipt = {'type': 'tianming.factory-cutover-issue-receipt.v1',
                           'request_sha256': request_sha256,
                           'authorization_sha256': hashlib.sha256(raw).hexdigest(),
                           'authorization_path': str(destination),
                           'backup_sha256': expected_backup_sha256,
                           'publisher_sha256': publisher_sha256,
                           'issued_at': body['issued_at'], 'install_before': body['install_before'],
                           'operator_isolation_attested': True, 'physical_isolation_proved': False,
                           'source_started': False, 'target_activated': False}
    return receipt


def run(args, prompt=getpass.getpass):
    _windows()
    public = _read(args.public_key, 8192)
    request_bytes = _read(args.request, protocol.LIMIT)
    _review(request_bytes, public, args.request_sha256, args.publisher_sha256,
            args.authorize_cutover, args.confirm_source_isolated)
    # A terminal without hidden-input support must fail, never fall back to echo.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        password = prompt('最终完整备份口令（本机隐藏输入）：')
    try:
        return issue(root=args.root, backup=args.backup, expected_backup_sha256=args.backup_sha256,
                     password=password, request_bytes=request_bytes, request_sha256=args.request_sha256,
                     target_package=args.target_package, public=public, publisher_sha256=args.publisher_sha256,
                     identity=args.identity, private_parent=args.private_parent,
                     output_directory=args.output_directory, authorize_cutover=args.authorize_cutover,
                     confirm_source_isolated=args.confirm_source_isolated)
    finally:
        del password


def parser():
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            # Do not echo unrecognized input, which might contain a password.
            raise SystemExit('签发参数缺失或不支持；使用--help核对，不要在参数中填写秘密')

    result = SafeParser(description='原Windows用户认证最终停写备份后签发；不停止/启动/激活ERP')
    for name in ('root', 'backup', 'request', 'target-package', 'public-key', 'identity',
                 'private-parent', 'output-directory'):
        result.add_argument('--' + name, type=Path, required=True)
    for name in ('backup-sha256', 'request-sha256', 'publisher-sha256'):
        result.add_argument('--' + name, required=True)
    result.add_argument('--authorize-cutover', action='store_true', help='仅在取得工厂当次切换授权后指定')
    result.add_argument('--confirm-source-isolated', action='store_true', help='仅在旧端已隔离后指定；仍须现场证明')
    return result


def main():
    args = parser().parse_args()
    if not sys.stdin.isatty():
        raise SystemExit('必须使用原用户本机交互终端隐藏输入；不接受口令参数、环境或管道')
    try:
        receipt = run(args)
    except (Exception, KeyboardInterrupt):
        raise SystemExit(FAILURE) from None
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
