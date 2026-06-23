from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


def test_wms_url_uses_ip_port_and_incoming_path() -> None:
    from scripts.generate_wms_qr import build_wms_url

    assert (
        build_wms_url("192.168.1.25", 8000)
        == "http://192.168.1.25:8000/incoming.html"
    )


def test_lan_ip_selection_prefers_real_private_network_over_virtual_range() -> None:
    from scripts.generate_wms_qr import select_lan_ip

    assert (
        select_lan_ip(["172.28.224.1", "192.168.1.3", "198.18.0.1"])
        == "192.168.1.3"
    )


def test_qr_script_generates_png(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    output = tmp_path / "wms_entry_qr.png"
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "generate_wms_qr.py"),
            "--ip",
            "192.168.1.25",
            "--port",
            "8000",
            "--output",
            str(output),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "http://192.168.1.25:8000/incoming.html" in result.stdout
