from __future__ import annotations

import os
import secrets
from ipaddress import ip_address, ip_network
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "carton_erp.sqlite3"
DEFAULT_BACKUP_DIR = Path(r"Z:\sata1-18015598002\BoxERP\backups")
DEFAULT_SECRET_FILE = PROJECT_ROOT / "data" / "session_secret.key"
DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:8000",
    "http://localhost:8000",
)
PRIVATE_LAN_ORIGIN_REGEX = (
    r"^https?://(?:localhost|127\.0\.0\.1|"
    r"10(?:\.\d{1,3}){3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
    r"(?::\d{1,5})?$"
)
PRIVATE_LAN_NETWORKS = tuple(
    ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    )
)


def load_project_env(env_file: Path = ENV_FILE) -> None:
    """Load simple KEY=VALUE settings without depending on the launch directory."""
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_project_env()


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    backup_dir: Path
    allowed_origins: tuple[str, ...]
    allowed_origin_regex: str | None
    trusted_hosts: tuple[str, ...]
    trusted_proxy_ips: tuple[str, ...]
    secret_key: str
    bind_host: str
    port: int
    workers: int
    health_url: str
    browser_url: str
    environment: str
    production_transport: str
    sqlite_busy_timeout_ms: int = 5_000
    session_cookie_name: str = "erp_session"
    session_expire_minutes: int = 480
    session_cookie_secure: bool = False
    lan_http_origin: str = ""
    additional_private_http_origins: tuple[str, ...] = ()

    @property
    def private_http_origins(self) -> tuple[str, ...]:
        return ((self.lan_http_origin,) if self.lan_http_origin else ()) + self.additional_private_http_origins

    def cookie_secure_for(self, scope) -> bool:
        return self.session_cookie_secure and not is_lan_http_scope(
            scope, self.private_http_origins
        )

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.database_path.as_posix()}"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def uses_https_proxy(self) -> bool:
        return self.is_production and self.production_transport == "https_proxy"


def normalize_path(value: str | Path) -> Path:
    """Normalize a path without requiring a mapped NAS drive to be online."""
    path = Path(value).expanduser()
    try:
        return path.resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(path))


def _resolved_path(env_name: str, default: Path) -> Path:
    configured = Path(os.getenv(env_name, str(default))).expanduser()
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return normalize_path(configured)


def _load_or_create_secret(secret_file: Path, *, allow_create: bool = True) -> str:
    if secret_file.exists():
        secret = secret_file.read_text(encoding="utf-8").strip()
        if secret:
            return secret

    if not allow_create:
        raise RuntimeError(
            "生产环境缺少会话密钥：请配置 ERP_SECRET_KEY，或预先创建 ERP_SECRET_KEY_FILE"
        )
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    secret_file.write_text(secret, encoding="utf-8")
    return secret


