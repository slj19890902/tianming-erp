"""Explicitly approved historical reference costs; no inventory/payable writes.

Reports only use approved rows for still-missing source events. Real actual
facts win automatically. A changed source or increased quantity fails closed.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.audit import OperationLog
from app.models.material_cost_supplement import FinanceMaterialCostSupplement as Supplement
from app.models.order import OrderItem
from app.models.warehouse_inventory import InventoryLot
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceiptItem
from app.services.order_material_cost import estimate_order_item_material_cost, _bom_sources, _component
from app.services.audit_log import append_audit_event

ALGORITHM = "historical-material-reference-v1"
REASONS = {"estimate_only", "missing_purchase_lineage", "no_delivery_cost_source"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def positive(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def target_identity(gap):
    item = gap["item"]
    source = gap.get("source") or {}
    lot = source.get("lot")
    completion = source.get("completion")
    component = source.get("component")
    identity = dict(delivery_item_id=item.id, delivery_id=item.delivery_id,
                    product_id=item.product_id, order_item_id=item.order_item_id,
                    delivery_source_type=item.source_type, month=gap["month"],
                    source_kind=source.get("kind", "untraced_delivery"), source_id=source.get("id", item.id),
                    lot_id=getattr(lot, "id", None), lot_source_type=getattr(lot, "source_ref_type", None),
                    lot_source_id=getattr(lot, "source_ref_id", None), order_product_id=gap.get("order_product_id"),
                    lot_product_id=getattr(getattr(lot, "finished_detail", None), "product_id", None),
                    completion_id=getattr(completion, "id", None), component_id=getattr(component, "id", None),
                    component_product_id=getattr(component, "component_product_id", None))
    return identity, fingerprint(identity)


def applicable_amount(row, gap):
    if gap["reason"] not in REASONS:
        return None
    _, key = target_identity(gap)
    quantity = gap["quantity"]
    if row.target_fingerprint != key or quantity <= 0 or quantity > row.quantity_limit:
        return None
    return Decimal(row.unit_cost) * quantity


def _reference(db, gap):
    if gap["reason"] not in REASONS:
        return None, "实际成本待冻结或外币缺汇率，不采用参考价"
    source = gap.get("source") or {}
    lot = source.get("lot")
    item = gap["item"]
    if gap["quantity"] <= 0:
        return None, "有效数量为零，不创建成本"
    if source.get("kind") == "subkit":
        return None, "套件/部件需完整专属成本依据"
    if source.get("kind") == "bom_direct_completion":
        component = source.get("component")
        completion = source.get("completion")
        if not component or not completion or component.sales_order_item_id != item.order_item_id or completion.order_item_id != item.order_item_id:
            return None, "部件完工与原订单身份不一致"
        if completion.status != "posted" or component.snapshot_component_base_report_length_mm or component.snapshot_component_base_report_width_mm:
            return None, "部件完工状态或盖底片成本需单独核对"
        raw = {c.name: getattr(component, c.name) for c in component.__table__.columns}
        quantity_basis = positive(component.required_piece_quantity)
        if not quantity_basis:
            return None, "部件需求数量无效"
        pieces = int(component.snapshot_component_pieces_per_box or 1)
        if pieces < 1:
            return None, "部件拼片关系无效"
        raw["effective_required_piece_quantity"] = int(quantity_basis) * pieces
        calculated, missing = _component(db, **_bom_sources([raw])[0])
        if missing or not calculated:
            return None, "；".join(missing) or "部件材料依据不完整"
        unit = Decimal(calculated["estimated_material_cost"]) / quantity_basis
        return dict(reference_kind="bom_component_material_reference", unit_cost=unit,
                    evidence=dict(component_id=component.id, completion_id=completion.id,
                                  product_id=component.component_product_id, required_piece_quantity=quantity_basis,
                                  pieces_per_component=pieces, calculated=calculated,
                                  scope="仅本来源部件材料成本，不套用整套价格")), None
    if lot is not None:
        detail = lot.finished_detail
        if lot.inventory_type != "finished" or detail is None:
            return None, "非成品来源，不能套用整件材料成本"
        if item.product_id and detail.product_id != item.product_id:
            return None, "送货与批次产品身份不一致"
        unit = positive(lot.estimated_unit_cost_snapshot)
        if unit and lot.cost_snapshot_source in {"material_quote_area", "purchase_receipt_actual"}:
            try:
                evidence = json.loads(lot.cost_snapshot_detail_json or "{}")
            except ValueError:
                return None, "批次成本证据无法读取"
            if not evidence:
                return None, "批次单价缺少计算依据"
            currency = str(evidence.get("currency") or "").upper()
            if not currency and evidence.get("price_unit") in {"元/㎡", "元/平方米"}:
                currency = "CNY"
            if not currency and evidence.get("source_semi_inventory_lot_id"):
                origin = db.get(InventoryLot, evidence["source_semi_inventory_lot_id"])
                try:
                    origin_detail = json.loads(origin.cost_snapshot_detail_json or "{}") if origin else {}
                except ValueError:
                    origin_detail = {}
                if origin_detail.get("price_unit") in {"元/㎡", "元/平方米"}:
                    currency = "CNY"
                    evidence = {**evidence, "currency_source_semi_detail": origin_detail}
            if currency != "CNY":
                return None, "参考币种不明或非人民币，不能自动换算"
            return dict(reference_kind="lot_cost_snapshot", unit_cost=unit,
                        evidence=dict(lot_id=lot.id, product_id=detail.product_id,
                                      snapshot_source=lot.cost_snapshot_source, snapshot_at=lot.cost_snapshot_at,
                                      original_detail=evidence, scope="事后采用原批次单价，不认定历史实际采购价")), None
        if lot.source_ref_type == "external_packaging_receipt_item":
            receipt = db.get(ExternalPackagingReceiptItem, lot.source_ref_id)
            purchase = db.get(ExternalPackagingPurchaseItem, receipt.purchase_item_id) if receipt else None
            if not purchase or purchase.customer_product_id_snapshot != detail.product_id:
                return None, "外购收料与成品身份不一致"
            order_basis = positive(purchase.order_quantity_basis_snapshot)
            purchase_basis = positive(purchase.purchase_quantity_basis_snapshot)
            price = positive(purchase.unit_price)
            if not order_basis or not purchase_basis or not price or purchase.currency != "CNY":
                return None, "外购冻结价/币种/成品换算依据不完整"
            if purchase.tax_mode == "tax_exclusive":
                price *= 1 + Decimal(purchase.tax_rate)
            elif purchase.tax_mode != "tax_inclusive":
                return None, "外购税口径不完整"
            unit = price * purchase_basis / order_basis
            return dict(reference_kind="external_receipt_reference", unit_cost=unit,
                        evidence=dict(receipt_item_id=receipt.id, purchase_item_id=purchase.id,
                                      price_version_id=purchase.price_version_id, unit_price=purchase.unit_price,
                                      currency=purchase.currency, tax_mode=purchase.tax_mode, tax_rate=purchase.tax_rate,
                                      purchase_unit=purchase.purchase_unit, order_basis=order_basis, purchase_basis=purchase_basis,
                                      product_id=detail.product_id, received_quantity=receipt.received_quantity,
                                      scope="冻结采购含税单价按成品关系换算；运费不重复加入材料成本")), None
    order = db.get(OrderItem, item.order_item_id) if item.order_item_id else None
    if order is None or (item.product_id and order.product_id != item.product_id):
        return None, "缺少可靠的原订单产品关联"
    if lot and lot.finished_detail.product_id != order.product_id:
        return None, "订单与批次产品身份不一致"
    if order.combination_role == "set_parent" or order.supply_mode_snapshot == "mixed_bom":
        return None, "组合整套需核对完整部件，不能只用主片成本"
    estimate = estimate_order_item_material_cost(db, order)
    if estimate["material_cost_status"] != "calculated":
        return None, "；".join(estimate["material_cost_missing_items"]) or "订单材料成本不完整"
    if any(str(c.get("currency", "CNY")).upper() != "CNY" for c in estimate["material_cost_components"]):
        return None, "参考成本为外币，缺人工确认汇率"
    unit = positive(estimate["estimated_material_unit_cost"])
    if not unit:
        return None, "缺少有效材料单价"
    return dict(reference_kind="order_current_material_reference", unit_cost=unit,
                evidence=dict(order_item_id=order.id, product_id=order.product_id,
                              estimate=estimate, scope="原订单报料尺寸乘现有平方价，事后参考采用，非历史采购价")), None


def summarize(db: Session, gaps, total_lines: int):
    item_ids = {g["item"].id for g in gaps}
    rows = {r.target_fingerprint: r for r in db.scalars(select(Supplement).where(Supplement.delivery_item_id.in_(item_ids)))} if item_ids else {}
    amount = Decimal(0)
    used = []
    unresolved = {}
    for gap in gaps:
        identity, key = target_identity(gap)
        row = rows.get(key)
        cost = applicable_amount(row, gap) if row else None
        if cost is None:
            unresolved[gap["item"].id] = dict(delivery_item_id=gap["item"].id,
                delivery_number=gap["delivery_number"], reason="来源或数量变化，补充成本需复核" if row else "尚无已批准的补充材料成本")
        else:
            amount += cost
            used.append(dict(id=row.id, delivery_item_id=row.delivery_item_id, source_kind=row.source_kind,
                             delivery_number=gap["delivery_number"],
                             product_name=gap.get("product_name", ""),
                             source_id=row.source_id, quantity=gap["quantity"], unit_cost=row.unit_cost,
                             amount=cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), reference_kind=row.reference_kind,
                             batch_id=row.batch_id, adopted_at=row.created_at))
    return dict(supplemental_material_cost=amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                supplemental_source_count=len(used), management_uncovered_lines=len(unresolved),
                management_covered_lines=total_lines - len(unresolved), management_cost_ready=not unresolved,
                supplemental_details=used, management_missing_details=list(unresolved.values())[:20],
                supplement_note="补充材料成本包括已确认的入库批次成本及已批准的历史参考价；不改变供应商应付，真实采购成本齐全时不重复计入。")


def freeze_inventory_entry_cost(db, *, allocation, lot, operator_id, source_kind, quantity):
    """Atomically carry the entry price into the dispatch source's immutable cost."""
    from app.models.delivery import Delivery, DeliveryItem
    from app.services.inventory_valuation import frozen_cost, ALGORITHM as ENTRY_ALGORITHM
    unit, original = frozen_cost(lot, db)
    if unit is None or quantity <= 0:
        return None
    if operator_id is None:
        raise ValueError("库存成本结转缺少出库操作人")
    item = db.get(DeliveryItem, allocation.delivery_item_id)
    delivery = db.get(Delivery, item.delivery_id) if item else None
    if item is None or delivery is None or allocation.id is None:
        raise ValueError("库存成本结转缺少送货来源")
    if item.product_id and lot.finished_detail and item.product_id != lot.finished_detail.product_id:
        raise ValueError("出库产品与库存成本批次不一致")
    order = db.get(OrderItem, item.order_item_id) if item.order_item_id else None
    gap = dict(item=item, month=delivery.delivery_date.strftime("%Y-%m"),
        source=dict(kind=source_kind, id=allocation.id, lot=lot),
        order_product_id=order.product_id if order else None)
    identity, key = target_identity(gap)
    existing = db.scalar(select(Supplement).where(Supplement.target_fingerprint == key))
    if existing:
        if existing.quantity_limit < quantity or existing.unit_cost != unit:
            raise ValueError("出库来源数量或成本已变化，不能覆盖冻结成本")
        return existing
    evidence = dict(lot_id=lot.id, snapshot_source=lot.cost_snapshot_source,
        snapshot_at=lot.cost_snapshot_at, original_detail=original, unit_cost=str(unit))
    row = Supplement(delivery_item_id=item.id, inventory_lot_id=lot.id,
        month=gap["month"], source_kind=source_kind, source_id=allocation.id,
        target_fingerprint=key, target_json=canonical(identity), quantity_limit=quantity,
        unit_cost=unit, currency="CNY", reference_kind="confirmed_inventory_entry_cost",
        evidence_json=canonical(evidence), evidence_fingerprint=fingerprint(evidence),
        algorithm_version=ENTRY_ALGORITHM, batch_id=f"dispatch-entry:{source_kind}:{allocation.id}",
        reason="按已确认入库批次单价自动结转材料成本，不新增采购或应付", created_by=operator_id)
    db.add(row)
    db.flush()
    return row


