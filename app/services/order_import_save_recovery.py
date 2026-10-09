"""Small, persistent order-save proofs; never infer a frozen order from live stock."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json

from fastapi import HTTPException


SCHEMA = "order-import-save-v1"
HEADERS = {"Cache-Control": "no-store", "X-Order-Save-Preserve": "1"}
TOKEN_KEYS = frozenset({"expected_actor_id", "mold_repair_confirmation_token",
    "preview_safety_token", "temp_drawing_token"})
PRICE_KEYS = frozenset({"unit_price", "requested_unit_price", "sale_unit_price", "sale_unit_price_no_tax", "cost_unit_price"})


def conflict(message="原保存证明不完整，请保留请求并核对原订单。"):
    return HTTPException(409, message, headers=HEADERS)


def echo(payload):
    result = payload.model_dump(mode="json", exclude_unset=True)
    if payload.readback_contract is None:
        for row in result.get("items") or []:
            row.pop("product_expected_version", None)
    return _filter(result, pricing_visible=True)


def _filter(value, *, pricing_visible):
    if isinstance(value, list):
        return [_filter(row, pricing_visible=pricing_visible) for row in value]
    if isinstance(value, dict):
        return {key: _filter(row, pricing_visible=pricing_visible)
            for key, row in value.items() if key not in TOKEN_KEYS
            and (pricing_visible or key not in PRICE_KEYS)}
    return value


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise conflict() from None
    if not result.is_finite():
        raise conflict()
    return result


def snapshot(response):
    """Only defined non-sensitive trace fields and explicit sales-price fields."""
    return {key: deepcopy(response.get(key)) for key in
        ("id", "order_number", "customer_id", "customer_po")} | {
        "items": [{key: deepcopy(row.get(key)) for key in
            ("id", "item_sequence", "client_line_id", "product_id", "quantity", "unit_price")}
            for row in response.get("items") or []]}


def source_snapshot(source, source_lines, current_order_number):
    """Source quantities/prices are persistent confirmations, not current order fields."""
    items = []
    for position, row in enumerate(source_lines, 1):
        if row.source_position != position or row.order_item_id is None:
            raise conflict()
        try:
            facts = json.loads(row.confirmation_summary_json)
            product_id = facts["confirmed_product_id"]
            quantity = facts["confirmed_quantity"]
            price = facts["confirmed_unit_price"]
        except (ValueError, TypeError, KeyError):
            raise conflict() from None
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise conflict()
        if number(quantity) <= 0 or number(price) < 0:
            raise conflict()
        items.append(dict(id=row.order_item_id, item_sequence=position,
            client_line_id=facts.get("client_line_id"), product_id=product_id,
            quantity=quantity, unit_price=price))
    return dict(id=source.order_id, order_number=current_order_number,
        customer_id=source.customer_id, customer_po=source.customer_po_snapshot, items=items)


def build(*, request_echo, frozen_order, key, digest, actor_id, source=None, source_replay=False):
    requests = request_echo.get("items") or []
    items = frozen_order.get("items") or []
    if len(requests) != len(items) or not items:
        raise conflict()
    seen = set()
    lines = []
    for request, item in zip(requests, items, strict=True):
        if item.get("id") in seen or not isinstance(item.get("id"), int):
            raise conflict()
        seen.add(item["id"])
        if number(request.get("quantity")) != number(item.get("quantity")):
            raise conflict()
        requested_price = number(request.get("unit_price"))
        saved_price = number(item.get("unit_price"))
        scale = Decimal("0.000001")
        if (saved_price.quantize(scale) != saved_price
            or abs(saved_price - requested_price) > scale / 2):
            raise conflict()
        product = request.get("product_id")
        is_new = bool(request.get("is_new_product") or request.get("manual_size_entry"))
        if product is not None and product != item.get("product_id"):
            raise conflict()
        if product is None and not is_new:
            raise conflict()
        lines.append(dict(request_client_line_id=request.get("client_line_id"),
            original_client_line_id=item.get("client_line_id"), order_item_id=item["id"],
            request_product_id=product, is_new_product=is_new,
            product_id=item.get("product_id"), quantity=item.get("quantity"),
            requested_unit_price=request.get("unit_price"), unit_price=item.get("unit_price"),
            unit_price_scale=6, price_normalized=saved_price != requested_price))
    return dict(schema=SCHEMA, request_key=key, actor_id=actor_id,
        payload_digest=digest, proof_status="complete", source_replay=source_replay,
        request=deepcopy(request_echo), order=deepcopy(frozen_order), lines=lines,
        source=source, line_count=len(lines), request_match=True, pricing_visible=True)


def public(proof, *, pricing_visible, matched):
    result = _filter(deepcopy(proof), pricing_visible=pricing_visible)
    result["pricing_visible"] = pricing_visible
    result["request_match"] = bool(matched and proof.get("proof_status") == "complete")
    if not matched:
        result["proof_status"] = "legacy" if pricing_visible else "restricted"
        result["request"] = None
    return result


def legacy(record, *, key, actor_id, pricing_visible):
    return public(dict(schema=SCHEMA, request_key=key, actor_id=actor_id,
        payload_digest=record.get("digest"), proof_status="legacy", source_replay=False,
        request=None, order=snapshot(record["response"]), lines=[], source=None,
        line_count=len(record["response"].get("items") or []), request_match=False),
        pricing_visible=pricing_visible, matched=False)