def _allowed_origins() -> tuple[str, ...]:
    raw = os.getenv("ERP_ALLOWED_ORIGINS", "")
    if not raw.strip():
        return DEFAULT_ALLOWED_ORIGINS
    origins = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
    for origin in origins:
        parsed = urlsplit(origin)
        host = parsed.hostname
        if (
            origin == "*"
            or parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("ERP_ALLOWED_ORIGINS 仅允许局域网 HTTP/HTTPS 来源")
        if host == "localhost":
            continue
        try:
            address = ip_address(host)
        except ValueError as error:
            raise ValueError(
                "ERP_ALLOWED_ORIGINS 只能使用 localhost 或局域网 IP"
            ) from error
        if not (address.is_private or address.is_loopback):
            raise ValueError("ERP_ALLOWED_ORIGINS 禁止公网 IP")
    return origins or DEFAULT_ALLOWED_ORIGINS


def _is_private_lan_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(
        address.version == network.version and address in network
        for network in PRIVATE_LAN_NETWORKS
    )


def _production_transport(*, production: bool) -> str:
    if not production:
        return "development"
    value = os.getenv("ERP_PRODUCTION_TRANSPORT", "https_proxy").strip().lower()
    if value not in {"https_proxy", "lan_http"}:
        raise ValueError(
            "ERP_PRODUCTION_TRANSPORT must be one of: https_proxy, lan_http"
        )
    return value


def _lan_http_origin(*, production: bool, transport: str) -> str:
    if not production:
        return ""
    value = os.getenv("ERP_LAN_HTTP_ORIGIN", "").strip().rstrip("/")
    if not value:
        return ""
    if transport != "https_proxy":
        raise ValueError("ERP_LAN_HTTP_ORIGIN 仅用于生产 https_proxy 双入口")
    parsed = urlsplit(value)
    try:
        address = ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as error:
        raise ValueError("ERP_LAN_HTTP_ORIGIN 必须是明确的私网 HTTP IP 和端口") from error
    if (
        parsed.scheme != "http"
        or not any(address in network for network in PRIVATE_LAN_NETWORKS)
        or not port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ERP_LAN_HTTP_ORIGIN 必须是明确的私网 HTTP IP 和端口")
    return value


def _additional_private_http_origins(
    *, production: bool, transport: str, lan_http_origin: str
) -> tuple[str, ...]:
    raw = os.getenv("ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS", "")
    values = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
    if not values:
        return ()
    if not production or transport != "https_proxy" or not lan_http_origin:
        raise ValueError("ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS 仅用于已配置双入口的正式 HTTPS 代理")
    if len(values) > 4 or len(set(values)) != len(values) or lan_http_origin in values:
        raise ValueError("ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS 必须是互不重复的明确私网入口")
    for value in values:
        parsed = urlsplit(value)
        try:
            address = ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError as error:
            raise ValueError("ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS 必须使用明确私网 IP 和端口") from error
        if (
            parsed.scheme != "http"
            or not any(address in network for network in PRIVATE_LAN_NETWORKS)
            or not port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS 必须使用明确私网 IP 和端口")
    return values


def is_lan_http_scope(scope, origin: str | tuple[str, ...]) -> bool:
    """Only an explicit private origin and a private client may use LAN HTTP."""
    origins = (origin,) if isinstance(origin, str) else origin
    if not origins or scope.get("type") != "http" or scope.get("scheme") != "http":
        return False
    client = scope.get("client")
    if not client or not _is_private_lan_host(client[0]):
        return False
    try:
        ip_address(client[0])
    except ValueError:
        return False
    host = dict(scope.get("headers", ())).get(b"host", b"").decode("latin-1")
    return f"http://{host}" in origins


def _production_allowed_origins(
    *,
    transport: str,
    port: int,
) -> tuple[str, ...]:
    """Allow only explicitly configured origins in production."""
    raw = os.getenv("ERP_ALLOWED_ORIGINS", "")
    if not raw.strip():
        raise ValueError("生产环境必须显式配置 ERP_ALLOWED_ORIGINS 来源")
    origins = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
    expected_scheme = "https" if transport == "https_proxy" else "http"
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            origin == "*"
            or parsed.scheme != expected_scheme
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "ERP_ALLOWED_ORIGINS 协议不匹配：https_proxy 只允许 HTTPS，"
                "lan_http 只允许 HTTP"
            )
        if transport == "lan_http":
            if not _is_private_lan_host(parsed.hostname):
                raise ValueError("lan_http 的 ERP_ALLOWED_ORIGINS 只允许私网 IP 或 localhost")
            try:
                origin_port = parsed.port
            except ValueError as error:
                raise ValueError("ERP_ALLOWED_ORIGINS 端口无效") from error
            if origin_port != port:
                raise ValueError("lan_http 的 ERP_ALLOWED_ORIGINS 必须显式使用 ERP_PORT")
    return origins


def _trusted_hosts(*, production: bool) -> tuple[str, ...]:
    if not production:
        return ()
    raw = os.getenv("ERP_TRUSTED_HOSTS", "")
    hosts = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not hosts or any("*" in host for host in hosts):
        raise ValueError("ERP_TRUSTED_HOSTS must contain explicit hosts")
    return hosts


def _trusted_proxy_ips(*, production: bool, transport: str) -> tuple[str, ...]:
    raw = os.getenv("ERP_TRUSTED_PROXY_IPS", "")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if production and transport == "https_proxy" and not values:
        raise ValueError("生产环境必须显式配置 ERP_TRUSTED_PROXY_IPS")
    if production and transport == "lan_http" and values:
        raise ValueError("lan_http 直连模式禁止配置 ERP_TRUSTED_PROXY_IPS")
    for value in values:
        if value == "*":
            raise ValueError("ERP_TRUSTED_PROXY_IPS must contain explicit IPs")
        try:
            address = ip_address(value)
        except ValueError as error:
            raise ValueError("ERP_TRUSTED_PROXY_IPS only accepts IP addresses") from error
        if production and transport == "https_proxy" and not address.is_loopback:
            raise ValueError(
                "生产环境 ERP_TRUSTED_PROXY_IPS 只允许 loopback TCP peer"
            )
    return values


def _environment() -> str:
    environment = os.getenv("ERP_ENVIRONMENT", "development").strip().lower()
    if environment not in {"development", "test", "production"}:
        raise ValueError(
            "ERP_ENVIRONMENT must be one of: development, test, production"
        )
    return environment


def _bind_host(*, production: bool, transport: str) -> str:
    default = "127.0.0.1" if production and transport == "https_proxy" else "0.0.0.0"
    value = os.getenv("ERP_BIND_HOST", default).strip() or default
    if production and transport == "https_proxy":
        try:
            address = ip_address(value)
        except ValueError as error:
            raise ValueError("生产环境 ERP_BIND_HOST 必须是显式 loopback IP") from error
        if not address.is_loopback:
            raise ValueError("生产环境 ERP_BIND_HOST 只允许 loopback IP")
    if production and transport == "lan_http":
        try:
            address = ip_address(value)
        except ValueError as error:
            raise ValueError("lan_http 的 ERP_BIND_HOST 必须是显式 IP") from error
        if value != "0.0.0.0" and not _is_private_lan_host(value):
            raise ValueError("lan_http 的 ERP_BIND_HOST 只允许 0.0.0.0、私网或 loopback IP")
    return value


def _service_url(
    env_name: str,
    *,
    production: bool,
    transport: str,
    port: int,
    default: str,
) -> str:
    value = os.getenv(env_name, "").strip()
    if not value:
        if production:
            raise ValueError(f"生产环境必须显式配置 {env_name} URL")
        return default
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{env_name} 必须是无凭据的 HTTP/HTTPS URL")
    if production and transport == "https_proxy" and parsed.scheme != "https":
        raise ValueError(f"https_proxy 模式的 {env_name} 必须使用 HTTPS")
    if production and transport == "lan_http":
        if parsed.scheme != "http" or not _is_private_lan_host(parsed.hostname):
            raise ValueError(f"lan_http 的 {env_name} 只允许私网 HTTP URL")
        try:
            service_port = parsed.port
        except ValueError as error:
            raise ValueError(f"{env_name} 端口无效") from error
        if service_port != port:
            raise ValueError(f"lan_http 的 {env_name} 必须显式使用 ERP_PORT")
        expected_path = "/api/health" if env_name == "ERP_HEALTH_URL" else "/"
        actual_path = parsed.path or "/"
        if actual_path != expected_path:
            raise ValueError(f"lan_http 的 {env_name} 路径必须为 {expected_path}")
    return value


def load_settings() -> Settings:
    database_path = _resolved_path("ERP_DATABASE_PATH", DEFAULT_DATABASE_PATH)
    backup_dir = _resolved_path("ERP_BACKUP_DIR", DEFAULT_BACKUP_DIR)
    secret_file = _resolved_path("ERP_SECRET_KEY_FILE", DEFAULT_SECRET_FILE)
    environment = _environment()
    is_production = environment == "production"
    production_transport = _production_transport(production=is_production)
    if os.getenv("ERP_UAT_ROOT"):
        # Fail before _load_or_create_secret can touch a copied .env path.
        from app.core.uat_isolation import validate_uat_environment

        validate_uat_environment()
    configured_secret = os.getenv("ERP_SECRET_KEY", "").strip()
    secret_key = configured_secret or _load_or_create_secret(
        secret_file,
        allow_create=not is_production,
    )
    if is_production and len(secret_key) < 32:
        raise RuntimeError("生产环境 ERP_SECRET_KEY 至少需要 32 个字符")
    port = int(os.getenv("ERP_PORT", "8000"))
    workers = int(os.getenv("ERP_WORKERS", "1"))
    if workers != 1:
        raise ValueError(
            "ERP_WORKERS must be 1 because login throttling uses process-local keyed locks"
        )
    cookie_secure_requested = os.getenv(
        "ERP_SESSION_COOKIE_SECURE",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    if is_production and production_transport == "lan_http" and cookie_secure_requested:
        raise ValueError(
            "lan_http 模式必须关闭 ERP_SESSION_COOKIE_SECURE，否则 HTTP 登录会话不可用"
        )
    allowed_origins = (
        _production_allowed_origins(
            transport=production_transport,
            port=port,
        )
        if is_production
        else _allowed_origins()
    )
    lan_http_origin = _lan_http_origin(
        production=is_production, transport=production_transport
    )
    additional_private_http_origins = _additional_private_http_origins(
        production=is_production,
        transport=production_transport,
        lan_http_origin=lan_http_origin,
    )
    private_http_origins = ((lan_http_origin,) if lan_http_origin else ()) + additional_private_http_origins
    if private_http_origins:
        allowed_origins = (*allowed_origins, *private_http_origins)
    health_url = _service_url(
        "ERP_HEALTH_URL",
        production=is_production,
        transport=production_transport,
        port=port,
        default=f"http://127.0.0.1:{port}/api/health",
    )
    browser_url = _service_url(
        "ERP_BROWSER_URL",
        production=is_production,
        transport=production_transport,
        port=port,
        default=f"http://127.0.0.1:{port}/",
    )
    if is_production and production_transport == "lan_http":
        allowed_origin_values = {value.lower() for value in allowed_origins}
        for env_name, value in (
            ("ERP_HEALTH_URL", health_url),
            ("ERP_BROWSER_URL", browser_url),
        ):
            parsed = urlsplit(value)
            service_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
            if service_origin not in allowed_origin_values:
                raise ValueError(
                    f"lan_http 的 {env_name} 来源必须包含在 ERP_ALLOWED_ORIGINS"
                )
    result = Settings(
        database_path=database_path,
        backup_dir=backup_dir,
        allowed_origins=allowed_origins,
        allowed_origin_regex=None if is_production else PRIVATE_LAN_ORIGIN_REGEX,
        trusted_hosts=(
            *_trusted_hosts(production=is_production),
            *(urlsplit(origin).hostname for origin in private_http_origins),
        ),
        trusted_proxy_ips=_trusted_proxy_ips(
            production=is_production,
            transport=production_transport,
        ),
        secret_key=secret_key,
        bind_host=_bind_host(
            production=is_production,
            transport=production_transport,
        ),
        port=port,
        workers=workers,
        health_url=health_url,
        browser_url=browser_url,
        environment=environment,
        production_transport=production_transport,
        sqlite_busy_timeout_ms=int(os.getenv("ERP_SQLITE_BUSY_TIMEOUT_MS", "5000")),
        session_cookie_name=os.getenv(
            "ERP_SESSION_COOKIE_NAME",
            "erp_session",
        ).strip()
        or "erp_session",
        session_expire_minutes=int(
            os.getenv("ERP_SESSION_EXPIRE_MINUTES", "480")
        ),
        session_cookie_secure=(is_production and production_transport == "https_proxy")
        or cookie_secure_requested,
        lan_http_origin=lan_http_origin,
        additional_private_http_origins=additional_private_http_origins,
    )
    if os.getenv("ERP_UAT_ROOT"):
        validate_uat_environment(result)
    return result


settings = load_settings()
