"""Optional order-create evidence frozen in the existing transactional replay log."""
from fastapi import HTTPException


def capture_source_lines(payload, products):
    if payload.readback_contract != "a01-v1":
        return None
    if not payload.idempotency_key:
        raise HTTPException(422, "保存证明必须提供幂等标识")
    seen = set()
    lines = []
    for index, request in enumerate(payload.items, start=1):
        product = products[index]
        identity = (request.client_line_id or "").strip()
        if not identity or identity in seen or request.product_id != product.id:
            raise HTTPException(422, "保存证明须使用独立行标识和已登记常用箱")
        seen.add(identity)
        if request.product_expected_version is None:
            raise HTTPException(422, "保存证明必须提供常用箱版本")
        if request.product_expected_version != product.version:
            raise HTTPException(409, {"code": "ORDER_PRODUCT_VERSION_CONFLICT", "message": "常用箱版本已变化，请刷新后确认"})
        default = (product.production_notes or "").strip() or None
        expected = None if product.supply_mode == "external_purchase" else (request.production_notes or "").strip() or default
        lines.append(dict(client_line_id=identity, product_id=product.id,
            customer_id=product.customer_id, product_version=product.version,
            supply_mode=product.supply_mode, default_production_notes=default,
            expected_production_notes=expected, item_sequence=index))
    return lines


def attach_frozen_proof(response, source_lines):
    if source_lines is None:
        return
    if len(response["items"]) != len(source_lines):
        raise HTTPException(409, "保存来源与明细数量不一致")
    by_identity = {row.get("client_line_id"): row for row in response["items"]}
    if len(by_identity) != len(source_lines):
        raise HTTPException(409, "保存来源行标识不完整")
    lines = []
    for source in source_lines:
        item = by_identity.get(source["client_line_id"])
        if item is None or item["product_id"] != source["product_id"] or item["item_sequence"] != source["item_sequence"]:
            raise HTTPException(409, "保存来源身份不一致")
        lines.append({**source, "order_item_id": item["id"], "quantity": str(item["quantity"])})
    response["create_readback"] = dict(schema="a01-v1", order_id=response["id"], customer_id=response["customer_id"], lines=lines)


def attach_replay_evidence(response, recorded):
    proof = recorded.get("create_readback")
    if proof is None:
        raise HTTPException(409, "此历史保存没有来源证明，请核对原订单，不可重复创建")
    if proof.get("order_id") != response["id"] or proof.get("customer_id") != response["customer_id"]:
        raise HTTPException(409, "保存来源与当前订单身份不一致")
    identities = {row["order_item_id"]: row["client_line_id"] for row in proof["lines"]}
    for item in response["items"]:
        item["client_line_id"] = identities.get(item["id"])
    response["create_readback"] = proof
