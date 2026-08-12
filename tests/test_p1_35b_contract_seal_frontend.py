from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_contract_seal_ui_is_role_and_status_guarded() -> None:
    source = _source()
    assert 'canExportSealedContracts() { return ["admin","boss"]' in source
    assert 'canManageContractSeal() { return String(this.user?.role || "") === "admin"' in source
    assert "['confirmed','converted'].includes(contractDraft.status)" in source
    assert "导出盖章 PDF（永久归档）" in source
    assert "每次成功导出都会永久归档" in source
    assert 'accept="image/png,.png"' in source
    assert "/api/contract-seals/assets" in source
    assert "/api/contract-seals/selection" in source
    assert "/api/contract-seals/contracts/${target.id}/exports" in source
    assert "expected_seal_selection_version" in source
    assert "已有归档不会删除" in source


def test_contract_seal_frontend_uses_post_idempotency_and_keeps_unsealed_export() -> None:
    source = _source()
    assert 'method:"POST"' in source
    assert "contractSealedExportOperationKeys" in source
    assert "重试不会重复归档" in source
    assert "/api/contracts/${target.id}/pdf?expected_version=" in source
    assert "导出 PDF（未盖章）" in source
    assert "网页预览/自助打印" in source
    assert "window.opener" not in source


def test_contract_seal_inline_javascript_parses() -> None:
    source = _source()
    scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", source, flags=re.IGNORECASE)
    inline = max(scripts, key=len)
    result = subprocess.run(
        ["node", "--check", "-"], input=inline, text=True, capture_output=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
