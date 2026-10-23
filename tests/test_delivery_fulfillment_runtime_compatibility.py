"""Offline real manager admission using synthetic SQLite and ephemeral signatures.

No ERP process, keychain, installed Mac manager, formal database or protected
publisher is used. These tests do not prove installed signed Mac execution.
"""
import ast
from pathlib import Path
import sqlite3
from types import MethodType

import pytest

from desktop_assistant import build, service_supervisor
from desktop_assistant.manager import Manager
from desktop_assistant.storage import pack_tree, read_json, sha, write_json
from tests.desktop_assistant.test_stopped_update import case

CAPABILITY = "delivery_fulfillment_facts_v1"
TABLES = ("delivery_fulfillment_revisions", "delivery_fulfillment_segments")


def _mark_schema(case, tables=TABLES, *, populated=False, revision="r1"):
    database = case.manager.root / "shared/data/carton_erp.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE alembic_version SET version_num=?", (revision,))
        for table in tables:
            db.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY)")
            if populated:
                db.execute(f"INSERT INTO {table} VALUES(1)")
    return database


def _program(case, name, *, capability=None, revision="r1", rollback=None):
    metadata = dict(type="tianming.release.v1", version=name, revision=revision,
        reader_capabilities={} if capability is None else {CAPABILITY: capability})
    if rollback is not None:
        metadata["migration"] = dict(policy="preserve_existing_facts_v1",
            from_revision="r1", rollback_package_sha256=rollback)
    package = case.root / (name + ".zip")
    pack_tree(case.root / "source-one", package, metadata, case.key)
    return case.manager.stage_release(package)["id"]


def test_build_declares_exact_integer_fulfillment_reader():
    # Inspect the actual pack_tree metadata expression without building/signing
    # a production package or reading the configured publisher private key.
    tree = ast.parse(Path(build.__file__).read_text(encoding="utf-8"))
    declared = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "reader_capabilities":
                assert isinstance(value, ast.Dict)
                for capability, version in zip(value.keys, value.values):
                    if isinstance(capability, ast.Constant) and capability.value == CAPABILITY:
                        declared.append(ast.literal_eval(version))
    assert len(declared) == 1 and type(declared[0]) is int and declared[0] == 1


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("capability", [None, True, "1", 0, 1])
def test_real_manager_checks_signed_reader_before_same_revision(case, table, populated, capability):
    database = _mark_schema(case, (table,), populated=populated)
    identity = _program(case, "same-revision-reader", capability=capability)
    before, state = sha(database), case.manager.state
    assert case.manager.compatible(identity, "r1") is (type(capability) is int and capability == 1)
    assert sha(database) == before and case.manager.state == state


@pytest.mark.parametrize("populated", [False, True])
def test_signed_schema_authority_never_waives_fulfillment_reader(case, populated):
    database = _mark_schema(case, populated=populated, revision="next-fulfillment")
    old = case.manager.state["current"]
    authority = _program(case, "migration-authority", revision="next-fulfillment",
        capability=1, rollback=old)
    before = sha(database)
    assert not case.manager.compatible(old, "next-fulfillment", authority)
    assert case.manager.compatible(authority, "next-fulfillment", authority)
    assert sha(database) == before


def test_cached_manifest_claim_cannot_add_reader_to_original_signed_package(case):
    database = _mark_schema(case)
    identity = case.manager.state["current"]
    path = case.manager.root / "releases" / identity / "manifest.json"
    cached = read_json(path)
    cached["reader_capabilities"] = {CAPABILITY: 1}
    write_json(path, cached)
    before = sha(database)
    assert not case.manager.compatible(identity, "r1")
    assert sha(database) == before


def test_no_fulfillment_schema_preserves_existing_reader_behavior(case):
    assert case.manager.compatible(case.manager.state["current"], "r1")
    assert not case.manager.compatible(case.manager.state["current"], "next-fulfillment")


@pytest.mark.parametrize("entry", ["start", "supervisor"])
def test_real_start_and_supervisor_reject_before_launch_side_effects(case, monkeypatch, entry):
    database = _mark_schema(case, populated=True)
    manager = case.manager
    state = manager.state
    state["manual_stop"] = False
    write_json(manager.root / "state.json", state)
    before = sha(database)
    monkeypatch.setattr(manager, "_runtime_python", lambda *_args, **_kwargs: Path("/synthetic/not-executed"))
    monkeypatch.setattr(manager, "_process", lambda: None)
    monkeypatch.setattr(manager, "start", MethodType(Manager.start, manager))
    def no_launch(*_args, **_kwargs):
        pytest.fail("unsupported reader reached the data-link/environment/process boundary")
    monkeypatch.setattr(manager, "_link_data", no_launch)
    monkeypatch.setattr(manager, "_environment", no_launch)
    monkeypatch.setattr("desktop_assistant.manager.subprocess.Popen", no_launch)
    with pytest.raises(ValueError):
        manager.start() if entry == "start" else service_supervisor.tick(manager)
    assert sha(database) == before and manager.state == state
    assert not (manager.root / "control/process.json").exists()


def test_running_process_adoption_cannot_bypass_new_reader_contract(case, monkeypatch):
    database = _mark_schema(case, populated=True)
    manager = case.manager
    before, state = sha(database), manager.state
    executable = Path("/synthetic/not-executed")
    monkeypatch.setattr(manager, "_runtime_python", lambda *_args, **_kwargs: executable)
    def forbidden_adoption():
        pytest.fail("reader admission must precede inspecting or adopting a running process")
    monkeypatch.setattr(manager, "_process", forbidden_adoption)
    with pytest.raises(ValueError):
        Manager.start(manager)
    assert sha(database) == before and manager.state == state


def test_same_revision_backup_admission_rejects_missing_reader_before_export(case):
    database = _mark_schema(case, populated=True)
    before, state = sha(database), case.manager.state
    with pytest.raises(ValueError):
        case.manager._backup_inputs()
    assert sha(database) == before and case.manager.state == state


def test_supported_signed_reader_allows_existing_process_and_backup_inputs(case, monkeypatch):
    database = _mark_schema(case, populated=True)
    manager = case.manager
    identity = _program(case, "supported-runtime-reader", capability=1)
    state = manager.state
    state["current"] = identity
    write_json(manager.root / "state.json", state)
    before = sha(database)
    executable = Path("/synthetic/not-executed")
    monkeypatch.setattr(manager, "_runtime_python", lambda *_args, **_kwargs: executable)
    monkeypatch.setattr(manager, "_process", lambda: (object(), {"exe": str(executable)}))
    # Existing-process validation returns without spawning or probing a port.
    Manager.start(manager)
    inputs = manager._backup_inputs()
    assert inputs["current"] == identity
    assert sha(database) == before and manager.state == state
    assert not list((manager.root / "backups").iterdir())
