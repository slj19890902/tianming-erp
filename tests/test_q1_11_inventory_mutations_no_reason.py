from pathlib import Path

from app.api.warehouse import (
    AdjustPayload,
    QuantityOperationPayload,
    SemiFinishedLotVoidPayload,
)


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_API = (ROOT / "app" / "api" / "warehouse.py").read_text(encoding="utf-8")
WAREHOUSE_SERVICE = (ROOT / "app" / "services" / "warehouse_inventory.py").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_inventory_payloads_accept_missing_blank_and_legacy_reason() -> None:
    assert AdjustPayload(expected_version=1, quantity_delta=2).reason is None
    assert QuantityOperationPayload(expected_version=1, quantity=2).reason is None
    assert SemiFinishedLotVoidPayload(expected_version=1).reason is None
    assert AdjustPayload(expected_version=1, quantity_delta=-2, reason="盘点修正").reason == "盘点修正"


def test_inventory_operations_keep_business_quantity_and_one_confirmation() -> None:
    start = WAREHOUSE.index("async function operate(id,operation,version)")
    end = WAREHOUSE.index("async function loadMovements()", start)
    method = WAREHOUSE[start:end]
    assert 'prompt("输入调整数量' in method
    assert "输入报损数量" in method and "输入报废数量" in method
    assert "请输入调整原因" not in method
    assert "请输入原因" not in method
    assert "请输入转通用原因" not in method
    assert "body.reason" not in method
    assert "调整数量必须为非零整数" in method
    assert "数量必须为正整数" in method
    assert "系统会减少可用库存并保留流水" in method


def test_void_misentered_lot_has_one_confirmation_without_reason() -> None:
    start = WAREHOUSE.index("async function voidSemiLot()")
    end = WAREHOUSE.index("async function openProductAssignments", start)
    method = WAREHOUSE[start:end]
    assert method.count("confirm(") == 1
    assert "prompt(" not in method
    assert "reason:" not in method
    assert "已有预占、消耗、报损、报废或业务来源时系统会拒绝" in method


def test_backend_keeps_version_balance_idempotency_scope_and_audit() -> None:
    for label in (
        "库存数量调整（系统记录）",
        "库存报损（系统记录）",
        "库存报废（系统记录）",
        "转为通用成品库存（系统记录）",
    ):
        assert label in WAREHOUSE_API
    assert "_require_lot_customer_access" in WAREHOUSE_API
    assert "_inventory_operation_replayed" in WAREHOUSE_API
    assert 'action_code=f"warehouse.lot.{operation}"' in WAREHOUSE_API
    assert "lot.version != expected_version" in WAREHOUSE_SERVICE
    assert "lot.quantity_available + quantity < 0" in WAREHOUSE_SERVICE
    assert "quantity > lot.quantity_available" in WAREHOUSE_SERVICE
    assert "InventoryLot.version == expected_version" in WAREHOUSE_SERVICE
    assert "_movement(" in WAREHOUSE_SERVICE
    assert 'or "删除误录半成品批次（系统记录）"' in WAREHOUSE_SERVICE
    assert "批次已有预占、消耗、报损或报废，不能删除" in WAREHOUSE_SERVICE
    assert "批次关联来料或补库业务，不能删除" in WAREHOUSE_SERVICE
