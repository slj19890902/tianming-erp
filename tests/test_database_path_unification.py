from __future__ import annotations

import os
import subprocess
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


def test_phase1_compatibility_backend_no_longer_defaults_to_test_database(
    monkeypatch,
) -> None:
    import importlib
    import phase1_postgres.database as compatibility_database

    with monkeypatch.context() as isolated_environment:
        isolated_environment.delenv("ERP_DATABASE_PATH", raising=False)
        compatibility_database = importlib.reload(compatibility_database)

        assert compatibility_database.DATABASE_PATH == FORMAL_DATABASE
        assert "tm_phase3_dev.sqlite3" not in compatibility_database.DATABASE_URL

    # The environment is restored by this point. Reload the module as well so
    # later tests cannot inherit an engine that still points at the checkout DB.
    compatibility_database.engine.dispose()
    importlib.reload(compatibility_database)


def test_health_only_reports_minimal_liveness_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "health.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sales_orders (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE sales_order_items (id INTEGER PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT INTO sales_orders (id) VALUES (?)",
            [(1,), (2,)],
        )
        connection.executemany(
            "INSERT INTO sales_order_items (id) VALUES (?)",
            [(1,), (2,), (3,)],
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    from app.main import create_app

    application = create_app()
    from fastapi.testclient import TestClient

    with TestClient(application) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert str(database_path) not in response.text


def test_start_script_uses_complete_backend_entrypoint() -> None:
    from app.core.config import DEFAULT_DATABASE_PATH

    bat = (PROJECT_ROOT / "start_erp.bat").read_text(encoding="utf-8")
    launcher = (
        PROJECT_ROOT / "scripts" / "windows" / "start_erp.ps1"
    ).read_text(encoding="utf-8")

    assert "scripts\\windows\\start_erp.bat" in bat
    assert "app.main:app" in launcher
    assert "phase1_postgres.main:app" not in launcher
    assert ".venv\\Scripts\\python.exe" in launcher
    assert "alembic upgrade head" in launcher
    assert DEFAULT_DATABASE_PATH.resolve() == FORMAL_DATABASE
