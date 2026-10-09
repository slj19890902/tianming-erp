"""Immutable single-batch receipts; never infer old completion from live stock."""
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
from app.models.order import OrderItem
from app.models.stock_preparation import StockPreparationCommand as Command, StockPreparationJob as Job
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation, WarehouseLocation
from app.services.stock_preparation import fail
from app.services.stock_preparation_groups import encode
from app.services.product_unit_labels import basis_unit_label


class Receipt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1] = Field(alias='schema')
    operation_key: str
    actor_id: int = Field(gt=0)
    receipt_item_id: int = Field(gt=0)
    customer_id: int | None
    source_lot_id: int = Field(gt=0)
    request: dict
    requested_job_id: int = Field(gt=0)
    original_job_id: int = Field(gt=0)
    completed_job_id: int = Field(gt=0)
    actual_input_quantity: int = Field(gt=0)
    actual_output: int = Field(gt=0)
    output_kind: Literal['semi', 'finished']
    output_lot_id: int = Field(gt=0)
    location_id: int = Field(gt=0)
    layout_version: int | None
    location_name: str
    warehouse_floor: int | None
    input_unit: Literal['张']
    output_unit: str | None
    input_stock_unit: str = Field(min_length=1)
    output_stock_unit: str = Field(min_length=1)
    output_stock_quantity: int = Field(gt=0)
    consume_movement_id: int = Field(gt=0)
    output_movement_id: int = Field(gt=0)
    remaining_input_quantity: int = Field(ge=0)
    continuation_job_id: int | None = Field(default=None, gt=0)
    trace_url: str


def identity(db, receipt_id):
    """Read ownership only, including receipts whose write eligibility changed."""
    receipt = db.get(IncomingReceiptItem, receipt_id)
    if not receipt:
        fail('原收料记录不存在，原请求仍需核对')
    item = db.get(StockReplenishmentOrderItem, receipt.stock_replenishment_item_id) if receipt.stock_replenishment_item_id else None
    if item:
        return item.customer_id, receipt.received_inventory_lot_id
    purpose = db.scalar(select(IncomingReceiptPurposeAllocation).where(
        IncomingReceiptPurposeAllocation.incoming_receipt_item_id == receipt_id))
    order_item = db.get(OrderItem, purpose.source_order_item_id) if purpose and purpose.source_order_item_id else None
    if not purpose or not order_item or order_item.order.customer_id != purpose.customer_id:
        fail('原收料归属不完整，原请求仍需核对')
    return purpose.customer_id, purpose.semi_finished_inventory_lot_id


def normalized_payload(db, body, command=None):
    payload = body.model_dump()
    if 'output_kind' not in body.model_fields_set:
        if command is not None:
            try:
                kind = json.loads(command.request_json)['output_kind']
                if kind not in {'semi', 'finished'}:
                    raise ValueError()
                payload['output_kind'] = kind
                return payload
            except (ValueError, TypeError, KeyError):
                fail('原加工请求证明损坏，请保留原请求核对')
        job = db.get(Job, body.job_id) if body.job_id else None
        if body.action == 'process' or (job and json.loads(job.product_snapshot).get('auto_planned')):
            payload['output_kind'] = 'semi'
    return payload


