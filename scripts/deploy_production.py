from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from app.core.config import DEFAULT_BACKUP_DIR, PROJECT_ROOT, load_settings, normalize_path
from app.core.database import backup_to_nas


PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "carton_erp.sqlite3"


@dataclass(frozen=True, slots=True)
class PromotionResult:
    source: Path
    target: Path
    backup_path: Path | None
    integrity_check: str
    replaced: bool


def sqlite_integrity_check(path: Path) -> str:
    try:
        with closing(sqlite3.connect(path, timeout=30)) as connection:
            connection.execute("PRAGMA busy_timeout = 30000")
            result = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
    except sqlite3.DatabaseError as error:
        raise RuntimeError(f"数据库完整性检查失败: {path}") from error
    if result.lower() != "ok":
        raise RuntimeError(f"数据库完整性检查失败: {path}: {result}")
    return result


def _sqlite_backup(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source, timeout=30)) as source_connection:
        source_connection.execute("PRAGMA busy_timeout = 30000")
        with closing(sqlite3.connect(destination, timeout=30)) as target_connection:
            source_connection.backup(target_connection)
            target_connection.commit()


def promote_database(
    *,
    source: Path,
    target: Path = PRODUCTION_DATABASE,
    backup_dir: Path | None = None,
) -> PromotionResult:
    source = normalize_path(source)
    target = normalize_path(target)
    if not source.is_file():
        raise FileNotFoundError(f"预览数据库不存在: {source}")
    source_integrity = sqlite_integrity_check(source)

    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if target.is_file():
        backup_result = backup_to_nas(
            source_path=target,
            backup_dir=backup_dir,
            filename_suffix="_pre_production",
        )
        backup_path = backup_result.path

    if source == target:
        return PromotionResult(
            source=source,
            target=target,
            backup_path=backup_path,
            integrity_check=source_integrity,
            replaced=False,
        )

    temporary = target.with_suffix(".sqlite3.deploying")
    try:
        _sqlite_backup(source, temporary)
        temporary_integrity = sqlite_integrity_check(temporary)
        _sqlite_backup(temporary, target)
        target_integrity = sqlite_integrity_check(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink(missing_ok=True)

    return PromotionResult(
        source=source,
        target=target,
        backup_path=backup_path,
        integrity_check=target_integrity,
        replaced=True,
    )


def write_env_file(env_path: Path, values: dict[str, str]) -> None:
    existing = (
        env_path.read_text(encoding="utf-8").splitlines()
        if env_path.exists()
        else []
    )
    pending = dict(values)
    output: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in pending:
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)
    for key, value in pending.items():
        output.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_path.with_suffix(env_path.suffix + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, env_path)


def discover_preview_database(project_root: Path = PROJECT_ROOT) -> Path:
    configured = os.getenv("ERP_PREVIEW_DATABASE_PATH", "").strip()
    if configured:
        return normalize_path(configured)
    candidates = list(
        (project_root / "migration-workfiles").glob("*preview*.sqlite3")
    )
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    return normalize_path(project_root / "data" / "carton_erp.sqlite3")


