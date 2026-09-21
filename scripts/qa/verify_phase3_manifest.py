"""Create or verify a newline-normalized Phase 3 evidence manifest.

The manifest intentionally excludes itself.  Source, test and document files
are compared as UTF-8-compatible bytes after CRLF is normalized to LF so a
working tree, a fresh checkout and a Git archive use one deterministic rule.
External pytest artifacts remain raw-byte hashes in their report summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def normalized_lf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_paths(paths: list[str]) -> list[dict[str, str]]:
    entries = []
    for raw_path in sorted(set(paths)):
        path = Path(raw_path)
        if not path.is_file():
            raise SystemExit(f"manifest source file is missing: {raw_path}")
        entries.append({"path": raw_path.replace("\\", "/"), "sha256_lf": normalized_lf_sha256(path)})
    return entries


def verify(root: Path, manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_relative = manifest_path.resolve().relative_to(root.resolve()).as_posix()
    failures: list[str] = []
    for group in manifest["groups"]:
        for entry in group["files"]:
            relative = entry["path"]
            if relative == manifest_relative:
                failures.append("manifest must not hash itself")
                continue
            candidate = root / relative
            actual = normalized_lf_sha256(candidate) if candidate.is_file() else None
            if actual != entry["sha256_lf"]:
                failures.append(f"{relative}: expected {entry['sha256_lf']}, actual {actual}")
    if failures:
        print(json.dumps({"ok": False, "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "groups": [group["name"] for group in manifest["groups"]]}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--source-commit")
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--path", action="append", default=[])
    args = parser.parse_args()
    if args.verify == args.write:
        parser.error("choose exactly one of --verify or --write")
    root = args.root.resolve()
    manifest_path = args.manifest.resolve()
    if args.verify:
        return verify(root, manifest_path)
    if not args.source_commit or not args.group or not args.path:
        parser.error("--write requires --source-commit, --group and --path")
    manifest_relative = manifest_path.relative_to(root).as_posix()
    if manifest_relative in {Path(value).as_posix() for value in args.path}:
        parser.error("manifest cannot include itself")
    groups: dict[str, list[str]] = {name: [] for name in args.group}
    for value in args.path:
        name, separator, relative = value.partition(":")
        if not separator or name not in groups or not relative:
            parser.error("each --path must be GROUP:relative/path")
        groups[name].append(relative)
    payload = {
        "schema_version": 2,
        "hash_rule": "SHA-256 of file bytes with CRLF normalized to LF",
        "source_commit": args.source_commit,
        "manifest_excluded_from_hashed_universe": True,
        "groups": [
            {"name": name, "files": load_paths(paths)}
            for name, paths in groups.items()
        ],
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return verify(root, manifest_path)


if __name__ == "__main__":
    raise SystemExit(main())