def build_receipt(db, receipt_id, payload, actor, result):
    """Called under the original write lock, before the parent's first INSERT."""
    db.flush()
    customer_id, source_id = identity(db, receipt_id)
    original = db.get(Job, payload['job_id'])
    completed = db.get(Job, result['completed_job_id'])
    reservation = db.get(InventoryReservation, completed.reservation_id) if completed else None
    output = db.get(InventoryLot, completed.output_lot_id) if completed and completed.output_lot_id else None
    place = db.get(WarehouseLocation, output.warehouse_location_id) if output else None
    consume = db.scalar(select(InventoryMovement).where(InventoryMovement.reservation_id == completed.reservation_id,
        InventoryMovement.movement_type == 'consume')) if completed else None
    entry = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == f'prep-out:{completed.id}')) if completed else None
    if (not original or not completed or not reservation or not output or not place or not consume or not entry
            or completed.status != 'completed' or completed.receipt_item_id != receipt_id
            or original.receipt_item_id != receipt_id or completed.input_quantity != payload['actual_input_quantity']
            or completed.actual_output != payload['actual_output']
            or original.input_quantity != payload['actual_input_quantity'] + result['remaining_input_quantity']
            or reservation.status != 'consumed' or reservation.inventory_lot_id != source_id
            or reservation.consumed_stock_quantity != payload['actual_input_quantity']
            or consume.inventory_lot_id != source_id or consume.quantity != payload['actual_input_quantity']
            or consume.operator_id != actor.id or entry.operator_id != actor.id
            or entry.inventory_lot_id != output.id or entry.movement_type != 'manual_in'
            or output.source_ref_type != 'stock_preparation' or output.source_ref_id != completed.id
            or output.warehouse_location_id != payload['location_id']):
        fail('本次加工保存证明不完整，已回滚，请核对')
    proof = Receipt(schema=1, operation_key=payload['operation_key'], actor_id=actor.id,
        receipt_item_id=receipt_id, customer_id=customer_id, source_lot_id=source_id,
        request=payload, requested_job_id=original.id, original_job_id=original.id,
        completed_job_id=completed.id, actual_input_quantity=completed.input_quantity,
        actual_output=completed.actual_output, output_kind=payload['output_kind'], output_lot_id=output.id,
        location_id=place.id, layout_version=payload['layout_version'], location_name=place.location_name,
        warehouse_floor=place.warehouse_floor, input_unit='张',
        output_unit=basis_unit_label(json.loads(completed.product_snapshot).get('physical_basis') or {}) or None,
        input_stock_unit=consume.unit, output_stock_unit=entry.unit, output_stock_quantity=entry.quantity,
        consume_movement_id=consume.id, output_movement_id=entry.id,
        remaining_input_quantity=result['remaining_input_quantity'], continuation_job_id=result['continuation_job_id'],
        trace_url=f'/api/production/stock-preparation/history/job:{completed.id}').model_dump(by_alias=True)
    frozen = dict(result, completion_receipt=proof)
    # Fail serialization inside the same rollback boundary, never after commit.
    encode(frozen)
    return frozen


def checked_result(command, payload, actor_id, receipt_id):
    if (command.actor_id != actor_id or command.receipt_item_id != receipt_id
            or command.request_json != encode(dict(payload, receipt_id=receipt_id))):
        fail('操作标识与原账号或原内容不一致，请保留原请求核对')
    try:
        result = json.loads(command.result_json)
        if not isinstance(result, dict) or result.get('action') != 'complete':
            raise ValueError()
        if (set(result) - {'action', 'job_id', 'completed_job_id', 'original_job_id',
                'continuation_job_id', 'remaining_input_quantity', 'completion_receipt'}
                or type(result.get('job_id')) is not int or result['job_id'] <= 0
                or result.get('completed_job_id') != result['job_id']
                or type(result.get('completed_job_id')) is not int
                or type(result.get('remaining_input_quantity')) is not int or result['remaining_input_quantity'] < 0
                or 'continuation_job_id' not in result
                or (result['continuation_job_id'] is not None and
                    (type(result['continuation_job_id']) is not int or result['continuation_job_id'] <= 0))
                or ('original_job_id' in result and
                    (type(result['original_job_id']) is not int or result['original_job_id'] != payload['job_id']))):
            raise ValueError()
        proof = result.get('completion_receipt')
        if 'completion_receipt' not in result:
            return result, None
        parsed = Receipt.model_validate(proof).model_dump(by_alias=True)
        if (type(proof.get('schema')) is not int or encode(parsed['request']) != encode(payload) or parsed['operation_key'] != command.operation_key
                or parsed['actor_id'] != actor_id or parsed['receipt_item_id'] != receipt_id
                or parsed['requested_job_id'] != payload['job_id']
                or parsed['original_job_id'] != payload['job_id']
                or parsed['actual_input_quantity'] != payload['actual_input_quantity']
                or parsed['actual_output'] != payload['actual_output']
                or parsed['location_id'] != payload['location_id'] or parsed['layout_version'] != payload['layout_version']
                or parsed['output_kind'] != payload['output_kind']
                or parsed['completed_job_id'] != result.get('job_id')
                or parsed['completed_job_id'] != result.get('completed_job_id')
                or parsed['remaining_input_quantity'] != result.get('remaining_input_quantity')
                or parsed['continuation_job_id'] != result.get('continuation_job_id')
                or parsed['trace_url'] != f'/api/production/stock-preparation/history/job:{parsed["completed_job_id"]}'):
            raise ValueError()
        return result, parsed
    except (ValueError, TypeError, ValidationError):
        fail('原加工保存证明损坏，请保留原请求并联系管理员核对')


def envelope(result, proof, key, actor_id):
    plain = {k: v for k, v in result.items() if k != 'completion_receipt'} if result else None
    job_id = plain.get('completed_job_id') if plain else None
    trace = f'/api/production/stock-preparation/history/job:{job_id}' if type(job_id) is int and job_id > 0 else None
    return dict(status='completed' if proof else 'legacy_trace' if result else 'not_recorded',
        operation_key=key, current_actor_id=actor_id, completion_receipt=proof, result=plain, trace_url=trace)
