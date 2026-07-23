from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI


def _create_database(path: Path, marker: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (marker,))
        connection.commit()


def _read_marker(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT value FROM marker").fetchone()[0])


def test_promote_database_backs_up_target_before_atomic_replacement(
    tmp_path: Path,
) -> None:
    from scripts.deploy_production import promote_database

    source = tmp_path / "preview.sqlite3"
    target = tmp_path / "data" / "carton_erp.sqlite3"
    backup_dir = tmp_path / "nas"
    target.parent.mkdir()
    _create_database(source, "preview")
    _create_database(target, "production-before")

    result = promote_database(
        source=source,
        target=target,
        backup_dir=backup_dir,
    )

    assert _read_marker(target) == "preview"
    assert result.backup_path is not None
    assert _read_marker(result.backup_path) == "production-before"
    assert result.integrity_check == "ok"
    assert not target.with_suffix(".sqlite3.deploying").exists()


def test_promote_same_database_only_backs_up_and_validates(
    tmp_path: Path,
) -> None:
    from scripts.deploy_production import promote_database

    target = tmp_path / "carton_erp.sqlite3"
    _create_database(target, "validated")

    result = promote_database(
        source=target,
        target=target,
        backup_dir=tmp_path / "nas",
    )

    assert result.replaced is False
    assert _read_marker(target) == "validated"
    assert result.backup_path is not None


def test_invalid_preview_database_never_replaces_target(tmp_path: Path) -> None:
    from scripts.deploy_production import promote_database

    source = tmp_path / "broken.sqlite3"
    target = tmp_path / "carton_erp.sqlite3"
    source.write_text("not sqlite", encoding="utf-8")
    _create_database(target, "keep-me")

    with pytest.raises(RuntimeError, match="完整性"):
        promote_database(
            source=source,
            target=target,
            backup_dir=tmp_path / "nas",
        )

    assert _read_marker(target) == "keep-me"


def test_write_env_file_preserves_unrelated_values(tmp_path: Path) -> None:
    from scripts.deploy_production import write_env_file

    env_path = tmp_path / ".env"
    env_path.write_text(
        "CUSTOM_FLAG=keep\nERP_ENVIRONMENT=development\n",
        encoding="utf-8",
    )

    write_env_file(
        env_path,
        {
            "ERP_ENVIRONMENT": "production",
            "ERP_DATABASE_PATH": r"D:\ERP\data\carton_erp.sqlite3",
        },
    )

    content = env_path.read_text(encoding="utf-8")
    assert "CUSTOM_FLAG=keep" in content
    assert "ERP_ENVIRONMENT=production" in content
    assert content.count("ERP_ENVIRONMENT=") == 1
    assert r"ERP_DATABASE_PATH=D:\ERP\data\carton_erp.sqlite3" in content


def test_production_security_removes_docs_and_disables_lan_wildcard_cors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import load_settings
    from app.main import apply_production_security

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "phase13-production-secret-that-is-longer-than-32-characters",
    )
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_HEALTH_URL", "https://erp.example.com/api/health")
    monkeypatch.setenv("ERP_BROWSER_URL", "https://erp.example.com/")
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "127.0.0.1")
    settings = load_settings()
    app = FastAPI()

    apply_production_security(app, settings)

    paths = {route.path for route in app.routes}
    assert "/docs" not in paths
    assert "/redoc" not in paths
    assert "/openapi.json" not in paths
    assert settings.is_production is True
    assert settings.allowed_origins == ("https://erp.example.com",)
    assert settings.allowed_origin_regex is None
    assert settings.session_cookie_secure is True


