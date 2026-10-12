from pathlib import Path
import shutil
import subprocess

from tests.test_finished_goods_inventory_reservation import reservation_db, add_lot

ROOT = Path(__file__).resolve().parents[1]


def test_edited_inventory_refresh_recovers_and_preserves_manual_gate():
    result = subprocess.run([shutil.which('node'), str(ROOT / 'tests/order_inventory_refresh_20261012.cjs')],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_email_queue_request_errors_and_session_races():
    result = subprocess.run([shutil.which('node'), str(ROOT / 'tests/email_queue_request_20261012.cjs')],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_picker_scope_duplicate_import_and_mold_positions():
    result = subprocess.run([shutil.which('node'), str(ROOT / 'tests/order_entry_ui_20261012.cjs')],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_finished_reservation_500_requires_reconcile_before_retry(tmp_path):
    import json
    from tests.test_p1_09c_104_finished_reservation_action_guard import _method_contents
    bodies = {name: _method_contents(name) for name in
              ('confirmFinishedInventoryReservation', 'releaseFinishedInventoryReservation')}
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.confirm=()=>true;globalThis.createIdempotencyKey=()=> 'test-key';
let calls=0;globalThis.axios={{post:async()=>{{calls++;throw {{response:{{status:500}}}};}}}};
const bodies={json.dumps(bodies,ensure_ascii=False)};
(async()=>{{
 for(const [name,body] of Object.entries(bodies)){{
  const ctx={{orderItemForm:{{id:1}},orderItemOrder:{{id:2}},finishedReservationActionState:{{}},
   finishedReservationActionBlocked(){{return !!this.finishedReservationActionState.outcomeUncertain;}},
   resetFinishedReservationActionState(){{this.finishedReservationActionState={{}};}},
   showToast(){{}},orderLineUnitLabel(){{return '个';}},errorMessage(){{return '500';}}}};
  const action=new AsyncFunction(name.startsWith('confirm')?'candidate':'reservation',body).bind(ctx);
  const row=name.startsWith('confirm')?{{lot_id:3,_reserve_qty:20,version:1}}:{{id:3,reserved_stock_quantity:20}};
  await action(row);
  if(!ctx.finishedReservationActionState.outcomeUncertain)throw Error(name+' treats 500 as safely rejected');
  const before=calls;await action(row);if(calls!==before)throw Error('duplicate request issued');
 }}
}})().catch(e=>{{console.error(e);process.exitCode=1;}});
"""
    target = tmp_path / 'reservation_500.cjs'
    target.write_text(script)
    result = subprocess.run([shutil.which('node'), str(target)],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr


def test_manual_finished_reservation_then_unchanged_item_save(reservation_db):
    from app.api.warehouse import create_finished_reservation, FinishedReservationPayload
    from app.api.orders import update_order_item, OrderItemUpdate
    db, facts = reservation_db
    lot = add_lot(db, facts, quantity=80, key='manual-save-lot')
    db.commit()
    payload = FinishedReservationPayload(order_item_id=facts['item'].id, inventory_lot_id=lot.id,
        quantity=20, expected_version=lot.version, idempotency_key='manual-save-reserve')
    response = create_finished_reservation(payload, db=db, user=facts['admin'])
    assert response['reserved_stock_quantity'] == 20
    item = facts['item']
    saved = update_order_item(item.id, OrderItemUpdate(quantity=item.quantity,
        unit_price=item.unit_price, product_code=item.snapshot_product_code,
        product_name=item.snapshot_product_name, specification=item.snapshot_spec,
        material=item.snapshot_material), db=db, user=facts['admin'])
    assert saved['quantity'] == 100
    db.refresh(lot)
    assert (lot.quantity_available, lot.quantity_reserved) == (60, 20)
