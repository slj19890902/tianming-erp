"""Read-only current descriptions and compact rendering; commercial facts stay frozen."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from sqlalchemy import select

from app.models.delivery import DeliveryItem
from app.models.order import OrderItem
from app.models.product import Product
from app.services.delivery_snapshots import read_sales_contract
from app.services.product_specification import delivery_specification


def compact_layout(envelope):
    result = deepcopy(envelope)
    layout = result['layout']
    if layout.get('catalog_version') == 'delivery-print-v1':
        labels = [('sequence', '序号'), ('customer_po', '客户单号'),
                  ('product_code', '存货编码'), ('product_name', '产品名称'),
                  ('specification', '规格'), ('unit', '单位'),
                  ('quantity', '数量'), ('remarks', '备注')]
        widths = layout['column_widths']
        layout = dict(catalog_version='delivery-print-v2', preset='generic',
                      columns=[dict(key=k, label=label, width=widths[k]) for k, label in labels],
                      show_prices=False, show_remarks=layout.get('show_remarks', True),
                      price_decimals=3, amount_decimals=2, order_context='')
    else:
        layout = deepcopy(layout)
    if layout['preset'] == 'generic' and not layout.get('presentation_version'):
        # 2.5% of the 188mm safe table is 4.7mm: two 13pt half-em digits.
        # A 5.5% unit cell fits two 11pt Chinese characters plus padding.
        columns = {c['key']: c for c in layout['columns']}
        for receiver, donor, increase in (('product_code', 'product_name', 2.5),
                                           ('unit', 'remarks', 1.5)):
            if receiver in columns and donor in columns:
                delta = min(increase, max(0, columns[donor]['width'] - 3))
                columns[receiver]['width'] += delta
                columns[donor]['width'] -= delta
    layout.update(presentation_version='compact-20261017', show_headers=True, font_size_pt=11)
    for column in layout['columns']:
        if column['key'] == 'customer_model':
            column['label'] = '型番'
        if column['key'] == 'customer_material_code' and layout['preset'] in ('kew', 'yke'):
            column.update(key='product_code', label='存货编码')
    if layout['preset'] == 'kew':
        widths = dict(sequence=5, customer_model=24, customer_drawing_number=12,
                      product_code=17, customer_category=7, quantity=11, unit_price=11, amount=13)
        for column in layout['columns']:
            column['width'] = widths.get(column['key'], column['width'])
    result['source_layout_hash'] = result.get('layout_hash')
    result['layout'] = layout
    result['layout_hash'] = hashlib.sha256(json.dumps(layout, sort_keys=True, ensure_ascii=False,
                                                      separators=(',', ':')).encode()).hexdigest()
    return result


def current_descriptions(db, delivery, records):
    """Resolve identities by stable ID and exact customer ownership, never by code."""
    order_ids = {r.order_item_id for r in records if r.order_item_id is not None and r.product_id is None}
    order_products = dict(db.execute(select(OrderItem.id, OrderItem.product_id).where(
        OrderItem.id.in_(order_ids))).all()) if order_ids else {}
    product_ids = {r.product_id or order_products.get(r.order_item_id) for r in records}
    products = {
        p.id: p for p in db.scalars(select(Product).where(Product.id.in_(product_ids - {None})))
        if p.customer_id == delivery.customer_id and p.deleted_at is None
    }
    result = {}
    for record in records:
        product = products.get(record.product_id or order_products.get(record.order_item_id))
        if product is None:
            continue
        result[record.id] = dict(
            product_name=product.product_name or '',
            specification=delivery_specification(product),
            delivery_specification=delivery_specification(product),
            customer_drawing_number=product.customer_drawing_number or '',
            customer_category=product.customer_category or '',
            customer_category_unresolved=not bool((product.customer_category or '').strip()),
            customer_model=product.customer_model or '',
            description_product_id=product.id,
            description_product_version=product.version,
        )
    return result


def prepare_current_print(db, delivery, result):
    records = list(db.scalars(select(DeliveryItem).where(
        DeliveryItem.delivery_id == delivery.id, DeliveryItem.is_current.is_(True))))
    descriptions = current_descriptions(db, delivery, records)
    present_component_descriptions(db, delivery, result['items'], result.get('actual_goods_items', []))
    result['print_template'] = compact_layout(result['print_template'])
    result['sender'] = {**result['sender'], 'company_name': '苏州天明包装有限公司'}
    result['description_mode'] = 'current'
    result['description_versions'] = []
    for item in result['items']:
        # Missing/deleted/foreign masters must not expose an old production spec.
        item.update(specification='', delivery_specification='')
        for line in item.get('actual_goods_lines', []):
            if line.get('line_type') != 'component':
                line['specification'] = ''
        current = descriptions.get(item['delivery_item_id'])
        if current is None:
            continue
        item.update(current)
        result['description_versions'].append({k: current[k] for k in (
            'description_product_id', 'description_product_version')})
        for line in item.get('actual_goods_lines', []):
            if line.get('line_type') != 'component':
                line.update(product_name=current['product_name'], specification=current['specification'])
    for line in result.get('actual_goods_items', []):
        if line.get('line_type') != 'component':
            line['specification'] = ''
        current = descriptions.get(line.get('delivery_item_id'))
        if current and line.get('line_type') != 'component':
            line.update(product_name=current['product_name'], specification=current['specification'])
    return result


def delivery_amounts(db, delivery_ids):
    """One commercial delivery item is counted once; unknown frozen prices stay unknown."""
    totals = {int(i): Decimal('0') for i in delivery_ids}
    unknown = set()
    for item in db.scalars(select(DeliveryItem).where(
            DeliveryItem.delivery_id.in_(delivery_ids), DeliveryItem.is_current.is_(True))):
        try:
            contract = read_sales_contract(item)
            price = Decimal(contract['unit_price']) if contract else item.unit_price_snapshot
            if price is None or not Decimal(str(price)).is_finite() or Decimal(str(price)) < 0:
                raise ValueError('missing frozen price')
            totals[item.delivery_id] += (Decimal(str(price)) * item.delivered_quantity).quantize(
                Decimal('.001'), rounding=ROUND_HALF_UP)
        except (ValueError, TypeError, ArithmeticError):
            unknown.add(item.delivery_id)
    return {i: dict(total_amount=None if i in unknown else str(total.quantize(Decimal('.001'))),
                    amount_status='missing_frozen_price' if i in unknown else 'complete')
            for i, total in totals.items()}


def present_component_descriptions(db, delivery, items, actual_goods_items=()):
    """Resolve component display by stable product identity; keep frozen evidence."""
    links = {}
    for item in items:
        for line in item.get('component_lines', []):
            sid = line.get('component_snapshot_id') or line.get('snapshot_id')
            if sid and line.get('component_product_id'):
                links[sid] = line['component_product_id']
    groups = [[*item.get('component_lines', []), *item.get('actual_goods_lines', [])] for item in items]
    groups.append(actual_goods_items)
    ids = set(links.values()) | {line['component_product_id'] for lines in groups
                                for line in lines if line.get('component_product_id')}
    products = {p.id:p for p in db.scalars(select(Product).where(
        Product.id.in_(ids), Product.customer_id == delivery.customer_id, Product.deleted_at.is_(None)))} if ids else {}
    for lines in groups:
        for line in lines:
            pid = line.get('component_product_id') or links.get(line.get('component_snapshot_id') or line.get('snapshot_id'))
            if pid or line.get('line_type') == 'component':
                line['specification'] = ''
            if pid in products:
                line['specification'] = delivery_specification(products[pid])
                line['customer_drawing_number'] = products[pid].customer_drawing_number or ''
    return items
