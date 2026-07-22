from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".xlsx"}
ACTIVE_EXTENSIONS = {".html", ".htm", ".svg", ".xml", ".js"}
SENSITIVE_EXTENSIONS = {
    ".env",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".bak",
    ".sql",
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".ps1",
    ".bat",
    ".cmd",
    ".exe",
    ".dll",
}
MAX_EXPECTED_BYTES = 20 * 1024 * 1024


def _signature_matches(path: Path, extension: str) -> bool | None:
    if extension not in ALLOWED_EXTENSIONS:
        return None
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return False
    if extension == ".pdf":
        return head.startswith(b"%PDF-")
    if extension == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if extension == ".xlsx":
        return head.startswith(b"PK\x03\x04")
    return None


def audit(root: Path) -> dict[str, object]:
    resolved = root.resolve()
    report: dict[str, object] = {
        "root": str(resolved),
        "exists": resolved.is_dir(),
        "read_only": True,
        "summary": {
            "files": 0,
            "bytes": 0,
            "findings": 0,
        },
        "findings": [],
    }
    if not resolved.is_dir():
        return report

    findings: list[dict[str, object]] = []
    total_bytes = 0
    files = sorted(path for path in resolved.rglob("*") if path.is_file())
    for path in files:
        size = path.stat().st_size
        total_bytes += size
        extension = path.suffix.lower()
        reasons: list[str] = []
        if path.is_symlink():
            reasons.append("symbolic_link")
        if extension in ACTIVE_EXTENSIONS:
            reasons.append("active_content")
        if extension in SENSITIVE_EXTENSIONS:
            reasons.append("sensitive_type")
        if extension not in ALLOWED_EXTENSIONS:
            reasons.append("unexpected_extension")
        if size > MAX_EXPECTED_BYTES:
            reasons.append("oversized")
        signature_matches = _signature_matches(path, extension)
        if signature_matches is False:
            reasons.append("signature_mismatch")
        if reasons:
            findings.append(
                {
                    "relative_path": path.relative_to(resolved).as_posix(),
                    "size": size,
                    "extension": extension,
                    "reasons": reasons,
                }
            )

    report["summary"] = {
        "files": len(files),
        "bytes": total_bytes,
        "findings": len(findings),
    }
    report["findings"] = findings
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of a legacy public static/uploads directory."
    )
    parser.add_argument("--uploads-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.uploads_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
