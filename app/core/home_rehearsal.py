"""Application safeguards for a complete, offline home rehearsal.

This hook covers Python sockets only. Managed Mac rehearsal processes also use
desktop_assistant.home_process for kernel egress restrictions; direct Python
invocations do not acquire that protection. Neither is a full filesystem/IPC
sandbox, so native libraries and subprocesses still require review.
"""
import ipaddress
import os
import sys

_installed = False


def enabled():
    value = os.environ.get('ERP_HOME_REHEARSAL', '1' if sys.platform == 'darwin' else '0')
    if value not in ('0', '1'):
        raise ValueError('ERP_HOME_REHEARSAL必须为0或1，拒绝含糊配置')
    return value == '1'


def require_external_access():
    if enabled():
        raise ValueError('家庭预演禁止连接外部服务或读取生产邮箱')


def _loopback(address):
    if not isinstance(address, tuple) or not address:
        return False
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        return False


def _audit(event, args):
    # Once installed the guard cannot be disabled by changing an environment
    # variable. Inbound loopback HTTP is sufficient for local browser rehearsal.
    if event in ('socket.connect', 'socket.sendto', 'socket.sendmsg'):
        raise PermissionError('家庭预演禁止服务进程主动联网')
    if event == 'socket.bind' and not _loopback(args[1]):
        raise PermissionError('家庭预演仅允许监听本机回环地址')
    if event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'):
        host = args[0]
        if host not in ('localhost', '127.0.0.1', '::1'):
            raise PermissionError('家庭预演禁止外部域名解析')


def install_network_guard():
    global _installed
    if _installed:
        return True
    if not enabled():
        return False
    # An inherited localhost proxy must not become an accidental egress path.
    for name in list(os.environ):
        if name.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy'):
            os.environ.pop(name, None)
    sys.addaudithook(_audit)
    _installed = True
    return True
