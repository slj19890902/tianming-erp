"""Offline, portable evidence gate. Does not run pytest or access production."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

try:
    from .verify_phase3_manifest import confined_path, normalized_lf_sha256, verify
except ImportError:
    from verify_phase3_manifest import confined_path, normalized_lf_sha256, verify


def raw_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_delivery(root: Path, evidence_root: Path, index: dict) -> list[str]:
    failures = []
    try:
        if verify(root, confined_path(root, index["manifest"])):
            failures.append("manifest verification failed")
        if not index["files"] or not index["runs"]:
            failures.append("evidence files and runs must not be empty")
        if not any(run.get("bind_current_sources") is True for run in index["runs"]):
            failures.append("at least one run must bind current sources")
        indexed = {entry["path"] for entry in index["files"]}
        if len(indexed) != len(index["files"]):
            failures.append("duplicate evidence path")
        for entry in index["files"]:
            path = confined_path(evidence_root, entry["path"])
            if not path.is_file() or raw_hash(path) != entry["sha256"]:
                failures.append(f"evidence digest mismatch: {entry['path']}")
        for run in index["runs"]:
            directory = confined_path(evidence_root, run["directory"])
            for name in ("result.json", "pytest.log", "pytest.junit.xml", "source-hashes.json"):
                if f"{run['directory']}/{name}" not in indexed:
                    failures.append(f"run artifact not indexed: {run['directory']}/{name}")
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if result["exit_code"] != 0 or not result.get("ended_at"):
                failures.append(f"run not successfully finished: {run['directory']}")
            for key, name in (("log", "pytest.log"), ("junit", "pytest.junit.xml")):
                if raw_hash(directory / name) != result["artifacts"][key]["sha256"]:
                    failures.append(f"result artifact mismatch: {run['directory']}/{name}")
            junit = ElementTree.parse(directory / "pytest.junit.xml").getroot()
            if any(junit.findall(f".//{tag}") for tag in ("failure", "error", "skipped")):
                failures.append(f"JUnit failures/errors/skips: {run['directory']}")
            cases = [case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                     for case in junit.findall(".//testcase")]
            if not run["nodeids"] or len(cases) != len(set(cases)) or set(cases) != set(run["nodeids"]):
                failures.append(f"exact nodeid set differs: {run['directory']}")
            hashes = json.loads((directory / "source-hashes.json").read_text(encoding="utf-8"))
            tree_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            if not hashes or tree_hash != result["source_tree_sha256"]:
                failures.append(f"source tree binding invalid: {run['directory']}")
            if run.get("bind_current_sources", False):
                for relative, expected in hashes.items():
                    source = confined_path(root, relative)
                    if not source.is_file() or normalized_lf_sha256(source) != expected:
                        failures.append(f"tested source differs: {relative}")
    except (OSError, ValueError, KeyError, TypeError, ElementTree.ParseError) as error:
        failures.append(f"invalid evidence: {type(error).__name__}: {error}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    index = json.loads((root / args.index).read_text(encoding="utf-8"))
    failures = check_delivery(root, args.evidence_root.resolve(), index)
    print(json.dumps({"ok": not failures, "failures": failures}, ensure_ascii=False, indent=2))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
