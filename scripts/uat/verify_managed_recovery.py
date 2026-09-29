"""Run real managed backup -> restore -> start using only synthetic local data.

Run with the assistant's Python 3.12 tool environment. The runtime source is
read-only; all copies, configuration, keys and databases live beneath --root.
This uses a one-run test signing key, never the factory signing identity.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18929)
    parser.add_argument("--verify-existing", action="store_true", help="Verify the already restored local instance without restoring or starting again")
    parser.add_argument("--backup-again", action="store_true", help="After verification, test managed stop/backup/restart of this isolated instance")
    parser.add_argument("--experience-fixtures", action="store_true", help="Seed synthetic pending, production and backlog page scenarios")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if not args.verify_existing and subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo, text=True).strip():
        raise ValueError("Build the reviewable UAT package from a clean committed workspace")
    root = args.root.resolve()
    if root.parent != Path("D:/tm-uat").resolve() or not root.name.startswith("round-upgrade-"):
        raise ValueError("Use a new D:/tm-uat/round-upgrade-* directory")
    if (root.exists() and not args.verify_existing) or not 18000 <= args.port <= 19999:
        raise ValueError("Fresh root and isolated port required")
    root.mkdir(exist_ok=args.verify_existing)
    (root / "tmp").mkdir(exist_ok=args.verify_existing)
    for name in list(os.environ):
        if name.startswith(("ERP_", "TM_ERP_", "PYTHON")) or name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(name)
    os.environ.update(ERP_ENVIRONMENT="test", ERP_ROUND_TEST_ROOT=str(root),
                      TEMP=str(root / "tmp"), TMP=str(root / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(repo), str(args.runtime_source / "Lib/site-packages")]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from desktop_assistant.manager import Manager
    from desktop_assistant.storage import database_info, pack_tree, sha, write_json
    from desktop_assistant.schema_contract import schema_contract_from_sources

    if args.verify_existing:
        restored = Manager(root / "restored", (root / "test-public.pem").read_bytes())
        assert restored.state.get("restored_at"), "No completed isolated restore to verify"
        database = root / "before/shared/data/carton_erp.sqlite3"
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        result = {"data_time": restored.state["source_time"], "started": False,
                  "restore_proof": "completed state plus prior restore execution; this mode only resumes HTTP verification"}
        verify_running(repo, root, restored, args.port, revision, sha(database), result)
        if args.backup_again:
            backup = restored.backup("isolated-round-backup-password", root / "isolated-backup-storage")
            result["post_restore_backup"] = str(backup)
            verify_running(repo, root, restored, args.port, revision, sha(database), result)
        return

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    (root / "test-public.pem").write_bytes(public)
    source = root / "source"
    source.mkdir()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=repo).decode().split("\0")
    for relative in tracked:
        if not relative:
            continue
        path = Path(relative)
        if path.parts[0] not in {"app", "static", "templates", "desktop_assistant", "factory_twin"} and relative != "main.py":
            continue
        # Tracked app/data files are application assets required at runtime.
        # Only top-level persistent data are excluded by the allowlist above.
        if any(part in {"uploads", "__pycache__", "node_modules"} for part in path.parts) or path.suffix == ".pyc":
            continue
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / path, target)
    runtime = source / "runtime"
    shutil.copytree(args.runtime_source, runtime, ignore=shutil.ignore_patterns("__pycache__", "ocr"))
    shutil.copyfile(repo / "scripts/uat/round_runtime_guard.py", runtime / "sitecustomize.py")
    # The guard runs on every actual server process, before ERP imports.
    assert "import site" in (runtime / "python312._pth").read_text()

    installation = root / "before"
    manager = Manager(installation, public)
    shared = installation / "shared"
    paths = {
        "ERP_DATABASE_PATH": "data/carton_erp.sqlite3", "ERP_SECRET_KEY_FILE": "data/session.key",
        "ERP_BACKUP_DIR": "database_backups", "ERP_LOG_DIR": "logs",
        "ERP_FILE_STORAGE_DIR": "data/private_uploads", "ERP_UPLOAD_TEMP_DIR": "data/upload_tmp",
        "ERP_INVOICE_EXPORT_DIR": "data/invoice_exports", "ERP_INVOICE_ATTACHMENT_DIR": "data/invoice_attachments",
        "ERP_SUPPLIER_INVOICE_ATTACHMENT_DIR": "data/supplier_invoice_attachments",
        "ERP_PDF_TRAINING_DIR": "data/pdf_training", "ERP_LEGACY_UPLOAD_DIR": "legacy_uploads",
        "ERP_TWIN_LAYOUT_RUNTIME_PATH": "factory_twin_data/layout.json",
        "ERP_TWIN_LAYOUT_DRAFT_PATH": "factory_twin_data/draft.json",
        "ERP_TWIN_LAYOUT_BACKUP_DIR": "factory_twin_data/backups",
        "ERP_FACTORY_TWIN_DATABASE_PATH": "factory_twin_data/twin.sqlite3",
        "ERP_DELIVERY_PRINT_SETTINGS_PATH": "data/print_settings.json",
    }
    for name, relative in paths.items():
        target = shared / relative
        (target.parent if target.suffix else target).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / "static/factory_maps/twin_layout_v1.json", shared / paths["ERP_TWIN_LAYOUT_RUNTIME_PATH"])
    environment = {name: "${SHARED}/" + value for name, value in paths.items()}
    environment.update(ERP_ENVIRONMENT="test", ERP_BIND_HOST="127.0.0.1", ERP_PORT=str(args.port),
                       ERP_SESSION_COOKIE_NAME="round_uat_session", ERP_SESSION_COOKIE_SECURE="false",
                       ERP_SECRET_KEY="isolated-round-test-key-never-production-20260929",
                       ERP_ROUND_TEST_ROOT=str(root), ERP_BROWSER_URL=f"http://127.0.0.1:{args.port}",
                       ERP_HEALTH_URL=f"http://127.0.0.1:{args.port}/api/health")
    write_json(shared / "environment.json", environment)
    os.environ.update({name: value.replace("${SHARED}", str(shared)) for name, value in environment.items()})
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from sqlalchemy.orm import Session
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "alembic"))
    revision, = ScriptDirectory.from_config(config).get_heads()
    database = shared / paths["ERP_DATABASE_PATH"]
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version(version_num TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES (?)", (revision,))
    with Session(engine) as session:
        actor = User(username="round-admin", password_hash=hash_password("RoundUat2026!"),
                         role="admin", real_name="隔离体验管理员", is_active=True,
                         must_change_password=False, customer_access_mode="all")
        session.add(actor)
        customer = Customer(name="本轮隔离体验客户", customer_number=1, customer_code="ROUND-UAT", is_active=True)
        session.add(customer)
        session.flush()
        session.add(Product(customer_id=customer.id, product_code="ROUND-BOX-001", customer_material_code="ROUND-BOX-001", product_name="恢复验证纸箱",
                            length_mm=300, width_mm=200, height_mm=100, box_category="normal",
                            box_style="A1", unit="个", report_length_mm=1020, report_width_mm=310))
        if args.experience_fixtures:
            from scripts.uat.round_experience_fixtures import seed
            fixtures = seed(session, shared, customer, actor)
            write_json(root / "experience-fixtures.json", fixtures)
        session.commit()
    engine.dispose()
    (shared / "data/recovery-proof.txt").write_text("synthetic-attachment-restored", encoding="utf8")
    package = root / "test-release.zip"
    print("Building synthetic signed runtime package", flush=True)
    import runpy
    version = runpy.run_path(str(repo / "app/version.py"))["APP_VERSION"]
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    contract = schema_contract_from_sources({p.relative_to(source).as_posix(): p.read_bytes()
        for p in (source / "app/models").rglob("*.py")}, revision)
    pack_tree(source, package, {"type": "tianming.release.v1", "version": version + "-isolated",
        "revision": revision, "git_sha": source_commit, "schema_contract": contract}, key)
    release = manager.stage_release(package)
    write_json(installation / "state.json", {"current": release["id"], "previous": None})
    nas = root / "isolated-backup-storage"
    nas.mkdir()
    source_hash = sha(database)
    print("Creating complete managed backup", flush=True)
    backup = manager.backup("isolated-round-backup-password", nas)
    restored = Manager(root / "restored", public)
    print("Restoring complete backup into new installation", flush=True)
    result = restored.restore(backup, "isolated-round-backup-password")
    assert result["started"] is False
    restored_db = restored.root / "shared/data/carton_erp.sqlite3"
    assert sha(restored_db) == source_hash == sha(database)
    assert (restored.root / "shared/data/recovery-proof.txt").read_text() == "synthetic-attachment-restored"
    env = restored._environment(restored.root / "releases" / restored.state["current"])
    assert env["ERP_ENVIRONMENT"] == "test" and env["ERP_BIND_HOST"] == "127.0.0.1"
    for name in paths:
        assert Path(env[name]).resolve().is_relative_to(restored.root / "shared"), name
    assert all(not name in env for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"))
    # This performs actual Manager.start, including process identity and ready file.
    print("Starting restored managed application", flush=True)
    restored.start()
    verify_running(repo, root, restored, args.port, revision, source_hash, result)


def verify_running(repo, root, restored, port, revision, source_hash, result):
    from desktop_assistant.storage import read_json, write_json
    import httpx
    restored_db = restored.root / "shared/data/carton_erp.sqlite3"
    process = json.loads((restored.root / "control/process.json").read_text(encoding="utf8"))
    assert (root / f"runtime-guard-{process['pid']}.txt").is_file(), "Runtime isolation guard did not load"
    release_root = restored.root / "releases" / restored.state["current"]
    env = restored._environment(release_root)
    env.update(ERP_UAT_STDOUT_PATH=str(restored.root / "shared/logs/probe-stdout.log"),
               ERP_UAT_STDERR_PATH=str(restored.root / "shared/logs/probe-stderr.log"))
    probe = root / "probe_runtime.py"
    shutil.copyfile(repo / "scripts/uat/probe_round_runtime.py", probe)
    checked = subprocess.run([str(release_root / "runtime/python.exe"), str(probe)], cwd=release_root,
                             env=env, check=True, capture_output=True, text=True, encoding="utf8")
    isolation_probe = json.loads(checked.stdout)
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as client:
        health = client.get("/api/health")
        assert health.status_code == 200, health.text
        login = client.post("/api/auth/login", json={"username": "round-admin", "password": "RoundUat2026!"})
        assert login.status_code == 200, login.text
        checks = {}
        for path in ("/api/customers", "/api/master/products", "/api/orders", "/api/system/backups/managed-status", "/"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text[:300])
            checks[path] = response.status_code
        assert client.get("/api/system/backups/managed-status").json()["configured"] is True
        assert client.post("/api/system/backups/restore", json={"filename": "old.sqlite3"}).status_code == 409
    previous_path = root / "recovery-runtime-evidence.json"
    previous = read_json(previous_path) if previous_path.is_file() else {}
    original_restore = previous.get("actual_restore", result) if result.get("restore_proof") else result
    manifest = restored.manifest()
    receipt = {"status": "restored_started_authenticated", "source_commit": manifest.get("git_sha"),
               "candidate_version": manifest["version"], "package_sha256": restored.state["current"],
               "root": str(root), "url": base, "process": restored.state,
               "database": str(restored_db), "revision": revision, "db_sha_before_start": source_hash,
               "actual_restore": original_restore, "latest_verification": result, "actual_http_checks": checks,
               "actual_runtime_isolation_probe": isolation_probe,
               "isolation": {"environment": "test", "bind": "127.0.0.1", "all_paths_below_restored_shared": True,
                             "runtime_guard": "writes/database outside run, external network and subprocesses blocked"},
               "schema_source": "current SQLAlchemy metadata plus unique revision; not a migrated formal copy",
               "not_verified": ["physical printer", "phone camera", "production deployment"]}
    write_json(root / "recovery-runtime-evidence.json", receipt)
    print(json.dumps({"url": base, "evidence": str(root / "recovery-runtime-evidence.json")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
