"""Read-only audit of managed drawing files against an explicitly named database.

Usage: python scripts/drawing_v2_reconcile.py DB_PATH STORAGE_ROOT
No deletion, repair, migration or database writes are performed.
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def audit(db_path: Path, root: Path) -> dict:
    db_path, root = db_path.resolve(strict=True), root.resolve(strict=True)
    if not db_path.is_file() or not root.is_dir():
        raise ValueError("需要现有只读数据库文件和图纸存储根")
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    references: dict[Path, str] = {}
    try:
        for pdf_ref, pdf_sha, manifest_json in connection.execute(
            "SELECT pdf_reference,pdf_sha256,manifest_json FROM drawing_releases"
        ):
            def record(reference: str, sha: str) -> None:
                if not reference.startswith("private:"):
                    raise ValueError("图纸包含不受支持的存储引用")
                target = (root / reference.removeprefix("private:")).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("图纸存储引用越界")
                references[target] = sha
            record(pdf_ref, pdf_sha)
            manifest = json.loads(manifest_json)
            for snapshot in manifest.get('svg_snapshots', {}).values():
                record(snapshot['reference'], snapshot['sha256'])
            for obj in manifest.get("print_objects", []):
                if obj.get("kind") == "image" or obj.get('asset_reference'):
                    record(obj["asset_reference"], obj["asset_sha256"])
                    if obj.get("original_reference"):
                        record(obj["original_reference"], obj["original_sha256"])
    finally:
        connection.close()
    missing = [str(path) for path in references if not path.is_file()]
    damaged = [str(path) for path, digest in references.items()
               if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != digest]
    present = {path.resolve() for folder in (root / "managed_drawings", root / "managed_drawing_assets")
               if folder.is_dir() for path in folder.iterdir() if path.is_file() and not path.name.endswith(".metadata.json")}
    orphan = [str(path) for path in present - references.keys()]
    return {"referenced": len(references), "missing": sorted(missing),
            "damaged": sorted(damaged), "orphan": sorted(orphan)}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法: python scripts/drawing_v2_reconcile.py DB_PATH STORAGE_ROOT")
    print(json.dumps(audit(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, indent=2))
