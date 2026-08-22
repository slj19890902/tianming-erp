from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CARD = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)


def _inline_script() -> str:
    return CARD.split("<script>", 1)[1].split("</script>", 1)[0]


def _incoming_card_method() -> str:
    start = INDEX.index("async openIncomingProductionCard(row) {")
    end = INDEX.index("async loadIncomingHistory() {", start)
    return INDEX[start:end].strip().rstrip(",")


def _run_open_scenario(tmp_path: Path, scenario: str) -> dict[str, object]:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for production card entry validation"
    script = tmp_path / f"incoming-production-card-entry-{scenario}.mjs"
    script.write_text(
        f"""
const method = ({{{_incoming_card_method()}}}).openIncomingProductionCard;
const scenario = {json.dumps(scenario)};
let openCount = 0;
const toasts = [];
Object.defineProperty(globalThis, "crypto", {{
  value: {{ randomUUID: () => "p150a-open-token" }},
  configurable: true,
}});
globalThis.clearTimeout = () => {{}};
globalThis.setTimeout = callback => {{ queueMicrotask(callback); return 1; }};
globalThis.window = {{
  open: (url, target, features) => {{
    openCount += 1;
    if (scenario === "blocked") return null;
    return {{ location: {{ href: url }}, close: () => {{}} }};
  }},
}};
if (scenario === "no-broadcast-channel") {{
  globalThis.BroadcastChannel = undefined;
}} else {{
  globalThis.BroadcastChannel = class {{
    constructor() {{
      if (scenario === "allowed") {{
        queueMicrotask(() => this.onmessage?.({{ data: {{ type: "ready" }} }}));
      }}
    }}
    close() {{}}
  }};
}}
const vm = {{
  incomingProductionCardOpening: false,
  incomingProductionCardRecoveryUrl: "",
  showToast: (message, error) => toasts.push({{ message, error }}),
  openIncomingProductionCard: method,
}};
const row = {{ receipt_item_id: 42, receipt_status: "posted", order_item_id: 7 }};
const first = vm.openIncomingProductionCard(row);
const second = vm.openIncomingProductionCard(row);
await Promise.allSettled([first, second]);
process.stdout.write(JSON.stringify({{
  openCount,
  recoveryUrl: vm.incomingProductionCardRecoveryUrl,
  opening: vm.incomingProductionCardOpening,
  toasts,
}}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_incoming_production_card_has_same_origin_root_route() -> None:
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/incoming-production-card.html")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "生产任务单" in response.text
    assert "receiptMode" in response.text


def test_incoming_received_and_history_expose_read_only_recoverable_entry() -> None:
    incoming = INDEX.split('<template v-else-if="activePage === \'incoming\'">', 1)[1]
    incoming = incoming.split('<template v-else-if="activePage === \'production\'">', 1)[0]
    assert "openSelectedIncomingProductionCards" in incoming
    assert "incomingProductionCardSelections" in INDEX
    assert "/api/incoming/production-card-batch" in INDEX
    assert "openIncomingProductionCard(row)" not in incoming
    assert "row?.receipt_status === \"posted\" && row?.order_item_id" in INDEX
    assert 'v-if="incomingProductionCardRecoveryUrl"' in INDEX
    assert ':href="incomingProductionCardRecoveryUrl"' in INDEX
    assert 'target="_blank"' in INDEX
    assert "点此直接打开生产卡" in INDEX
    method = _incoming_card_method()
    assert "/incoming-production-card.html?id=" in method
    assert method.count("window.open(") == 1
    assert "incomingProductionCardOpening" in method
    assert "incomingProductionCardRecoveryUrl" in method
    assert "axios." not in method
    assert "window.location" not in method
    assert "location.reload" not in method
    assert "axios.put" not in method
    assert "axios.post" not in method
    assert "receiveIncoming" not in method
    assert "revertIncoming" not in method


def test_allowed_popup_and_adjacent_double_click_open_only_one_window(
    tmp_path: Path,
) -> None:
    result = _run_open_scenario(tmp_path, "allowed")

    assert result["openCount"] == 1
    assert result["recoveryUrl"] == ""
    assert result["opening"] is False


def test_blocked_popup_keeps_one_persistent_manual_link_without_retry(
    tmp_path: Path,
) -> None:
    result = _run_open_scenario(tmp_path, "blocked")

    assert result["openCount"] == 1
    assert result["recoveryUrl"] == "/incoming-production-card.html?id=42"
    assert result["opening"] is False


def test_missing_broadcast_channel_keeps_manual_link_without_open_loop(
    tmp_path: Path,
) -> None:
    result = _run_open_scenario(tmp_path, "no-broadcast-channel")

    assert result["openCount"] <= 1
    assert result["recoveryUrl"] == "/incoming-production-card.html?id=42"
    assert result["opening"] is False


def test_old_attempt_cannot_restore_a_card_link_after_session_reset(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for production card race validation"
    script = tmp_path / "incoming-production-card-stale-attempt.mjs"
    script.write_text(
        f"""
const method = ({{{_incoming_card_method()}}}).openIncomingProductionCard;
const timers = [];
const channels = [];
let token = "old-attempt";
Object.defineProperty(globalThis, "crypto", {{
  value: {{ randomUUID: () => token }},
  configurable: true,
}});
globalThis.clearTimeout = id => {{ if (timers[id]) timers[id].cleared = true; }};
globalThis.setTimeout = (callback, delay) => {{
  const id = timers.length;
  timers.push({{ callback, delay, cleared: false }});
  return id;
}};
globalThis.window = {{ open: () => null }};
globalThis.BroadcastChannel = class {{
  constructor() {{ channels.push(this); }}
  close() {{ this.closed = true; }}
}};
const vm = {{
  incomingProductionCardOpening: false,
  incomingProductionCardAttemptId: "",
  incomingProductionCardRecoveryUrl: "",
  incomingProductionCardRecoveryMessage: "",
  showToast() {{}},
  openIncomingProductionCard: method,
}};
const oldPromise = vm.openIncomingProductionCard({{
  receipt_item_id: 42, receipt_status: "posted", order_item_id: 7,
}});
vm.incomingProductionCardOpening = false;
vm.incomingProductionCardAttemptId = "";
vm.incomingProductionCardRecoveryUrl = "";
vm.incomingProductionCardRecoveryMessage = "";
token = "new-attempt";
const newPromise = vm.openIncomingProductionCard({{
  receipt_item_id: 99, receipt_status: "posted", order_item_id: 8,
}});
timers.find(timer => timer.delay === 2500 && !timer.cleared).callback();
await Promise.resolve();
if (vm.incomingProductionCardAttemptId !== "new-attempt") throw new Error("old timeout replaced the current attempt");
if (vm.incomingProductionCardRecoveryUrl) throw new Error("old timeout restored a stale production-card link");
channels[1].onmessage({{ data: {{ type: "ready" }} }});
await Promise.resolve();
for (const timer of timers.filter(timer => timer.delay === 800 && !timer.cleared)) timer.callback();
await Promise.allSettled([oldPromise, newPromise]);
if (vm.incomingProductionCardRecoveryUrl) throw new Error("stale link remained after the current attempt completed");
if (vm.incomingProductionCardOpening) throw new Error("current attempt did not unlock");
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_logout_reset_clears_all_production_card_open_state() -> None:
    logout_start = INDEX.index("async logout() {")
    reset_start = INDEX.index("resetPagePerformanceState() {", logout_start)
    reset_end = INDEX.index("pageCacheFresh(page) {", reset_start)
    logout_body = INDEX[logout_start:reset_start]
    reset_body = INDEX[reset_start:reset_end]

    assert "this.resetPagePerformanceState();" in logout_body
    for field in (
        "incomingProductionCardOpening",
        "incomingProductionCardAttemptId",
        "incomingProductionCardRecoveryUrl",
        "incomingProductionCardRecoveryMessage",
    ):
        assert f"this.{field}" in reset_body


def test_card_page_is_a4_half_page_print_and_keeps_writes_in_explicit_label_refresh() -> None:
    assert "生产任务单" in CARD
    assert "@page { size:A4 portrait" in CARD
    assert "待来料计划版" in CARD
    assert "本次实收" in CARD
    assert "本批最多生产" in CARD
    assert "每个生产任务固定半张 A4" in CARD
    assert "/api/incoming/receipt-items/${encodeURIComponent(receiptItemId)}/production-card" in CARD
    assert 'credentials:"include"' in CARD
    assert "window.opener" not in CARD
    assert "localStorage.clear" not in CARD
    assert "sessionStorage.clear" not in CARD
    load_body = CARD[CARD.index("async function loadPackage()") : CARD.index(
        'retryButton.addEventListener("click", loadPackage)'
    )]
    refresh_body = CARD[CARD.index('labelRefreshButton.addEventListener("click"') :]
    assert 'method:"GET"' in load_body
    assert 'method:"POST"' not in load_body
    assert 'method:"POST"' in refresh_body
    assert "confirmed_not_started:true" in refresh_body
    assert "confirmed_no_prior_print:true" in refresh_body
    assert "window.confirm" in refresh_body
    # Label-plan refresh is the page's one explicit audited write; receiving
    # and production completion still cannot be posted from this print page.
    assert CARD.count('method:"POST"') == 1
    assert "/label-plan-refresh" in CARD
    assert "/api/incoming/" not in CARD.split('method:"POST"', 1)[0][-200:]
    assert "method:\"PUT\"" not in CARD
    assert not (ROOT / "static" / "incoming-production-card.html").exists()


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
