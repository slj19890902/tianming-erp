"""Interactive Mac home-rehearsal restore; never starts the ERP service."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from desktop_assistant.credential_transfer import LIMIT, _read, unseal
from desktop_assistant.release_request import public_identity
from desktop_assistant.storage import sha


def _home_directory():
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def validate_target(root):
    if sys.platform != 'darwin':
        raise ValueError('此入口仅用于 Mac 家庭预演恢复')
    root = Path(root).absolute()
    if root.exists() or root.is_symlink():
        raise ValueError('恢复目标必须是尚不存在的新目录')
    if any(p.is_symlink() or p.is_junction() for p in root.parents):
        raise ValueError('恢复目标不能经过目录链接')
    home = _home_directory()
    resolved = root.resolve()
    parent = next(p for p in resolved.parents if p.exists())
    if (not resolved.is_relative_to(home) or parent.stat().st_dev != home.stat().st_dev
            or parent.stat().st_uid != os.getuid()):
        raise ValueError('家庭恢复必须位于当前用户本机主目录的磁盘，不能放NAS或他人目录')
    return resolved


def preflight(args):
    root = validate_target(args.root)
    public = _read(args.public_key, 8192)
    _key, actual = public_identity(public)
    if actual != args.publisher_sha256:
        raise ValueError('发布公钥指纹与已确认正式身份不一致')
    if sha(args.backup) != args.backup_sha256:
        raise ValueError('完整备份SHA256与已确认回执不一致')
    if args.backup_password_from_transfer and args.credentials is None:
        raise ValueError('从移交包取恢复密码必须同时指定凭据包')
    if args.credentials is not None and not args.credentials.is_file():
        raise ValueError('凭据移交包不存在')
    return root, public


def run(args, prompt=getpass.getpass):
    # Prompt only after all non-secret preflight checks, no target is created yet.
    root, public = preflight(args)
    transfer_password = None
    if args.credentials is not None:
        transfer_password = prompt('凭据移交口令（隐藏输入）：')
        record = unseal(_read(args.credentials, LIMIT), transfer_password)
        if record['bound_backup_sha256'] != args.backup_sha256:
            raise ValueError('凭据移交包未绑定本次完整备份')
    if args.backup_password_from_transfer:
        entry = record['entries'].get('backup')
        if entry is None:
            raise ValueError('移交包不含备份密码，请由原用户核对后重试')
        password = entry['value']
    else:
        password = prompt('完整备份恢复口令（隐藏输入）：')
    # The destination is checked again after interactive input.
    root = validate_target(root)
    from app.core.home_rehearsal import install_network_guard
    os.environ['ERP_HOME_REHEARSAL'] = '1'
    install_network_guard()
    from desktop_assistant.manager import Manager
    manager = Manager(root, public)
    result = manager.restore(args.backup, password, credentials=args.credentials,
                             credential_password=transfer_password)
    receipt = {'type': 'tianming.mac-home-restore.v1', 'backup_sha256': args.backup_sha256,
               'publisher_sha256': args.publisher_sha256, 'started': False,
               'factory_cutover': False, 'result': result}
    if args.credentials is not None:
        receipt['credential_package_sha256'] = sha(args.credentials)
    destination = root / 'control/home-restore-receipt.json'
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(receipt, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    return receipt


def parser():
    result = argparse.ArgumentParser(description='核验并恢复到全新Mac本地目录；不启动ERP或接管工厂')
    result.add_argument('--root', type=Path, required=True)
    result.add_argument('--backup', type=Path, required=True)
    result.add_argument('--backup-sha256', required=True)
    result.add_argument('--public-key', type=Path, required=True)
    result.add_argument('--publisher-sha256', required=True)
    result.add_argument('--credentials', type=Path)
    result.add_argument('--backup-password-from-transfer', action='store_true')
    return result


def main():
    args = parser().parse_args()
    if not sys.stdin.isatty():
        raise SystemExit('必须在本机交互终端隐藏输入口令；不接受口令参数、文件或管道')
    try:
        receipt = run(args)
    except (Exception, KeyboardInterrupt):
        # Underlying errors may originate in native credential libraries. Do not
        # print their exception text or locals. Interrupted staging is retained.
        raise SystemExit('恢复未确认完成，未通过此入口启动ERP。请检查目标状态和暂存现场，勿覆盖重试；密码及异常详情未输出。') from None
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
