"""Render customer templates from frozen delivery facts, with server-side price control."""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.models.delivery import DeliveryItem
from app.services.customer_document_fields import decode_snapshot
from app.services.delivery_snapshots import read_sales_contract


def enrich_customer_print(db, delivery, result, user, *, show_prices=None, order_context=None):
    layout = result['print_template']['layout']
    if layout.get('catalog_version') != 'delivery-print-v2':
        return result
    from app.api.orders import _can_view_order_sales_amount
    from app.models.finance import FinanceIdempotencyRecord
    first_print = db.scalar(select(FinanceIdempotencyRecord).where(
        FinanceIdempotencyRecord.action == 'delivery.customer_print_requested',
        FinanceIdempotencyRecord.resource_type == 'delivery',
        FinanceIdempotencyRecord.resource_id == delivery.id,
    ).order_by(FinanceIdempotencyRecord.id))
    original_mode = json.loads(first_print.response_json) if first_print else {}
    allowed = _can_view_order_sales_amount(user)
    if show_prices is True and not allowed:
        raise HTTPException(403, '没有销售金额查看权限，不能打印有价送货单')
    prices = bool(allowed and (original_mode.get('show_prices', layout['show_prices']) if show_prices is None else show_prices))
    context = original_mode.get('order_context', layout['order_context']) if order_context is None else order_context
    if context not in ('', '海外订单') or (context and layout['preset'] != 'yke'):
        raise HTTPException(422, '此客户模板不支持该送货场景')
    records = {item.id: item for item in db.scalars(select(DeliveryItem).where(
        DeliveryItem.delivery_id == delivery.id, DeliveryItem.is_current.is_(True)))}
    rows, warnings, total = [], [], Decimal('0')
    for index, item in enumerate(result['items'], 1):
        record = records[item['delivery_item_id']]
        try:
            identity = decode_snapshot(record.customer_document_snapshot_json)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if identity is None:
            # Do not replace missing historical identity with today's product master.
            identity = {}
            warnings.append(f'第{index}行没有冻结客户资料，请核对原始送货依据')
        if identity.get('conflicts'):
            warnings.append(f'第{index}行客户资料存在不同来源版本，请在常用箱核对后新建单据')
        row = {key: item.get(key, '') for key in (
            'delivery_item_id', 'customer_po', 'product_code', 'product_name',
            'specification', 'unit', 'quantity', 'remarks')}
        row.update({key: identity.get(key) or '' for key in (
            'customer_material_code', 'customer_drawing_number', 'customer_category',
            'customer_model', 'customer_product_name')})
        if identity.get('customer_drawing_display') is not None:
            row['customer_drawing_number'] = identity['customer_drawing_display']
        row['customer_product_name'] = row['customer_product_name'] or row['product_name']
        row['sequence'] = index
        row['identity_basis'] = identity.get('basis', 'missing_snapshot')
        # A commercial parent is counted/priced once. Visible components remain
        # explicit supporting rows and never contribute to the commercial total.
        row['pricing_included'] = True
        if prices:
            try:
                contract = read_sales_contract(record)
                if not contract:
                    raise ValueError('没有冻结销售单价')
                unit_price = Decimal(contract['unit_price'])
                amount = (unit_price * Decimal(str(row['quantity']))).quantize(
                    Decimal(1).scaleb(-layout['amount_decimals']), rounding=ROUND_HALF_UP)
            except (ValueError, TypeError, ArithmeticError) as error:
                raise HTTPException(409, f'第{index}行销售口径未冻结，请选择无价打印并核对原单据') from error
            row.update(unit_price=str(unit_price), amount=str(amount), tax_mode=contract['tax_mode'])
            total += amount
        rows.append(row)
        for component in item.get('actual_goods_lines', []):
            if component.get('line_type') != 'component':
                continue
            rows.append(dict(sequence=f'{index}附', customer_po=row['customer_po'],
                customer_material_code=component.get('product_code', ''),
                customer_product_name=component.get('product_name', ''),
                product_name=component.get('product_name', ''),
                specification=component.get('specification', ''),
                quantity=component.get('quantity', 0), unit=component.get('unit', ''),
                remarks='配套组件（不另计价）', pricing_included=False))
    result.update(customer_document_rows=rows, document_warnings=list(dict.fromkeys(warnings)),
        price_display=dict(allowed=allowed, shown=prices), order_context=context,
        commercial_quantity=sum(int(row['quantity'] or 0) for row in rows if row['pricing_included']))
    if prices:
        result['total_amount'] = str(total)
    quantities = {}
    for row in rows:
        if row['pricing_included']:
            unit = str(row.get('unit') or '')
            quantities[unit] = quantities.get(unit, 0) + int(row['quantity'] or 0)
    result['commercial_quantities'] = quantities
    result['document_hash'] = hashlib.sha256(json.dumps(jsonable_encoder({
        key: result[key] for key in ('id', 'delivery_number', 'delivery_date', 'vehicle_number',
        'sender', 'customer', 'print_template', 'customer_document_rows', 'price_display', 'order_context')
    }), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return result