def test_start_batch_uses_project_venv_one_worker_and_production_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import load_settings

    project_root = Path(__file__).resolve().parents[1]
    bat = (project_root / "start_erp.bat").read_text(encoding="utf-8")
    launcher = (
        project_root / "scripts" / "windows" / "start_erp.ps1"
    ).read_text(encoding="utf-8")

    assert "scripts\\windows\\start_erp.bat" in bat
    assert ".venv\\Scripts\\python.exe" in launcher
    assert "app.main:app" in launcher
    assert '"--host", $BindHost' in launcher
    assert '"--port", $ErpPort.ToString()' in launcher
    assert "from app.core.config import load_settings" in launcher
    assert "https_proxy requires ERP_HEALTH_URL" in launcher
    assert "https_proxy requires ERP_BROWSER_URL" in launcher
    assert "lan_http requires ERP_HEALTH_URL and ERP_BROWSER_URL" in launcher
    assert '"--workers", "1"' in launcher
    assert "erp_server.log" in launcher
    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "n031-test-secret-that-is-longer-than-32-characters",
    )
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_HEALTH_URL", "https://erp.example.com/api/health")
    monkeypatch.setenv("ERP_BROWSER_URL", "https://erp.example.com/")
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "127.0.0.1")
    assert load_settings().is_production is True


def test_generated_production_config_loads_with_loopback_and_https(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.config import load_settings
    from scripts.deploy_production import (
        build_production_env_values,
        validate_production_env_values,
        write_env_file,
    )

    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "generated-production-secret-that-is-longer-than-32-characters",
    )
    values = build_production_env_values(
        database_path=tmp_path / "production.sqlite3",
        backup_dir=tmp_path / "backups",
        external_url="https://erp.example.com",
        trusted_proxy_ips="127.0.0.1",
    )
    validate_production_env_values(values)
    env_path = tmp_path / ".env"
    write_env_file(env_path, values)
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        monkeypatch.setenv(key, value)

    current = load_settings()
    assert current.bind_host == "127.0.0.1"
    assert current.production_transport == "https_proxy"
    assert current.workers == 1
    assert current.allowed_origins == ("https://erp.example.com",)
    assert values["ERP_WORKERS"] == "1"
    assert values["ERP_PRODUCTION_TRANSPORT"] == "https_proxy"
    assert values["ERP_HEALTH_URL"] == "https://erp.example.com/api/health"
    assert values["ERP_BROWSER_URL"] == "https://erp.example.com/"


def test_production_config_generation_fails_closed_without_https_or_proxy(tmp_path: Path) -> None:
    from scripts.deploy_production import build_production_env_values

    with pytest.raises(ValueError, match="HTTPS"):
        build_production_env_values(
            database_path=tmp_path / "production.sqlite3",
            backup_dir=tmp_path / "backups",
            external_url="http://erp.example.com",
            trusted_proxy_ips="127.0.0.1",
        )
    with pytest.raises(ValueError, match="trusted-proxy-ips"):
        build_production_env_values(
            database_path=tmp_path / "production.sqlite3",
            backup_dir=tmp_path / "backups",
            external_url="https://erp.example.com",
            trusted_proxy_ips="",
        )
    with pytest.raises(ValueError, match="loopback"):
        build_production_env_values(
            database_path=tmp_path / "production.sqlite3",
            backup_dir=tmp_path / "backups",
            external_url="https://erp.example.com",
            trusted_proxy_ips="10.0.0.10",
        )


def test_generated_production_config_requires_explicit_preprovisioned_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.deploy_production import (
        build_production_env_values,
        validate_production_env_values,
    )

    monkeypatch.delenv("ERP_SECRET_KEY", raising=False)
    monkeypatch.delenv("ERP_SECRET_KEY_FILE", raising=False)
    values = build_production_env_values(
        database_path=tmp_path / "production.sqlite3",
        backup_dir=tmp_path / "backups",
        external_url="https://erp.example.com",
        trusted_proxy_ips="127.0.0.1",
    )
    with pytest.raises(RuntimeError, match="显式提供"):
        validate_production_env_values(values)

    secret_file = tmp_path / "production-session.key"
    secret_file.write_text("S" * 48, encoding="utf-8")
    values = build_production_env_values(
        database_path=tmp_path / "production.sqlite3",
        backup_dir=tmp_path / "backups",
        external_url="https://erp.example.com",
        trusted_proxy_ips="127.0.0.1",
        secret_key_file=secret_file,
    )
    validate_production_env_values(values)
    assert values["ERP_SECRET_KEY_FILE"] == str(secret_file.resolve())
