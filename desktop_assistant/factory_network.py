"""Explicit public production network proposal. Validation performs no IO.

This is a proposal, never an activation, TLS/network reachability proof or an
environment override. Missing fields are rejected instead of using defaults.
"""
from ipaddress import ip_address, ip_network
import re
from urllib.parse import urlsplit

FIELDS = frozenset({
    'ERP_HOME_REHEARSAL', 'ERP_PRODUCTION_TRANSPORT', 'ERP_BIND_HOST', 'ERP_PORT',
    'ERP_ALLOWED_ORIGINS', 'ERP_TRUSTED_HOSTS', 'ERP_TRUSTED_PROXY_IPS',
    'ERP_SESSION_COOKIE_SECURE', 'ERP_BROWSER_URL', 'ERP_HEALTH_URL',
    'ERP_LAN_HTTP_ORIGIN', 'ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS', 'ERP_SHELF_LABEL_ORIGIN', 'ERP_MOBILE_QR_ORIGIN',
})
PRIVATE = tuple(ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def _private(host):
    try: address = ip_address(host)
    except ValueError: return False
    return any(address.version == item.version and address in item for item in PRIVATE)


def _host(host):
    try:
        address = ip_address(host)
        if address.version != 4 or str(address) != host or address.is_unspecified or address.is_multicast:
            raise ValueError('网络地址不是明确主机')
        return host
    except ValueError:
        if (not isinstance(host, str) or len(host) > 253 or not host.isascii() or host != host.lower()
                or '.' not in host or all(x.isdigit() or x == '.' for x in host)
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                       for part in host.split('.'))):
            raise ValueError('网络主机必须是明确IP或规范域名') from None
        return host


def _csv(value, *, empty=False):
    if value == '' and empty: return []
    values = value.split(',')
    if not 1 <= len(values) <= 16 or any(not v or v != v.strip() for v in values) or len(set(values)) != len(values):
        raise ValueError('网络列表必须明确、不重复且无空项')
    return values


def _url(value, scheme, path='', *, private=False, port=None):
    parsed = urlsplit(value)
    host = parsed.hostname
    if not host: raise ValueError('网络URL缺少主机')
    _host(host)
    actual_port = parsed.port
    authority = '['+host+']' if ':' in host else host
    if actual_port is not None: authority += ':'+str(actual_port)
    if (parsed.scheme != scheme or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path != path
            or value != scheme+'://'+authority+path or (actual_port is not None and not 1 <= actual_port <= 65535)
            or (private and not _private(host)) or (port is not None and actual_port != port)):
        raise ValueError('网络URL不是无凭据、明确协议/端口/路径的规范入口')
    return scheme+'://'+authority, host


def validate(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or any(not isinstance(v, str) or len(v) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in v)
                   for v in value.values())):
        raise ValueError('正式网络必须完整列出固定公开字段')
    if value['ERP_HOME_REHEARSAL'] != '0': raise ValueError('正式网络提案须明确模式；提案本身不激活')
    transport = value['ERP_PRODUCTION_TRANSPORT']
    if transport not in {'lan_http', 'https_proxy'}: raise ValueError('正式传输模式无效')
    if not re.fullmatch(r'[1-9][0-9]{0,4}', value['ERP_PORT']): raise ValueError('监听端口无效')
    port = int(value['ERP_PORT'])
    if not 1024 <= port <= 65535: raise ValueError('专用低权限服务须用非特权端口')
    bind = ip_address(value['ERP_BIND_HOST'])
    if str(bind) != value['ERP_BIND_HOST']: raise ValueError('监听地址必须规范')
    proxies = _csv(value['ERP_TRUSTED_PROXY_IPS'], empty=True)
    if transport == 'https_proxy':
        if not bind.is_loopback or value['ERP_SESSION_COOKIE_SECURE'] != 'true' or not proxies:
            raise ValueError('HTTPS代理必须回环监听、明确回环代理并使用安全Cookie')
        for proxy in proxies:
            if str(ip_address(proxy)) != proxy or not ip_address(proxy).is_loopback:
                raise ValueError('只能信任明确回环代理')
    elif (str(bind) != '0.0.0.0' and not _private(str(bind))) or proxies or value['ERP_SESSION_COOKIE_SECURE'] != 'false':
        raise ValueError('私网HTTP须明确私网监听且不得信任代理或使用Secure Cookie')
    https = transport == 'https_proxy'; scheme = 'https' if https else 'http'
    origins = _csv(value['ERP_ALLOWED_ORIGINS'])
    hosts = set()
    for origin in origins:
        _, host = _url(origin, scheme, private=not https, port=None if https else port); hosts.add(host)
    if not https and str(bind) != '0.0.0.0' and str(bind) not in hosts:
        raise ValueError('私网监听地址没有明确访问入口')
    primary = value['ERP_LAN_HTTP_ORIGIN']
    secondary = _csv(value['ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS'], empty=True)
    if len(secondary) > 4 or primary in secondary or (secondary and not primary):
        raise ValueError('附加私网入口须绑定主入口且互不重复')
    private_origins = ([primary] if primary else []) + secondary
    if private_origins and not https: raise ValueError('附加私网入口仅供HTTPS双入口')
    for origin in private_origins:
        _, host = _url(origin, 'http', private=True)
        if urlsplit(origin).port is None: raise ValueError('私网入口须显式端口')
        hosts.add(host)
    all_origins = set(origins + private_origins)
    for key, path in (('ERP_BROWSER_URL','/'), ('ERP_HEALTH_URL','/api/health')):
        origin, host = _url(value[key], scheme, path, private=not https, port=None if https else port)
        if origin not in origins: raise ValueError('浏览器和健康入口必须在已批准来源中')
    label = value['ERP_SHELF_LABEL_ORIGIN']
    if label:
        _url(label, 'http', private=True)
        if urlsplit(label).port is None or label not in all_origins:
            raise ValueError('货位二维码入口必须是明确批准的私网HTTP来源')
    mobile = value['ERP_MOBILE_QR_ORIGIN']
    if mobile and mobile not in all_origins: raise ValueError('手机二维码入口必须是明确批准来源')
    # Empty QR fields preserve the application's explicit fallback to the
    # already approved browser origin, rather than inheriting old machine data.
    trusted = _csv(value['ERP_TRUSTED_HOSTS'])
    for host in trusted: _host(host)
    if not hosts <= set(trusted): raise ValueError('可信主机遗漏访问入口')
    return dict(value)
