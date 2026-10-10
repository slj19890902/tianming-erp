from __future__ import annotations

import hashlib
import os
import secrets
from ipaddress import ip_address
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "carton_erp.sqlite3"
DEFAULT_BACKUP_DIR = Path(r"Z:\sata1-18015598002\BoxERP\backups")
DEFAULT_SECRET_FILE = PROJECT_ROOT / "data" / "session_secret.key"
DEFAULT_EMAIL_INTAKE_STORAGE_DIR = PROJECT_ROOT / "data" / "email_order_intake"
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
    sqlite_busy_timeout_ms: int = 5_000
    session_cookie_name: str = "erp_session"
    session_expire_minutes: int = 480
    session_cookie_secure: bool = False
    email_intake_enabled: bool = False
    email_intake_mailbox_key: str = "default"
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_imap_username: str = ""
    email_imap_password_file: Path | None = None
    email_imap_folder: str = "INBOX"
    email_imap_use_ssl: bool = True
    email_imap_starttls: bool = False
    email_imap_timeout_seconds: int = 30
    email_intake_poll_timeout_seconds: int = 300
    email_intake_max_messages: int = 50
    email_intake_max_raw_message_bytes: int = 50 * 1024 * 1024
    email_intake_max_attachments: int = 20
    email_intake_max_attachment_bytes: int = 20 * 1024 * 1024
    email_intake_max_total_attachment_bytes: int = 40 * 1024 * 1024
    email_intake_xlsx_max_zip_members: int = 2_000
    email_intake_xlsx_max_uncompressed_bytes: int = 100 * 1024 * 1024
    email_intake_xlsx_max_compression_ratio: int = 100
    email_intake_max_pdf_pages: int = 100
    email_intake_storage_dir: Path = DEFAULT_EMAIL_INTAKE_STORAGE_DIR

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.database_path.as_posix()}"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


def normalize_path(value: str | Path) -> Path:
    """Normalize a path without requiring a mapped NAS drive to be online."""
    path = Path(value).expanduser()
    try:
        return path.resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(path))


