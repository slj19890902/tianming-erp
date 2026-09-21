"""Negative controls for the portable delivery gate; no application imports."""
import hashlib
import json

import pytest

from scripts.qa.verify_phase3_delivery import check_delivery, raw_hash
from scripts.qa.verify_phase3_manifest import load_paths


@pytest.fixture()
def evidence(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n")
    manifest = {"groups": [{"name": "code", "files": load_paths(root, ["source.py"])}]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    external = tmp_path / "portable-evidence"
    run = external / "run"
    run.mkdir(parents=True)
    (run / "pytest.log").write_text("1 passed\n")
    (run / "pytest.junit.xml").write_text('<testsuites><testsuite><testcase classname="tests.test_demo" name="test_ok"/></testsuite></testsuites>')
    hashes = {"source.py": manifest["groups"][0]["files"][0]["sha256_lf"]}
    (run / "source-hashes.json").write_text(json.dumps(hashes))
    result = {"exit_code": 0, "ended_at": "2026-09-21T00:00:00Z",
              "source_tree_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
              "artifacts": {k: {"sha256": raw_hash(run / name)} for k, name in
                            (("log", "pytest.log"), ("junit", "pytest.junit.xml"))}}
    (run / "result.json").write_text(json.dumps(result))
    index = {"manifest": "manifest.json", "files": [], "runs": [{"directory": "run",
        "bind_current_sources": True, "nodeids": ["tests/test_demo.py::test_ok"]}]}
    return root, external, run, index


def refresh_index(run, index):
    index["files"] = [{"path": "run/" + p.name, "sha256": raw_hash(p)} for p in run.iterdir()]


def test_portable_gate_accepts_complete_evidence(evidence):
    root, external, run, index = evidence
    refresh_index(run, index)
    assert check_delivery(root, external, index) == []


@pytest.mark.parametrize("failure", ["digest", "nonzero", "unfinished", "junit_failure", "missing_nodeid", "source", "outside", "unbound"])
def test_portable_gate_rejects_incomplete_or_changed_evidence(evidence, failure):
    root, external, run, index = evidence
    refresh_index(run, index)
    if failure == "digest":
        (run / "pytest.log").write_text("tampered")
    elif failure in {"nonzero", "unfinished"}:
        result = json.loads((run / "result.json").read_text())
        result["exit_code" if failure == "nonzero" else "ended_at"] = 1 if failure == "nonzero" else None
        (run / "result.json").write_text(json.dumps(result))
        refresh_index(run, index)
    elif failure == "junit_failure":
        (run / "pytest.junit.xml").write_text('<testsuites><testsuite><testcase classname="tests.test_demo" name="test_ok"><failure/></testcase></testsuite></testsuites>')
        result = json.loads((run / "result.json").read_text())
        result["artifacts"]["junit"]["sha256"] = raw_hash(run / "pytest.junit.xml")
        (run / "result.json").write_text(json.dumps(result))
        refresh_index(run, index)
    elif failure == "missing_nodeid":
        index["runs"][0]["nodeids"].append("tests/test_demo.py::test_missing")
    elif failure == "source":
        (root / "source.py").write_text("VALUE = 2\n")
        (root / "manifest.json").write_text(json.dumps({"groups": [{"name": "code", "files": load_paths(root, ["source.py"])}]}))
    elif failure == "outside":
        index["runs"][0]["directory"] = "../outside"
    else:
        index["runs"][0]["bind_current_sources"] = False
    assert check_delivery(root, external, index)
