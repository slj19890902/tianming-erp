"""Recover a lossy derived mold field from its original purchase-owned facts.

This is a read-only qualification, not a physical confirmation or a rewrite.
Only a completed, fully traced stock assembly with missing child mold fields
can use it.  All other frozen physical fields must already match the order.
"""
import hashlib
import json
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.orm import object_session
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.stock_preparation import StockPreparationJob, StockPreparationCommand
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.warehouse_inventory import InventoryMovement, InventoryLot
from app.services.stock_purchase_identity import _signature


def _json(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('invalid frozen document')
    return value


def _positive(value):
    if isinstance(value, bool) or int(value) != value or int(value) <= 0:
        raise ValueError('invalid identity or quantity')
    return int(value)


def _purchase_source_valid(db, job, snapshot, customer_id):
    identity = snapshot['purchase_identity']
    if (identity['schema'] != 1 or identity['product_id'] != job.product_id
            or identity['customer_id'] != customer_id
            or identity['product_version'] != snapshot['product_version']
            or _positive(identity['product_version']) <= 0
            or identity['fields']['customer_id'] != customer_id
            or _json(identity['physical_basis']) != _json(snapshot['physical_basis'])):
        return False
    receipt = db.get(IncomingReceiptItem, job.receipt_item_id)
    if not receipt or receipt.status != 'posted' or not receipt.stock_replenishment_item_id:
        return False
    item = db.get(StockReplenishmentOrderItem, receipt.stock_replenishment_item_id)
    if (not item or item.customer_id != customer_id
            or (item.reference_product_id or item.product_id) != job.product_id
            or identity['purchase_signature'] != _signature(item)):
        return False
    if identity['source'] == 'purchase_snapshot':
        return bool(item.production_snapshot_json
                    and _json(item.production_snapshot_json) == identity)
    if identity['source'] != 'master_history':
        return False
    history = db.get(MasterDataObjectVersion, identity['history_id'])
    if (not history or history.object_type != 'product' or history.object_id != job.product_id
            or history.version != identity['product_version']
            or hashlib.sha256(history.snapshot_json.encode()).hexdigest() != history.snapshot_sha256
            or history.created_at > item.created_at):
        return False
    fields = _json(history.snapshot_json)
    return all(fields.get(key) == value for key, value in identity['fields'].items())


def matches_proven_assembly(lot, expected):
    """Compare only to the consuming order's frozen identity, without writes."""
    if not expected or not hasattr(lot, '_sa_instance_state'):
        return False
    db = object_session(lot)
    if not db or not lot.finished_detail:
        return False
    try:
        detail = lot.finished_detail
        actual, wanted = _json(detail.physical_basis_json), _json(expected)
        if (lot.inventory_type != 'finished' or lot.source_ref_type != 'preparation_assembly'
                or lot.source_ref_id != lot.id or detail.is_general
                or actual['schema'] != 1 or wanted['schema'] != 1
                or actual['product_id'] != detail.product_id
                or {k:v for k,v in actual.items() if k != 'assembly'}
                   != {k:v for k,v in wanted.items() if k != 'assembly'}):
            return False
        old = {r['product_id']: r for r in actual['assembly']}
        new = {r['product_id']: r for r in wanted['assembly']}
        if not old or set(old) != set(new) or len(old) != len(actual['assembly']) or len(new) != len(wanted['assembly']):
            return False
        entries = list(db.scalars(select(InventoryMovement).where(
            InventoryMovement.inventory_lot_id == lot.id,
            InventoryMovement.movement_type == 'manual_in',
            InventoryMovement.idempotency_key.like('prep-kit:%'))))
        if len(entries) != 1:
            return False
        entry = entries[0]
        command = db.get(StockPreparationCommand, entry.idempotency_key[len('prep-kit:'):])
        if not command or command.actor_id != entry.operator_id:
            return False
        result = _json(command.result_json)
        if (result['action'] != 'assemble' or result['output_lot_id'] != lot.id
                or _positive(result['sets']) != entry.quantity
                or result['recipe']['parent_id'] != detail.product_id
                or result['recipe']['customer_id'] != detail.owner_customer_id
                or _json(result['recipe']['parent_basis']) != actual):
            return False
        quantities = defaultdict(int)
        for source in result['inputs']:
            pid = source['product_id']
            child = db.get(InventoryLot, source['lot_id'])
            if not child or child.source_ref_type != 'stock_preparation' or pid not in old:
                return False
            job = db.get(StockPreparationJob, child.source_ref_id)
            if (not job or job.status != 'completed' or job.product_id != pid
                    or job.output_lot_id != child.id or _positive(job.actual_output) < _positive(source['quantity'])):
                return False
            snapshot = _json(job.product_snapshot)
            basis = _json(snapshot['physical_basis'])
            if (hashlib.sha256(snapshot['physical_basis'].encode()).hexdigest() != old[pid]['basis_hash']
                    or old[pid]['quantity'] != new[pid]['quantity']
                    or snapshot['sheet_cutting_snapshot']['is_die_cut'] is not True
                    or basis.get('mold_tool_id') is not None
                    or not _purchase_source_valid(db, job, snapshot, detail.owner_customer_id)):
                return False
            # The omitted value comes from the original purchase/history, never
            # from the expected order or today's editable product master.
            basis['mold_tool_id'] = str(_positive(snapshot['purchase_identity']['fields']['mold_tool_id']))
            corrected = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if hashlib.sha256(corrected.encode()).hexdigest() != new[pid]['basis_hash']:
                return False
            child_detail = child.finished_detail
            if child_detail:
                identity = (child_detail.product_id, child_detail.owner_customer_id,
                            child_detail.physical_basis_json) if not child_detail.is_general else None
            else:
                # A completed die-cut piece deliberately stays semi_finished;
                # its explicit output-piece profile owns the same 1:1 identity.
                from app.services.processed_component_stock import output_identity
                identity = output_identity(db, child)
            if (not identity or identity[:2] != (pid, detail.owner_customer_id)
                    or _json(identity[2]) != _json(snapshot['physical_basis'])):
                return False
            movement = db.scalar(select(InventoryMovement).where(
                InventoryMovement.idempotency_key == source['movement_key']))
            if (not movement or movement.inventory_lot_id != child.id
                    or movement.movement_type != 'consume' or movement.quantity != source['quantity']
                    or movement.operator_id != command.actor_id):
                return False
            quantities[pid] += source['quantity']
        return dict(quantities) == {pid: entry.quantity * _positive(row['quantity']) for pid, row in old.items()}
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
