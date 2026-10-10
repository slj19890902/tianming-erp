"""Noninteractive signed-runtime entry for a Mac user backup schedule."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description='Mac完整加密备份到期检查')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--public-key', required=True, type=Path)
    parser.add_argument('--publisher-sha256', required=True)
    args = parser.parse_args()
    from desktop_assistant.mac_service import local_root
    from desktop_assistant.release_request import public_identity
    from desktop_assistant.manager import Manager
    from desktop_assistant.nightly import run_once
    from desktop_assistant.storage import write_json
    root = local_root(args.root)
    public = args.public_key.read_bytes()
    if public_identity(public)[1] != args.publisher_sha256:
        raise ValueError('备份任务发布身份不一致')
    os.environ['ERP_HOME_REHEARSAL'] = '1'
    result = run_once(Manager(root, public), resume_when_stopped=False)
    write_json(root/'control/nightly-status.json', {
        **result, 'at':datetime.now(timezone.utc).isoformat(), 'requires_user_login':True})


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('自动备份未确认完成；请检查服务用户钥匙串、NAS连接和维护状态。', file=sys.stderr)
        raise SystemExit(1)
