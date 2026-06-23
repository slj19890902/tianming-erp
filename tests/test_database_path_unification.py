from __future__ import annotations

import os
import subprocess
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_DATABASE = (PROJECT_ROOT / "data" / "carton_erp.sqlite3").resolve()


def test_explicit_erp_database_path_has_priority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.core.config import load_settings

    selected = tmp_path / "selected.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(selected))
    monkeypatch.setenv(
        "TM_ERP_DATABASE_URL",
        "sqlite+pysqlite:///./data/tm_phase3_dev.sqlite3",
    )

    assert load_settings().database_path == selected.resolve()


def test_project_env_loader_populates_database_path_without_overwriting_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.core.config import load_project_env

    env_file = tmp_path / ".env"
    from_file = tmp_path / "from-file.sqlite3"
    from_process = tmp_path / "from-process.sqlite3"
    env_file.write_text(
        f"ERP_DATABASE_PATH={from_file}\nERP_PORT=8123\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ERP_DATABASE_PATH", raising=False)
    monkeypatch.delenv("ERP_PORT", raising=False)
    load_project_env(env_file)
    assert os.environ["ERP_DATABASE_PATH"] == str(from_file)
    assert os.environ["ERP_PORT"] == "8123"

    monkeypatch.setenv("ERP_DATABASE_PATH", str(from_process))
    load_project_env(env_file)
    assert os.environ["ERP_DATABASE_PATH"] == str(from_process)


def test_default_database_is_formal_database_and_not_legacy_test_database(
    monkeypatch,
) -> None:
    from app.core.config import DEFAULT_DATABASE_PATH, load_settings

    monkeypatch.delenv("ERP_DATABASE_PATH", raising=False)

    assert DEFAULT_DATABASE_PATH.resolve() == FORMAL_DATABASE
    assert load_settings().database_path == FORMAL_DATABASE
    assert load_settings().database_path.name != "tm_phase3_dev.sqlite3"


def test_database_path_is_absolute_and_independent_of_working_directory(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("ERP_DATABASE_PATH", None)
    command = [
        sys.executable,
        "-c",
        (
            "from app.core.config import load_settings;"
            "print(load_settings().database_path)"
        ),
    ]

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={
            **environment,
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert Path(result.stdout.strip()).resolve() == FORMAL_DATABASE


def test_phase1_compatibility_backend_no_longer_defaults_to_test_database() -> None:
    from phase1_postgres.database import DATABASE_PATH, DATABASE_URL

    assert DATABASE_PATH == FORMAL_DATABASE
    assert "tm_phase3_dev.sqlite3" not in DATABASE_URL


def test_health_reports_current_formal_database_path(monkeypatch) -> None:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(FORMAL_DATABASE))
    from app.main import create_app

    application = create_app()
    health_endpoint = next(
        route.endpoint
        for route in application.routes
        if route.path == "/api/health" and "GET" in route.methods
    )
    response = health_endpoint()
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert Path(payload["database"]).resolve() == FORMAL_DATABASE
    assert payload["orders_table"] == "sales_orders"
    with sqlite3.connect(
        f"file:{FORMAL_DATABASE.as_posix()}?mode=ro",
        uri=True,
    ) as connection:
        expected_orders = connection.execute(
            "SELECT COUNT(*) FROM sales_orders"
        ).fetchone()[0]
        expected_items = connection.execute(
            "SELECT COUNT(*) FROM sales_order_items"
        ).fetchone()[0]
    assert payload["orders_count"] == expected_orders
    assert payload["order_items_count"] == expected_items


def test_start_script_uses_complete_backend_entrypoint() -> None:
    bat = (PROJECT_ROOT / "start_erp.bat").read_text(encoding="utf-8")
    launcher = (
        PROJECT_ROOT / "scripts" / "admin" / "start_erp_background.ps1"
    ).read_text(encoding="utf-8")
    env_file = (PROJECT_ROOT / ".env").read_text(encoding="utf-8")

    assert "start_erp_background.ps1" in bat
    assert "app.main:app" in launcher
    assert "phase1_postgres.main:app" not in launcher
    assert "ERP_DATABASE_PATH" in env_file
