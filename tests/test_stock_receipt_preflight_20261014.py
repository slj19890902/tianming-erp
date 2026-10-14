from types import SimpleNamespace

from sqlalchemy import event

from test_p1_36j_incoming_projection import _fixture


def test_unpublished_stock_destination_is_blocked_before_selection(tmp_path, monkeypatch):
    from app.api import incoming
    from app.models.user import User
    from app.services.warehouse_inventory import WarehouseInventoryError

    engine, factory, user_id = _fixture(tmp_path)
    calls = []

    def unavailable(db, **kwargs):
        calls.append(kwargs)
        raise WarehouseInventoryError("RAW-002：该库位所属区域的发布版本不是当前运行地图版本", 409)

    monkeypatch.setattr(incoming, "automatic_raw_material_staging_location", unavailable)
    with factory() as db:
        user = db.get(User, user_id)
        writes = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                writes.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        rows = incoming._stock_replenishment_pending_rows(db, user=user)
        incoming._decorate_rows_with_receipt_purpose(db, rows)
        assert len(rows) == 1  # The second customer's row stays hidden.
        assert rows[0]["receipt_execution_ready"] is False
        assert rows[0]["receipt_location_ready"] is False
        assert "当前运行地图版本" in rows[0]["purpose_issue"]
        assert rows[0]["purpose_status"] == "legacy_unset"
        assert rows[0]["incoming_quantity"] == 50
        assert calls == [{"require_floor3_left": True}]
        assert writes == []
        event.remove(engine, "before_cursor_execute", capture)
    engine.dispose()


def test_stock_blocker_disables_checkbox_and_refresh_can_restore_it(tmp_path):
    from test_p1_91v_incoming_receive_409_busy import _method_body, _run_node

    can_receive = _method_body("canReceiveIncoming(row) {", "incomingSelectedCount() {")
    issue = _method_body("incomingReceiptExecutionIssue(row) {", "toggleAllIncoming(checked) {")
    script = (
        "const assert=require('node:assert/strict'); const vm={"
        "incomingSurplusDispositionRequired:()=>false,"
        "incomingPurposeProjection:()=>({orderSheets:0,reserveSheets:0}),"
        "canReceiveIncoming(row){" + can_receive + "},"
        "incomingReceiptExecutionIssue(row){" + issue + "}};"
        + r"""
const row={source_type:'stock_replenishment',material_status:'pending',source_receivable:true,
  purpose_status:'legacy_unset',receipt_execution_ready:false,receipt_location_ready:false,
  purpose_issue:'RAW-002：该库位所属区域的发布版本不是当前运行地图版本'};
assert.equal(vm.canReceiveIncoming(row),false);
assert.match(vm.incomingReceiptExecutionIssue(row),/当前运行地图版本/);
const refreshed={...row,receipt_execution_ready:true,receipt_location_ready:true,purpose_issue:undefined};
assert.equal(vm.canReceiveIncoming(refreshed),true);
assert.equal(vm.canReceiveIncoming({...refreshed,source_receivable:false}),false);
"""
    )
    _run_node(script, tmp_path, "stock-receipt-checkbox.cjs")


def test_published_stock_destination_reuses_readonly_projection(tmp_path, monkeypatch):
    from app.api import incoming
    from app.models.user import User

    engine, factory, user_id = _fixture(tmp_path)
    calls = []

    def available(db, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=123)

    monkeypatch.setattr(incoming, "automatic_raw_material_staging_location", available)
    with factory() as db:
        user = db.get(User, user_id)
        user.customer_access_mode = "all"
        user.role = "admin"
        rows = incoming._stock_replenishment_pending_rows(db, user=user)
        assert len(rows) == 2
        assert all(row["receipt_execution_ready"] is True for row in rows)
        assert all(row["receipt_location_ready"] is True for row in rows)
        assert calls == [{"require_floor3_left": True}]
    engine.dispose()
