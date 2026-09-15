"""Read-only, bounded ERP reachability checks; never treats a PID as readiness."""
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
import json
import ipaddress
import socket
import subprocess


def browser_url(config):
    lan = (config.get('ERP_LAN_HTTP_ORIGIN') or '').strip().rstrip('/')
    return lan + '/' if lan else config.get('ERP_BROWSER_URL', 'http://127.0.0.1:' + config['ERP_PORT'] + '/')


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('invalid endpoint')
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise ValueError('endpoint redirects to another origin')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    origin(url)
    opener = build_opener(ProxyHandler({}), SameOriginRedirect())
    with opener.open(url, timeout=3) as response:
        return response.status, response.headers.get_content_type(), response.read(16384)


def health(url, request):
    status, _, body = request(url)
    return status == 200 and json.loads(body) == {'ok': True}


def inspect(config, running, request=fetch):
    if not running:
        return '后台已停止', '网页不可用'
    try:
        port = int(config['ERP_PORT'])
        if not 1 <= port <= 65535:
            raise ValueError()
        if not health(f'http://127.0.0.1:{port}/api/health', request):
            raise ValueError()
    except Exception:
        return '进程存在，但后台无响应', '网页状态待核对'
    try:
        page = browser_url(config)
        origin(page)
        if not health(page.rstrip('/') + '/api/health', request):
            raise ValueError()
        status, content_type, body = request(page)
        if status != 200 or content_type != 'text/html' or b'<html' not in body.lower():
            raise ValueError()
    except Exception:
        return '后台正常', '网页入口不可达：请检查访问地址、转发或网络'
    return '后台正常', '网页可访问'


def repair_lan_forward(config):
    """Restore only the configured private origin; refuse unrelated existing rules."""
    import winreg
    scheme, host, port = origin(config.get('ERP_LAN_HTTP_ORIGIN', ''))
    ip = ipaddress.IPv4Address(host)
    target_port = int(config['ERP_PORT'])
    if scheme != 'http' or not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise ValueError('未配置可修复的私网访问地址')
    if config.get('ERP_BIND_HOST') != '127.0.0.1' or not 1 <= target_port <= 65535 or port == target_port:
        raise ValueError('当前不是独立内网转发配置，请检查访问设置')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))  # The configured address must belong to this PC.
    if not health(f'http://127.0.0.1:{target_port}/api/health', fetch):
        raise ValueError('后台未就绪，请先启动ERP')
    name = f'{host}/{port}'
    expected = f'127.0.0.1/{target_port}'
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Services\PortProxy\v4tov4\tcp') as key:
            existing = winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        existing = None
    if existing is not None and existing != expected:
        raise ValueError('该地址已有其他转发，未覆盖；请管理员检查')
    result = subprocess.run(['netsh', 'interface', 'portproxy', 'add', 'v4tov4',
        f'listenaddress={host}', f'listenport={port}', 'connectaddress=127.0.0.1',
        f'connectport={target_port}'], capture_output=True, timeout=10,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('转发未能恢复，请以Windows管理员身份打开助手后重试；未关闭防火墙')
    return '已恢复配置中的ERP私网转发；请等待网页状态检查。'
