from pathlib import Path

from app.api.warehouse import Floor3PalletClearPayload, Floor3PalletRelocationPayload


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_API = (ROOT / "app" / "api" / "warehouse.py").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_location_payloads_accept_missing_blank_and_legacy_remarks() -> None:
    assert Floor3PalletClearPayload(expected_version=1).remarks is None
    assert Floor3PalletClearPayload(expected_version=1, remarks="").remarks == ""
    assert Floor3PalletRelocationPayload(
        expected_version=1, needs_relocation=True
    ).remarks is None
    assert Floor3PalletRelocationPayload(
        expected_version=1, needs_relocation=False, remarks="现场核对"
    ).remarks == "现场核对"


def test_clear_and_relocation_have_one_confirmation_without_reason() -> None:
    clear = WAREHOUSE.split("async function clearFloor3Pallet(palletId){", 1)[1].split(
        "async function setFloor3Relocation", 1
    )[0]
    relocation = WAREHOUSE.split("async function setFloor3Relocation(palletId,needsRelocation){", 1)[1].split(
        "async function confirmFloor3Placement", 1
    )[0]
    assert clear.count("confirm(") == 1
    assert relocation.count("confirm(") == 1
    assert "prompt(" not in clear
    assert "prompt(" not in relocation
    assert "remarks" not in clear
    assert "remarks" not in relocation


def test_backend_keeps_scope_version_inventory_and_automatic_audit() -> None:
    assert 'or "清空三楼货位栈板（系统记录）"' in WAREHOUSE_API
    assert '"标记三楼栈板待归位（系统记录）"' in WAREHOUSE_API
    assert '"现场确认三楼栈板已归位（系统记录）"' in WAREHOUSE_API
    assert "_require_floor3_pallet_customer_access" in WAREHOUSE_API
    assert "expected_version=payload.expected_version" in WAREHOUSE_API
    assert 'description="清空三楼货位的当前栈板"' in WAREHOUSE_API
    assert '"reason": remarks' in WAREHOUSE_API
    assert "db.rollback()" in WAREHOUSE_API
