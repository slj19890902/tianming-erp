"""Additional macOS kernel egress restriction for home rehearsal children.

This is a current-host safeguard, not a supported cross-version sandbox API or
a full filesystem/IPC sandbox. Python's sticky guard remains required as well.
"""
import os
from pathlib import Path
import sys

SANDBOX = Path('/usr/bin/sandbox-exec')
PROFILE = '(version 1)(allow default)(deny network-outbound)'


def command(arguments, environment):
    arguments = list(arguments)
    if sys.platform != 'darwin':
        return arguments
    mode = environment.get('ERP_HOME_REHEARSAL', '1')
    if mode not in ('0', '1'):
        raise ValueError('家庭预演模式无效，拒绝启动')
    if mode == '0':
        return arguments
    if (not SANDBOX.is_file() or SANDBOX.is_symlink()
            or SANDBOX.stat().st_uid != 0 or not os.access(SANDBOX, os.X_OK)):
        raise ValueError('Mac家庭预演进程隔离不可用，拒绝降级启动')
    # No shell, profile file, credentials, endpoint or user-controlled policy.
    # sandbox-exec applies the policy before exec; children inherit it.
    return [str(SANDBOX), '-p', PROFILE, *arguments]
