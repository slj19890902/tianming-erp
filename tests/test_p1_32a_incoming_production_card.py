from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CARD = (ROOT / "static" / "incoming-production-card.html").read_text(
    encoding="utf-8"
)


def _inline_script() -> str:
    return CARD.split("<script>", 1)[1].split("</script>", 1)[0]


def test_incoming_received_and_history_expose_read_only_card_entry() -> None:
    assert INDEX.count("openIncomingProductionCard(row)") >= 2
    assert "row.receipt_status==='posted' && row.order_item_id" in INDEX
    method = INDEX.split("async openIncomingProductionCard(row) {", 1)[1].split(
        "async loadIncomingHistory() {", 1
    )[0]
    assert "BroadcastChannel" in method
    assert 'window.open(path, "_blank", "noopener")' in method
    assert "浏览器阻止了生产卡页面" in method
    assert "axios.put" not in method
    assert "axios.post" not in method
    assert "receiveIncoming" not in method
    assert "revertIncoming" not in method


def test_card_page_is_a4_large_print_and_has_no_business_write() -> None:
    assert "纸板生产随料卡" in CARD
    assert "@page { size:A4 portrait" in CARD
    assert "本垛实收纸板" in CARD
    assert "本垛最多可生产" in CARD
    assert "生产顺序" in CARD
    assert "工艺待确认" in CARD
    assert "/api/incoming/receipt-items/${encodeURIComponent(receiptItemId)}/production-card" in CARD
    assert 'credentials:"same-origin"' in CARD
    assert "window.opener" not in CARD
    assert "localStorage.clear" not in CARD
    assert "sessionStorage.clear" not in CARD
    assert "method:\"POST\"" not in CARD
    assert "method:\"PUT\"" not in CARD


def test_card_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for production card syntax validation"
    target = tmp_path / "incoming-production-card.js"
    target.write_text(_inline_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