def preview(db: Session, months):
    from app.services.material_cost_lineage import material_cost_coverage_report
    proposals, missing = [], []
    existing = {r.target_fingerprint: r for r in db.scalars(select(Supplement))}
    already = 0
    for month in sorted(set(months)):
        if month not in {"2026-08", "2026-09"}:
            raise ValueError("本次历史补齐仅限2026年8月和9月")
        gaps = []
        material_cost_coverage_report(db, month=month, _gap_collector=gaps, _apply_supplements=False)
        for gap in gaps:
            identity, key = target_identity(gap)
            if key in existing:
                if applicable_amount(existing[key], gap) is not None:
                    already += 1
                else:
                    missing.append(dict(**identity, reason="已有不可变补充记录与当前数量不符，需另行受控更正"))
                continue
            ref, error = _reference(db, gap)
            if error:
                missing.append(dict(**identity, reason=error))
                continue
            ref["unit_cost"] = str(ref["unit_cost"].quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
            proposals.append(dict(target=identity, target_fingerprint=key, quantity_limit=gap["quantity"],
                                  reference_kind=ref["reference_kind"], unit_cost=ref["unit_cost"],
                                  evidence=json.loads(canonical(ref["evidence"]))))
    payload = dict(algorithm=ALGORITHM, months=sorted(set(months)), proposals=proposals,
                   missing=missing, already_approved=already)
    payload["preview_fingerprint"] = fingerprint(payload)
    payload["proposed_amount"] = str(sum((Decimal(p["unit_cost"])*p["quantity_limit"] for p in proposals), Decimal(0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return payload


def adopt(db: Session, *, months, user, expected_preview: str, batch_id: str, reason: str):
    """Caller owns BEGIN IMMEDIATE and commit; audit and rows share transaction."""
    if not user or not user.is_active or user.role != "admin":
        raise PermissionError("仅管理员可执行已授权的历史成本补齐")
    if not reason.strip() or not batch_id.strip() or len(batch_id) > 64:
        raise ValueError("补齐批次和授权依据不能为空")
    prior = db.scalar(select(OperationLog).where(OperationLog.resource == "material_cost_supplement", OperationLog.batch_id == batch_id))
    if prior:
        data = json.loads(prior.details)
        if data["preview_fingerprint"] != expected_preview or data["months"] != sorted(set(months)) or data["reason"] != reason:
            raise ValueError("同一补齐批次不能使用不同预览")
        return {**data, "replayed": True}
    plan = preview(db, months)
    if plan["preview_fingerprint"] != expected_preview:
        raise ValueError("来源或价格已变化，请重新预览，未写入任何补充成本")
    for p in plan["proposals"]:
        target = p["target"]
        db.add(Supplement(delivery_item_id=target["delivery_item_id"], inventory_lot_id=target["lot_id"],
                          month=target["month"], source_kind=target["source_kind"], source_id=target["source_id"],
                          target_fingerprint=p["target_fingerprint"], target_json=canonical(target),
                          quantity_limit=p["quantity_limit"], unit_cost=Decimal(p["unit_cost"]), currency="CNY",
                          reference_kind=p["reference_kind"], evidence_json=canonical(p["evidence"]),
                          evidence_fingerprint=fingerprint(p["evidence"]), algorithm_version=ALGORITHM,
                          batch_id=batch_id, reason=reason, created_by=user.id))
    result = dict(preview_fingerprint=expected_preview, months=plan["months"], count=len(plan["proposals"]),
                  amount=plan["proposed_amount"], missing_count=len(plan["missing"]), batch_id=batch_id, reason=reason, replayed=False)
    append_audit_event(db, actor=user, event_category="business", result="success", source="script",
                       module_code="finance.cost", action_code="historical_material_supplement",
                       resource="material_cost_supplement", batch_id=batch_id,
                       description=reason, details=result)
    db.flush()
    return result
