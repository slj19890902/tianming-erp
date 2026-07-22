from __future__ import annotations

from pathlib import Path

from scripts.audit.public_uploads_audit import audit


def test_public_upload_audit_is_read_only_and_flags_risks(tmp_path: Path) -> None:
    uploads = tmp_path / "static" / "uploads"
    uploads.mkdir(parents=True)
    safe = uploads / "safe.pdf"
    active = uploads / "active.svg"
    database = uploads / "formal.sqlite3"
    disguised = uploads / "disguised.png"
    safe.write_bytes(b"%PDF-1.4\n%%EOF")
    active.write_text("<svg></svg>", encoding="utf-8")
    database.write_bytes(b"SQLite format 3\x00")
    disguised.write_bytes(b"not-a-png")
    before = {path.name: path.read_bytes() for path in uploads.iterdir()}

    report = audit(uploads)

    after = {path.name: path.read_bytes() for path in uploads.iterdir()}
    assert before == after
    assert report["read_only"] is True
    assert report["summary"]["files"] == 4
    assert report["summary"]["bytes"] == sum(len(value) for value in before.values())
    assert report["summary"]["findings"] == 3
    findings = {row["relative_path"]: row["reasons"] for row in report["findings"]}
    assert "safe.pdf" not in findings
    assert "active_content" in findings["active.svg"]
    assert "sensitive_type" in findings["formal.sqlite3"]
    assert "signature_mismatch" in findings["disguised.png"]
