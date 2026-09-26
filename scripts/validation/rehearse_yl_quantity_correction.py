"""Rehearse the reviewed YL/00006 plan on a NEW isolated copy only.

This is deliberately not a production repair command. It refuses changed source
facts, records receipt-cost evidence, and leaves commercial history untouched.
"""
import argparse
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.core.time_contract import utc_now_naive
from app.models.audit import OperationLog
from app.models.product import Product
from app.models.user import User
from app.models.delivery import Delivery, DeliveryItem
from app.models.external_packaging_purchase import (
    ExternalPackagingReceiptItem, ExternalPackagingPurchaseItem, ExternalPackagingReceiptReversal,
)
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, FinishedGoodsInventoryDetail
from app.services.delivery_quantities import product_basis, physical_stock_basis, require_physical_stock
from app.services.historical_quantity_ledger import post_audited_quantity_batch
from app.services.warehouse_inventory import _balances
from app.services.warehouse_display_units import lot_display_unit
from scripts.validation.rehearse_customer_diecut_molds import fingerprints
from scripts.validation.rehearse_delivery_quantity_migration import migrate, readonly


def validate_source(path, plan):
    """Require the exact audited lots, movements and commercial source facts."""
    raw = plan['raw_evidence']
    with readonly(path) as db:
        db.row_factory = sqlite3.Row
        ids = [r[0] for r in db.execute('''SELECT l.id FROM inventory_lots l
            JOIN finished_goods_inventory_details d ON d.inventory_lot_id=l.id
            WHERE d.owner_customer_id=136 AND d.product_id=3807 ORDER BY l.id''')]
        assert ids == sorted(r['id'] for r in raw['lots']), 'Product lot scope changed; reaudit'
        for table, rows in [('inventory_lots', raw['lots']), ('inventory_movements', raw['movements'])]:
            for old in rows:
                current = db.execute(f'SELECT * FROM {table} WHERE id=?', (old['id'],)).fetchone()
                assert current is not None and all(current[k] == v for k, v in old.items()), (table, old['id'], 'changed')
        move_ids = [r[0] for r in db.execute('''SELECT m.id FROM inventory_movements m
            JOIN finished_goods_inventory_details d ON d.inventory_lot_id=m.inventory_lot_id
            WHERE d.owner_customer_id=136 AND d.product_id=3807 ORDER BY m.id''')]
        assert move_ids == sorted(r['id'] for r in raw['movements']), 'Inventory events changed; reaudit'


def build_events(db, plan):
    events = []
    moves = plan['raw_evidence']['movements']
    effective = {s['id'] for s in plan['shipments'] if s['status'] == 'dispatched'}
    for row in plan['lot_plan']:
        lot = db.get(InventoryLot, row['lot_id'])
        before, version = _balances(lot), lot.version
        inbound = [m for m in moves if m['inventory_lot_id'] == lot.id
                   and m['movement_type'] == 'location_transfer'
                   and m['after_available'] - m['before_available'] == row['inbound_basis_correction']]
        assert len(inbound) == 1, 'Ambiguous original receipt split'
        sources = [(inbound[0], 'inbound_basis', row['inbound_basis_correction'])]
        outbound = [m for m in moves if m['inventory_lot_id'] == lot.id
                    and m['movement_type'] == 'consume' and m['related_delivery_id'] in effective]
        assert sum(m['quantity'] for m in outbound) == -row['outbound_basis_correction']
        sources += [(m, 'outbound_basis', m['quantity']) for m in outbound]
        for source, kind, quantity in sources:
            events.append(dict(kind=kind, quantity=quantity, source_movement_id=source['id'],
                lot_id=lot.id, customer_id=136, product_id=3807, before=dict(before),
                expected_version=version, physical_unit='片'))
            before['available'] += quantity if kind == 'inbound_basis' else -quantity
            before['consumed'] += quantity if kind == 'outbound_basis' else 0
            version += 1
    assert sum(e['quantity'] for e in events if e['kind'] == 'inbound_basis') == 2021
    assert sum(e['quantity'] for e in events if e['kind'] == 'outbound_basis') == 1700
    return events