def build_production_env_values(
    *,
    database_path: Path,
    backup_dir: Path,
    external_url: str,
    trusted_proxy_ips: str,
    secret_key_file: Path | None = None,
    port: int = 8000,
) -> dict[str, str]:
    parsed = urlsplit(external_url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("--external-url 必须是无路径、无凭据的显式 HTTPS URL")
    proxy_ips = ",".join(
        item.strip() for item in trusted_proxy_ips.split(",") if item.strip()
    )
    if not proxy_ips:
        raise ValueError("--trusted-proxy-ips 必须显式配置反向代理 IP")
    for proxy_ip in proxy_ips.split(","):
        try:
            address = ip_address(proxy_ip)
        except ValueError as error:
            raise ValueError("--trusted-proxy-ips 只接受 loopback IP") from error
        if not address.is_loopback:
            raise ValueError(
                "--trusted-proxy-ips 在生产 loopback 后端只允许 127.0.0.1 或 ::1"
            )
    origin = f"https://{parsed.netloc}"
    values = {
        "ERP_ENVIRONMENT": "production",
        "ERP_DATABASE_PATH": str(normalize_path(database_path)),
        "ERP_BACKUP_DIR": str(normalize_path(backup_dir)),
        "ERP_ALLOWED_ORIGINS": origin,
        "ERP_TRUSTED_HOSTS": parsed.hostname,
        "ERP_TRUSTED_PROXY_IPS": proxy_ips,
        "ERP_BIND_HOST": "127.0.0.1",
        "ERP_PORT": str(port),
        "ERP_WORKERS": "1",
        "ERP_HEALTH_URL": f"{origin}/api/health",
        "ERP_BROWSER_URL": f"{origin}/",
    }
    if secret_key_file is not None:
        values["ERP_SECRET_KEY_FILE"] = str(normalize_path(secret_key_file))
    return values


def _validate_explicit_production_secret(values: dict[str, str]) -> None:
    configured_secret = os.getenv("ERP_SECRET_KEY", "").strip()
    if configured_secret:
        if len(configured_secret) < 32:
            raise RuntimeError("生产环境 ERP_SECRET_KEY 至少需要 32 个字符")
        return

    configured_file = values.get("ERP_SECRET_KEY_FILE") or os.getenv(
        "ERP_SECRET_KEY_FILE", ""
    ).strip()
    if not configured_file:
        raise RuntimeError(
            "部署生产配置前必须显式提供 ERP_SECRET_KEY 或 ERP_SECRET_KEY_FILE"
        )
    secret_path = normalize_path(configured_file)
    if not secret_path.is_file():
        raise RuntimeError(f"显式 ERP_SECRET_KEY_FILE 不存在：{secret_path}")
    if len(secret_path.read_text(encoding="utf-8").strip()) < 32:
        raise RuntimeError("ERP_SECRET_KEY_FILE 中的生产会话密钥至少需要 32 个字符")
    values["ERP_SECRET_KEY_FILE"] = str(secret_path)


def validate_production_env_values(values: dict[str, str]) -> None:
    _validate_explicit_production_secret(values)
    managed_keys = set(values)
    previous = {key: os.environ.get(key) for key in managed_keys}
    try:
        os.environ.update(values)
        current = load_settings()
        if not current.is_production or current.bind_host != "127.0.0.1":
            raise RuntimeError("生成的生产配置未通过 loopback 安全校验")
        if current.workers != 1:
            raise RuntimeError("生成的生产配置未通过单 worker 安全校验")
        if current.allowed_origins != (values["ERP_ALLOWED_ORIGINS"],):
            raise RuntimeError("生成的生产 HTTPS origin 未通过配置校验")
        if not values["ERP_HEALTH_URL"].startswith("https://"):
            raise RuntimeError("生产健康检查 URL 必须使用 HTTPS")
        if not values["ERP_BROWSER_URL"].startswith("https://"):
            raise RuntimeError("生产浏览器 URL 必须使用 HTTPS")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全提升已验证数据库并写入生产环境配置。",
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", type=Path, default=PRODUCTION_DATABASE)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
    )
    parser.add_argument("--external-url", required=True)
    parser.add_argument("--trusted-proxy-ips", required=True)
    parser.add_argument("--secret-key-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source = args.source or discover_preview_database()
    configured_backup = os.getenv("ERP_BACKUP_DIR", "").strip()
    backup_dir = args.backup_dir or (
        Path(configured_backup) if configured_backup else DEFAULT_BACKUP_DIR
    )
    production_values = build_production_env_values(
        database_path=args.target,
        backup_dir=backup_dir,
        external_url=args.external_url,
        trusted_proxy_ips=args.trusted_proxy_ips,
        secret_key_file=args.secret_key_file,
    )
    # Validate every generated value before any production database promotion.
    validate_production_env_values(production_values)
    result = promote_database(
        source=source,
        target=args.target,
        backup_dir=backup_dir,
    )

    production_values["ERP_DATABASE_PATH"] = str(result.target)
    write_env_file(
        normalize_path(args.env_file),
        production_values,
    )

    print(f"source={result.source}")
    print(f"target={result.target}")
    print(f"replaced={str(result.replaced).lower()}")
    print(f"integrity_check={result.integrity_check}")
    print(f"backup={result.backup_path or 'none'}")
    print(f"env_file={normalize_path(args.env_file)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