def derive_email_intake_mailbox_key(host: str, username: str, folder: str) -> str:
    """Return a stable, non-sensitive identity for one physical mailbox."""

    canonical = "\0".join(
        (
            (host or "").strip().casefold(),
            (username or "").strip().casefold(),
            (folder or "INBOX").strip().casefold() or "inbox",
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"imap-{digest}"


def _resolved_path(env_name: str, default: Path) -> Path:
    configured = Path(os.getenv(env_name, str(default))).expanduser()
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return normalize_path(configured)


def _optional_resolved_path(env_name: str) -> Path | None:
    value = os.getenv(env_name, "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return normalize_path(path)


def _bool_env(env_name: str, default: bool) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


def _production_allowed_origins() -> tuple[str, ...]:
    """Allow only explicitly configured origins in production."""
    raw = os.getenv("ERP_ALLOWED_ORIGINS", "")
    if not raw.strip():
        raise ValueError("生产环境必须显式配置 ERP_ALLOWED_ORIGINS HTTPS 来源")
    origins = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            origin == "*"
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("生产环境 ERP_ALLOWED_ORIGINS 只允许显式 HTTPS 来源")
    return origins


def _trusted_hosts(*, production: bool) -> tuple[str, ...]:
    if not production:
        return ()
    raw = os.getenv("ERP_TRUSTED_HOSTS", "")
    hosts = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not hosts or "*" in hosts:
        raise ValueError("ERP_TRUSTED_HOSTS must contain explicit hosts")
    return hosts


def _trusted_proxy_ips(*, production: bool) -> tuple[str, ...]:
    raw = os.getenv("ERP_TRUSTED_PROXY_IPS", "")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if production and not values:
        raise ValueError("生产环境必须显式配置 ERP_TRUSTED_PROXY_IPS")
    for value in values:
        if value == "*":
            raise ValueError("ERP_TRUSTED_PROXY_IPS must contain explicit IPs")
        try:
            address = ip_address(value)
        except ValueError as error:
            raise ValueError("ERP_TRUSTED_PROXY_IPS only accepts IP addresses") from error
        if production and not address.is_loopback:
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


def _bind_host(*, production: bool) -> str:
    default = "127.0.0.1" if production else "0.0.0.0"
    value = os.getenv("ERP_BIND_HOST", default).strip() or default
    if production:
        try:
            address = ip_address(value)
        except ValueError as error:
            raise ValueError("生产环境 ERP_BIND_HOST 必须是显式 loopback IP") from error
        if not address.is_loopback:
            raise ValueError("生产环境 ERP_BIND_HOST 只允许 loopback IP")
    return value


def _service_url(
    env_name: str,
    *,
    production: bool,
    default: str,
) -> str:
    value = os.getenv(env_name, "").strip()
    if not value:
        if production:
            raise ValueError(f"生产环境必须显式配置 {env_name} HTTPS URL")
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
    if production and parsed.scheme != "https":
        raise ValueError(f"生产环境 {env_name} 必须使用 HTTPS")
    return value


def load_settings() -> Settings:
    database_path = _resolved_path("ERP_DATABASE_PATH", DEFAULT_DATABASE_PATH)
    backup_dir = _resolved_path("ERP_BACKUP_DIR", DEFAULT_BACKUP_DIR)
    secret_file = _resolved_path("ERP_SECRET_KEY_FILE", DEFAULT_SECRET_FILE)
    environment = _environment()
    is_production = environment == "production"
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
    email_enabled = _bool_env("ERP_EMAIL_INTAKE_ENABLED", False)
    email_password_file = _optional_resolved_path("ERP_EMAIL_IMAP_PASSWORD_FILE")
    email_use_ssl = _bool_env("ERP_EMAIL_IMAP_USE_SSL", True)
    email_starttls = _bool_env("ERP_EMAIL_IMAP_STARTTLS", False)
    email_imap_host = os.getenv("ERP_EMAIL_IMAP_HOST", "").strip()
    email_imap_username = os.getenv("ERP_EMAIL_IMAP_USERNAME", "").strip()
    email_imap_folder = os.getenv("ERP_EMAIL_IMAP_FOLDER", "INBOX").strip() or "INBOX"
    explicit_mailbox_key = os.getenv("ERP_EMAIL_INTAKE_MAILBOX_KEY", "").strip()
    email_mailbox_key = explicit_mailbox_key or derive_email_intake_mailbox_key(
        email_imap_host,
        email_imap_username,
        email_imap_folder,
    )
    email_imap_timeout_seconds = int(
        os.getenv("ERP_EMAIL_IMAP_TIMEOUT_SECONDS", "30")
    )
    email_poll_timeout_seconds = int(
        os.getenv("ERP_EMAIL_INTAKE_POLL_TIMEOUT_SECONDS", "300")
    )
    email_max_messages = int(os.getenv("ERP_EMAIL_INTAKE_MAX_MESSAGES", "50"))
    email_max_raw_message_bytes = int(
        os.getenv("ERP_EMAIL_INTAKE_MAX_RAW_MESSAGE_BYTES", str(50 * 1024 * 1024))
    )
    email_max_attachments = int(os.getenv("ERP_EMAIL_INTAKE_MAX_ATTACHMENTS", "20"))
    email_max_attachment_bytes = int(
        os.getenv("ERP_EMAIL_INTAKE_MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
    )
    email_max_total_attachment_bytes = int(
        os.getenv(
            "ERP_EMAIL_INTAKE_MAX_TOTAL_ATTACHMENT_BYTES", str(40 * 1024 * 1024)
        )
    )
    email_xlsx_max_zip_members = int(
        os.getenv("ERP_EMAIL_INTAKE_XLSX_MAX_ZIP_MEMBERS", "2000")
    )
    email_xlsx_max_uncompressed_bytes = int(
        os.getenv(
            "ERP_EMAIL_INTAKE_XLSX_MAX_UNCOMPRESSED_BYTES",
            str(100 * 1024 * 1024),
        )
    )
    email_xlsx_max_compression_ratio = int(
        os.getenv("ERP_EMAIL_INTAKE_XLSX_MAX_COMPRESSION_RATIO", "100")
    )
    email_max_pdf_pages = int(os.getenv("ERP_EMAIL_INTAKE_MAX_PDF_PAGES", "100"))
    if not email_mailbox_key or len(email_mailbox_key) > 100 or not all(
        char.isalnum() or char in {"-", "_", "."} for char in email_mailbox_key
    ):
        raise ValueError("ERP_EMAIL_INTAKE_MAILBOX_KEY 只能包含字母、数字、点、横线和下划线")
    if not 1 <= email_max_messages <= 500:
        raise ValueError("ERP_EMAIL_INTAKE_MAX_MESSAGES 必须在 1 到 500 之间")
    if not 1 <= email_imap_timeout_seconds <= 300:
        raise ValueError("ERP_EMAIL_IMAP_TIMEOUT_SECONDS 必须在 1 到 300 之间")
    if not 5 <= email_poll_timeout_seconds <= 900:
        raise ValueError("ERP_EMAIL_INTAKE_POLL_TIMEOUT_SECONDS 必须在 5 到 900 之间")
    if not 1 * 1024 * 1024 <= email_max_raw_message_bytes <= 100 * 1024 * 1024:
        raise ValueError(
            "ERP_EMAIL_INTAKE_MAX_RAW_MESSAGE_BYTES 必须在 1048576 到 104857600 之间"
        )
    if not 1 <= email_max_attachments <= 100:
        raise ValueError("ERP_EMAIL_INTAKE_MAX_ATTACHMENTS 必须在 1 到 100 之间")
    if not 1 <= email_max_attachment_bytes <= 100 * 1024 * 1024:
        raise ValueError("ERP_EMAIL_INTAKE_MAX_ATTACHMENT_BYTES 必须在 1 到 104857600 之间")
    if not 1 * 1024 * 1024 <= email_max_total_attachment_bytes <= 100 * 1024 * 1024:
        raise ValueError(
            "ERP_EMAIL_INTAKE_MAX_TOTAL_ATTACHMENT_BYTES 必须在 1048576 到 104857600 之间"
        )
    if not 1 <= email_xlsx_max_zip_members <= 10_000:
        raise ValueError("ERP_EMAIL_INTAKE_XLSX_MAX_ZIP_MEMBERS 必须在 1 到 10000 之间")
    if not 1 * 1024 * 1024 <= email_xlsx_max_uncompressed_bytes <= 250 * 1024 * 1024:
        raise ValueError(
            "ERP_EMAIL_INTAKE_XLSX_MAX_UNCOMPRESSED_BYTES 必须在 1048576 到 262144000 之间"
        )
    if not 1 <= email_xlsx_max_compression_ratio <= 1_000:
        raise ValueError(
            "ERP_EMAIL_INTAKE_XLSX_MAX_COMPRESSION_RATIO 必须在 1 到 1000 之间"
        )
    if not 1 <= email_max_pdf_pages <= 1_000:
        raise ValueError("ERP_EMAIL_INTAKE_MAX_PDF_PAGES 必须在 1 到 1000 之间")
    if email_enabled:
        if not email_imap_host:
            raise ValueError("启用邮箱自动收单时必须配置 ERP_EMAIL_IMAP_HOST")
        if not email_imap_username:
            raise ValueError("启用邮箱自动收单时必须配置 ERP_EMAIL_IMAP_USERNAME")
        if email_password_file is None:
            raise ValueError("启用邮箱自动收单时必须配置 ERP_EMAIL_IMAP_PASSWORD_FILE")
        if not (email_use_ssl or email_starttls):
            raise ValueError("邮箱自动收单必须启用 SSL 或 STARTTLS")
    return Settings(
        database_path=database_path,
        backup_dir=backup_dir,
        allowed_origins=(
            _production_allowed_origins() if is_production else _allowed_origins()
        ),
        allowed_origin_regex=None if is_production else PRIVATE_LAN_ORIGIN_REGEX,
        trusted_hosts=_trusted_hosts(production=is_production),
        trusted_proxy_ips=_trusted_proxy_ips(production=is_production),
        secret_key=secret_key,
        bind_host=_bind_host(production=is_production),
        port=port,
        workers=workers,
        health_url=_service_url(
            "ERP_HEALTH_URL",
            production=is_production,
            default=f"http://127.0.0.1:{port}/api/health",
        ),
        browser_url=_service_url(
            "ERP_BROWSER_URL",
            production=is_production,
            default=f"http://127.0.0.1:{port}/",
        ),
        environment=environment,
        sqlite_busy_timeout_ms=int(os.getenv("ERP_SQLITE_BUSY_TIMEOUT_MS", "5000")),
        session_cookie_name=os.getenv(
            "ERP_SESSION_COOKIE_NAME",
            "erp_session",
        ).strip()
        or "erp_session",
        session_expire_minutes=int(
            os.getenv("ERP_SESSION_EXPIRE_MINUTES", "480")
        ),
        session_cookie_secure=is_production
        or os.getenv(
            "ERP_SESSION_COOKIE_SECURE",
            "false",
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        email_intake_enabled=email_enabled,
        email_intake_mailbox_key=email_mailbox_key,
        email_imap_host=email_imap_host,
        email_imap_port=int(os.getenv("ERP_EMAIL_IMAP_PORT", "993")),
        email_imap_username=email_imap_username,
        email_imap_password_file=email_password_file,
        email_imap_folder=email_imap_folder,
        email_imap_use_ssl=email_use_ssl,
        email_imap_starttls=email_starttls,
        email_imap_timeout_seconds=email_imap_timeout_seconds,
        email_intake_poll_timeout_seconds=email_poll_timeout_seconds,
        email_intake_max_messages=email_max_messages,
        email_intake_max_raw_message_bytes=email_max_raw_message_bytes,
        email_intake_max_attachments=email_max_attachments,
        email_intake_max_attachment_bytes=email_max_attachment_bytes,
        email_intake_max_total_attachment_bytes=email_max_total_attachment_bytes,
        email_intake_xlsx_max_zip_members=email_xlsx_max_zip_members,
        email_intake_xlsx_max_uncompressed_bytes=email_xlsx_max_uncompressed_bytes,
        email_intake_xlsx_max_compression_ratio=email_xlsx_max_compression_ratio,
        email_intake_max_pdf_pages=email_max_pdf_pages,
        email_intake_storage_dir=_resolved_path(
            "ERP_EMAIL_INTAKE_STORAGE_DIR", DEFAULT_EMAIL_INTAKE_STORAGE_DIR
        ),
    )


settings = load_settings()