def rehearse(source, output, plan_path):
    source = source.resolve(strict=True)
    output = output.resolve()
    permitted = Path('D:/.codex/workspace_artifacts').resolve()
    assert output.is_relative_to(permitted) and not output.exists(), 'Use a NEW artifact directory'
    output.mkdir(parents=True)
    target = output / 'isolated-yl-correction.sqlite3'
    with readonly(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    assert plan['status'] == 'preview_only_not_authorized_to_apply'
    validate_source(target, plan)
    result = migrate(target, 'upgrade', 'eb0926dq', output)
    (output / 'migration.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    with readonly(target) as check:
        assert check.execute('SELECT version_num FROM alembic_version').fetchall() == [('eb0926dq',)]
        assert 'quantity_contract_json' in {r[1] for r in check.execute('PRAGMA table_info(sales_delivery_items)')}
    before_tables = fingerprints(target)
    engine = create_sqlite_engine(target)
    with Session(engine) as db:
        product = db.get(Product, 3807)
        assert product.customer_id == 136 and product.product_code == 'Z.004.000006'
        basis = product_basis(product)
        assert (basis['customer_basis'], basis['physical_basis'], basis['physical_unit']) == (1, 2, '片')
        receipt = db.get(ExternalPackagingReceiptItem, 7)
        purchase = db.get(ExternalPackagingPurchaseItem, 14)
        assert receipt.purchase_item_id == purchase.id
        assert receipt.received_quantity == 4042 and receipt.converted_finished_quantity == 2021
        assert purchase.unit_price == Decimal('1.97') and purchase.line_amount == Decimal('7962.74')
        assert not db.scalar(select(ExternalPackagingReceiptReversal.receipt_id).where(
            ExternalPackagingReceiptReversal.receipt_id == receipt.receipt_id))
        for audited in plan['shipments']:
            delivery, item = db.get(Delivery, audited['id']), db.get(DeliveryItem, audited['item_id'])
            assert delivery.status == audited['status'] and item.delivery_id == delivery.id
            assert item.product_id == product.id and item.delivered_quantity == audited['delivered_quantity']
            assert item.quantity_contract_json is None, 'Do not reinterpret modern snapshots'
        actor = db.scalar(select(User).where(User.is_active.is_(True), User.role == 'admin').order_by(User.id))
        assert actor is not None
        actor_id = actor.id
        events = build_events(db, plan)
        posted = post_audited_quantity_batch(db, events, operator_id=actor_id)
        changes = []
        for row in plan['raw_evidence']['lots']:
            lot = db.get(InventoryLot, row['id'])
            if lot.id == 442:
                continue  # Empty, closed source remains an immutable legacy link.
            detail = db.get(FinishedGoodsInventoryDetail, lot.id)
            change = {'lot_id': lot.id, 'before_unit': lot.unit,
                      'before_cost': str(lot.estimated_unit_cost_snapshot),
                      'before_basis': detail.physical_basis_json}
            if lot.id in {r['lot_id'] for r in plan['lot_plan']}:
                assert lot.source_ref_type == 'external_packaging_receipt_item' and lot.source_ref_id == 7
                assert lot.estimated_unit_cost_snapshot in (None, Decimal('3.94'))
                # The receipt and its frozen purchase price are the evidence,
                # including when an old split lost its cost snapshot entirely.
                lot.estimated_unit_cost_snapshot = purchase.unit_price
                lot.cost_snapshot_source = 'historical_receipt_basis'
                lot.cost_snapshot_detail_json = json.dumps({
                    'schema': 1, 'receipt_item_id': 7, 'purchase_item_id': 14,
                    'received_physical_quantity': 4042, 'purchase_amount': '7962.74',
                    'unit_cost': '1.97', 'physical_unit': '片',
                    'prior_cost': change['before_cost'], 'prior_source': row['cost_snapshot_source'],
                    'prior_detail': row['cost_snapshot_detail_json'],
                    'audit_source_fingerprint': plan['source_fingerprint']}, ensure_ascii=False)
                lot.cost_snapshot_at = utc_now_naive()
            else:
                assert lot.id in plan['untouched_move_in_lot_ids']
                assert lot.estimated_unit_cost_snapshot == Decimal('7.71')
                assert _balances(lot) == {k: row['quantity_' + k] for k in _balances(lot)}
            # boxes/sheets is the existing internal storage classification;
            # the verified quantity marker supplies the employee physical unit.
            detail.physical_basis_json = physical_stock_basis(detail.physical_basis_json, basis)
            lot.version += 1
            require_physical_stock(lot, basis, require_marker=True)
            assert lot_display_unit(lot) == '片'
            changes.append({**change, 'after_unit': lot.unit,
                            'display_unit': lot_display_unit(lot),
                            'after_cost': str(lot.estimated_unit_cost_snapshot), 'balances': _balances(lot)})
        # Cost, quantity basis activation and ledger writes are one transaction.
        db.add(OperationLog(user_id=actor_id, action='historical_quantity_rehearsal',
            resource='inventory_lots',
            entity_type='product', entity_id=3807,
            details=json.dumps({'source_fingerprint': plan['source_fingerprint'],
                'rehearsal_only': True, 'changes': changes}, ensure_ascii=False)))
        db.commit()
    # Persisted replay must not alter stock after units/costs have been activated.
    with Session(engine) as db:
        replay = post_audited_quantity_batch(db, events, operator_id=actor_id)
        assert not any(r['created'] for r in replay)
        assert [r['movement_id'] for r in replay] == [r['movement_id'] for r in posted]
        lots = db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
            FinishedGoodsInventoryDetail.owner_customer_id == 136,
            FinishedGoodsInventoryDetail.product_id == 3807)).all()
        assert sum(l.quantity_available for l in lots) == 3303
        assert sum(l.quantity_consumed for l in lots) == 3400
        assert sum(l.quantity_available for l in lots if l.id in plan['untouched_move_in_lot_ids']) == 2661
        receipt_lots = [l for l in lots if l.id in {r['lot_id'] for r in plan['lot_plan']}]
        assert sum((l.quantity_available + l.quantity_consumed) * l.estimated_unit_cost_snapshot
                   for l in receipt_lots) == Decimal('7962.74')
        db.commit()
    engine.dispose()
    after_tables = fingerprints(target)
    changed = sorted(t for t in before_tables if before_tables[t] != after_tables[t])
    assert set(changed) == {'inventory_lots', 'inventory_movements', 'finished_goods_inventory_details', 'operation_logs'}
    with readonly(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    report = dict(database=str(target), production_modified=False, source_plan=str(plan_path),
        source_fingerprint=plan['source_fingerprint'], before_available=2982,
        inbound_correction=2021, outbound_correction=1700, theoretical_available=3303,
        physical_count_performed=False, partner_move_in_retained=2661,
        receipt_cost_conserved='7962.74', correction_events=events, movements=posted,
        activation_changes=changes, replay_created=0, changed_tables=changed,
        unchanged_tables=len(before_tables)-len(changed), integrity='ok', foreign_key_errors=0)
    (output / 'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k,v in report.items() if k not in ('correction_events', 'activation_changes')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    rehearse(args.source, args.output, args.plan)
