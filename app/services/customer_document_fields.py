"""Customer-facing identity, kept separate from production box configuration."""
from __future__ import annotations

import json
import re
from typing import Any
from functools import lru_cache
from pathlib import Path

FIELDS = (
    'customer_drawing_number', 'customer_category', 'customer_model',
)
# Retain validation of old snapshots, but never create new values for retired fields.
RETIRED_FIELDS = ('customer_product_name', 'customer_drawing_display')
LABELS = dict(zip(FIELDS, ('客户图号', '客户类别', '使用型番')))
SOURCE_KEYS = {
    'customer_drawing_number': 'C图号', 'customer_category': 'E箱型',
    'customer_model': 'B使用型番',
}


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


@lru_cache(maxsize=1)
def review_entries():
    path = Path(__file__).resolve().parents[1] / 'data' / 'customer_document_review_20260922.json'
    return json.loads(path.read_text(encoding='utf-8'))['entries']


def source_candidates(product) -> dict:
    """Read retained source evidence only; conflicting identities are never guessed."""
    values = {field: [] for field in FIELDS}
    sources = []
    text = str(_get(product, 'remark', '') or '')
    for match in re.finditer(r'【基础资料原始行20260810】(.*?)【基础资料原始行结束】', text, re.S):
        try:
            rows = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            evidence = row.get('字段口径')
            if not isinstance(evidence, dict):
                continue
            sources.append({'sheet': row.get('工作表'), 'row': row.get('Excel行')})
            for field, key in SOURCE_KEYS.items():
                value = evidence.get(key)
                if value is None:
                    continue
                value = str(value).strip()
                if value and value != '0' and value not in values[field]:
                    values[field].append(value)
    for entry in review_entries():
        if entry['field'] not in FIELDS:
            continue
        if entry['customer_id'] != _get(product, 'customer_id') or entry['code'] != _get(product, 'customer_material_code'):
            continue
        value = str(entry['value']).strip()
        if value and value not in values[entry['field']]:
            values[entry['field']].append(value)
        sources.append({key: entry[key] for key in ('file', 'sheet', 'row')})
    return {'values': values, 'sources': sources,
            'conflicts': [field for field, choices in values.items() if len(choices) > 1]}


def document_snapshot(product, *, basis: str | None = None) -> dict[str, Any]:
    evidence = source_candidates(product)
    resolved = {}
    references = []
    for field in FIELDS:
        value = _get(product, field)
        # NULL means not maintained. An explicit empty string means confirmed blank.
        if value is None and len(evidence['values'][field]) == 1:
            value = evidence['values'][field][0]
            references.append(field)
        resolved[field] = value
    required = {137: ('customer_drawing_number', 'customer_category'),
                138: ('customer_model', 'customer_drawing_number', 'customer_category'),
                136: ()}.get(_get(product, 'customer_id'), ())
    return {
        'schema_version': 1, 'product_id': _get(product, 'id'),
        'customer_material_code': _get(product, 'customer_material_code') or '',
        **resolved,
        'basis': basis or ('stored_source_reference' if references else 'product_fields'),
        'reference_fields': references,
        'conflicts': [field for field in evidence['conflicts'] if _get(product, field) is None],
        'missing_labels': [LABELS[field] for field in required if not str(resolved[field] or '').strip()],
    }


def encode_snapshot(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decode_snapshot(value: str | None) -> dict | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or decoded.get('schema_version') != 1:
            raise ValueError()
        if any(decoded.get(field) is not None and not isinstance(decoded[field], str)
               for field in (*FIELDS, *RETIRED_FIELDS, 'customer_material_code')):
            raise ValueError()
        return decoded
    except (ValueError, TypeError) as error:
        raise ValueError('客户送货资料快照损坏，请核对原单据') from error


def freeze_order_document(_mapper, connection, target):
    from app.models.product import Product
    if target.customer_document_snapshot_json is not None or not target.product_id:
        return
    row = connection.execute(Product.__table__.select().where(
        Product.id == target.product_id)).mappings().first()
    if row is not None:
        target.customer_document_snapshot_json = encode_snapshot(document_snapshot(dict(row)))


def freeze_delivery_document(_mapper, connection, target):
    from app.models.order import OrderItem
    from app.models.product import Product
    if target.customer_document_snapshot_json is not None:
        return
    from app.models.delivery import DeliveryItem
    previous = connection.execute(DeliveryItem.__table__.select().where(
        DeliveryItem.delivery_id == target.delivery_id,
        DeliveryItem.order_item_id == target.order_item_id,
        DeliveryItem.product_id == target.product_id,
        DeliveryItem.customer_po_snapshot == target.customer_po_snapshot,
        DeliveryItem.customer_document_snapshot_json.is_not(None),
    ).order_by(DeliveryItem.revision_number.desc(), DeliveryItem.id.desc())).mappings().first()
    if previous is not None:
        decode_snapshot(previous['customer_document_snapshot_json'])
        target.customer_document_snapshot_json = previous['customer_document_snapshot_json']
        return
    order_row = None
    if target.order_item_id:
        order_row = connection.execute(OrderItem.__table__.select().where(
            OrderItem.id == target.order_item_id)).mappings().first()
    if order_row and order_row['customer_document_snapshot_json'] is not None:
        decode_snapshot(order_row['customer_document_snapshot_json'])
        target.customer_document_snapshot_json = order_row['customer_document_snapshot_json']
        return
    product_id = target.product_id or (order_row['product_id'] if order_row else None)
    if product_id:
        row = connection.execute(Product.__table__.select().where(Product.id == product_id)).mappings().first()
        if row is not None:
            value = document_snapshot(dict(row))
            value['captured_at'] = 'delivery_creation'
            value['order_snapshot_missing'] = order_row is not None
            target.customer_document_snapshot_json = encode_snapshot(value)
