from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


def test_wms_url_uses_configured_mobile_origin(monkeypatch) -> None:
    from scripts.generate_wms_qr import build_wms_url

    monkeypatch.setenv("ERP_MOBILE_QR_ORIGIN", "https://erp.example.test")
    assert build_wms_url() == "https://erp.example.test/mobile/?mobile_page=incoming#incoming"


def test_qr_script_generates_png(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    output = tmp_path / "wms_entry_qr.png"
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "generate_wms_qr.py"),
            "--output",
            str(output),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "ERP_MOBILE_QR_ORIGIN": "https://erp.example.test"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "https://erp.example.test/mobile/?mobile_page=incoming#incoming" in result.stdout
