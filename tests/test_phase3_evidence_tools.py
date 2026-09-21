"""Offline evidence contracts: portability, tampering, and path isolation."""
import json
import os
import sys
from pathlib import Path

import pytest

from scripts.qa.verify_phase3_manifest import load_paths, verify
from scripts.qa.run_pytest_with_artifacts import build_environment


def test_manifest_uses_root_and_accepts_lf_equivalence_but_rejects_content_change(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"one\r\ntwo\r\n")
    monkeypatch.chdir(tmp_path)
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"groups": [{"name": "code", "files": load_paths(root, ["source.txt"])}]}))
    source.write_bytes(b"one\ntwo\n")
    assert verify(root, manifest) == 0
    source.write_bytes(b"changed\n")
    assert verify(root, manifest) == 1


def test_manifest_rejects_traversal_self_hash_and_empty_groups(tmp_path):
    with pytest.raises(ValueError):
        load_paths(tmp_path, ["../outside.txt"])
    manifest = tmp_path / "manifest.json"
    for groups in ([], [{"name": "code", "files": []}],
                   [{"name": "code", "files": [{"path": "manifest.json", "sha256_lf": "anything"}]}],
                   [{"name": "code", "files": [{"path": "../outside", "sha256_lf": "anything"}]}]):
        manifest.write_text(json.dumps({"groups": groups}))
        assert verify(tmp_path, manifest) == 1


def test_strict_wrapper_confines_write_paths_and_refuses_dotenv(tmp_path, monkeypatch):
    from scripts.qa import run_pytest_with_artifacts as wrapper
    monkeypatch.setenv("ERP_DATABASE_URL", "sqlite:///not-allowed")
    monkeypatch.setenv("ERP_UAT_ROOT", "not-this-task")
    environment, root = build_environment(tmp_path / "fresh", strict=True)
    assert "ERP_DATABASE_URL" not in environment
    assert "ERP_UAT_ROOT" not in environment  # no UAT server is started
    for key, value in environment.items():
        if key.endswith(("_PATH", "_DIR", "_FILE")) and key.startswith("ERP_"):
            assert Path(value).resolve().is_relative_to(root.resolve()), key
    assert Path(environment["TEMP"]).is_relative_to(root)
    fake_checkout = tmp_path / "checkout"
    fake_checkout.mkdir()
    (fake_checkout / ".env").write_text("ERP_DATABASE_PATH=production")
    monkeypatch.setattr(wrapper, "PROJECT_ROOT", fake_checkout)
    with pytest.raises(RuntimeError, match="without .env"):
        build_environment(tmp_path / "rejected", strict=True)


def test_offline_actual_consumers_are_confined_before_requests():
    from app.core.config import load_settings
    from app.core.uat_isolation import collect_actual_uat_consumers
    measurements = Path(os.environ["TM_PHASE3_ASTRA_RESULT_DIR"])
    root = measurements.parent / "pytest-isolated"
    consumers = collect_actual_uat_consumers(load_settings())
    consumers["log_dir"] = os.environ["ERP_LOG_DIR"]
    for name, path in consumers.items():
        assert Path(path).resolve().is_relative_to(root.resolve()), (name, path)
    assert "app.main" not in sys.modules  # no production lifespan/schedulers
    measurements.mkdir(parents=True, exist_ok=True)
    (measurements / "isolation-consumers.json").write_text(json.dumps({
        "actual_consumers": consumers, "root": str(root),
        "execution": "in-process TestClient on fixture FastAPI apps; no sockets or app.main lifespan",
        "configuration": "no checkout .env; explicit isolated paths; no inherited ERP credentials",
        "test_sqlite": "tmp_path under wrapper --basetemp; conftest temp under TMP/TEMP",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
