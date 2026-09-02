from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from threading import Lock

from sqlalchemy.orm import Session

from app.models.requisition import Requisition
from app.models.stock_replenishment import StockReplenishmentOrder
from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.services.requisition_production_print import (
    build_composite_requisition_production_package,
    build_stock_replenishment_production_package,
    build_supplier_requisition_production_package,
    production_print_card_fingerprint,
    production_print_card_task_versions,
)


class ProductionPrintBatchError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        invalid_items: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.invalid_items = invalid_items or []


_PRODUCTION_PRINT_BATCH_LOCK = Lock()


def production_print_batch_guard():
    """Serialize audit-backed idempotency in the supported single worker."""

    _PRODUCTION_PRINT_BATCH_LOCK.acquire()
    try:
        yield
    finally:
        _PRODUCTION_PRINT_BATCH_LOCK.release()


def canonical_batch_items(items: list[dict]) -> list[dict]:
    canonical: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for raw in items:
        source_type = str(raw.get("source_type") or "supplier_order").strip()
        if source_type not in {
            "supplier_order",
            "composite_bom_requisition",
            "stock_replenishment",
        }:
            raise ProductionPrintBatchError(
                "批量打印任务来源无效，请刷新后重新勾选",
                status_code=422,
            )
        supplier_order_id = int(raw.get("supplier_order_id") or 0)
        document_id = int(raw.get("document_id") or supplier_order_id or 0)
        source_identity = str(raw.get("source_identity") or "").strip()
        fingerprint = str(raw.get("selection_fingerprint") or "").strip().lower()
        if (
            document_id <= 0
            or (source_type == "supplier_order" and supplier_order_id <= 0)
            or not source_identity
            or len(fingerprint) != 64
        ):
            raise ProductionPrintBatchError(
                "批量打印任务身份或纸面指纹无效，请刷新后重新勾选",
                status_code=422,
            )
        identity = (source_type, document_id, source_identity)
        if identity in seen:
            raise ProductionPrintBatchError(
                "同一生产任务不能重复加入批量打印",
                status_code=422,
            )
        seen.add(identity)
        versions = sorted(
            {
                (int(row.get("task_id") or 0), int(row.get("version") or 0))
                for row in raw.get("task_versions") or []
            }
        )
        if any(task_id <= 0 or version <= 0 for task_id, version in versions):
            raise ProductionPrintBatchError(
                "生产任务版本不完整，请刷新后重新勾选",
                status_code=422,
            )
        if source_type == "stock_replenishment" and versions:
            raise ProductionPrintBatchError(
                "库存补库计划不能冒充订单生产任务",
                status_code=422,
            )
        if source_type != "stock_replenishment" and not versions:
            raise ProductionPrintBatchError(
                "生产任务版本不完整，请刷新后重新勾选",
                status_code=422,
            )
        canonical_item = {
            "source_identity": source_identity,
            "selection_fingerprint": fingerprint,
            "task_versions": [
                {"task_id": task_id, "version": version}
                for task_id, version in versions
            ],
        }
        if source_type == "supplier_order":
            # Keep the original canonical shape so previously prepared supplier
            # batches retain the same request hash and remain replayable.
            canonical_item = {
                "supplier_order_id": supplier_order_id,
                **canonical_item,
            }
        else:
            canonical_item = {
                "source_type": source_type,
                "document_id": document_id,
                **canonical_item,
            }
        canonical.append(canonical_item)
    if not canonical:
        raise ProductionPrintBatchError("请至少勾选一项待来料生产任务", status_code=422)
    if len(canonical) > 20:
        raise ProductionPrintBatchError("一次最多打印 20 项生产任务", status_code=422)
    return canonical


def production_print_batch_request_hash(items: list[dict]) -> str:
    return sha256(
        json.dumps(
            canonical_batch_items(items),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def production_print_batch_id(idempotency_key: str) -> str:
    key = str(idempotency_key or "").strip()
    if not key:
        raise ProductionPrintBatchError("批量打印幂等键不能为空", status_code=422)
    return sha256(f"production-print-batch:{key}".encode("utf-8")).hexdigest()


def production_print_batch_pages(cards: list[dict]) -> list[dict]:
    pages = [
        {
            "page_number": index // 2 + 1,
            "full_page": False,
            "top": cards[index],
            "bottom": cards[index + 1] if index + 1 < len(cards) else None,
        }
        for index in range(0, len(cards), 2)
    ]
    return pages


def build_selected_production_print_package(
    db: Session,
    *,
    selections: list[dict],
    orders: dict[int, SupplierRequisitionOrder],
    composite_requisitions: dict[int, Requisition] | None = None,
    stock_replenishments: dict[int, StockReplenishmentOrder] | None = None,
    batch_id: str,
) -> dict:
    canonical = canonical_batch_items(selections)
    composite_requisitions = composite_requisitions or {}
    stock_replenishments = stock_replenishments or {}
    packages: dict[tuple[str, int], dict] = {}
    invalid: list[dict] = []
    selected_cards: list[dict] = []

    for item in canonical:
        source_type = str(item.get("source_type") or "supplier_order")
        document_id = int(
            item.get("document_id") or item.get("supplier_order_id") or 0
        )
        source_identity = item["source_identity"]
        source = (
            orders.get(document_id)
            if source_type == "supplier_order"
            else stock_replenishments.get(document_id)
            if source_type == "stock_replenishment"
            else composite_requisitions.get(document_id)
        )
        expected_statuses = (
            {"confirmed"}
            if source_type == "supplier_order"
            else {"confirmed", "partially_stocked", "stocked"}
            if source_type == "stock_replenishment"
            else {"已报料"}
        )
        identity_fields = {
            "supplier_order_id": document_id,
        } if source_type == "supplier_order" else {
            "source_type": source_type,
            "document_id": document_id,
        }
        if source is None or source.status not in expected_statuses:
            invalid.append(
                {
                    **identity_fields,
                    "source_identity": source_identity,
                    "reason": (
                        "库存补库单已撤销、作废或不存在"
                        if source_type == "stock_replenishment"
                        else "报料单已撤销、作废或不存在"
                    ),
                }
            )
            continue
        package_key = (source_type, document_id)
        package = packages.get(package_key)
        if package is None:
            try:
                package = (
                    build_supplier_requisition_production_package(db, source)
                    if source_type == "supplier_order"
                    else build_stock_replenishment_production_package(db, source)
                    if source_type == "stock_replenishment"
                    else build_composite_requisition_production_package(db, source)
                )
            except ValueError as error:
                invalid.append(
                    {
                        **identity_fields,
                        "source_identity": source_identity,
                        "reason": str(error),
                    }
                )
                continue
            packages[package_key] = package
        card = next(
            (
                row
                for row in package.get("cards") or []
                if str(row.get("source_identity") or "") == source_identity
            ),
            None,
        )
        if card is None:
            invalid.append(
                {
                    **identity_fields,
                    "source_identity": source_identity,
                    "reason": "任务已不在当前正式报料单中",
                }
            )
            continue
        reasons = list(card.get("selection_block_reasons") or [])
        if not bool(card.get("selection_eligible")):
            invalid.append(
                {
                    **identity_fields,
                    "source_identity": source_identity,
                    "reason": "；".join(reasons) or "任务当前不可批量打印",
                }
            )
            continue
        current_versions = production_print_card_task_versions(card)
        if current_versions != item["task_versions"]:
            invalid.append(
                {
                    **identity_fields,
                    "source_identity": source_identity,
                    "reason": (
                        "库存补库计划身份已变化"
                        if source_type == "stock_replenishment"
                        else "生产任务版本已变化"
                    ),
                }
            )
            continue
        current_fingerprint = production_print_card_fingerprint(card)
        if current_fingerprint != item["selection_fingerprint"]:
            invalid.append(
                {
                    **identity_fields,
                    "source_identity": source_identity,
                    "reason": "纸面内容已变化",
                }
            )
            continue
        frozen = deepcopy(card)
        if source_type == "supplier_order":
            frozen["supplier_order_id"] = source.id
            frozen["supplier_order_number"] = source.order_number
        elif source_type == "stock_replenishment":
            frozen["source_type"] = source_type
            frozen["stock_replenishment_order_id"] = source.id
            frozen["supplier_order_number"] = source.order_number
        else:
            frozen["source_type"] = source_type
            frozen["material_requisition_id"] = source.id
            frozen["supplier_order_number"] = source.requisition_number
        selected_cards.append(frozen)

    if invalid:
        raise ProductionPrintBatchError(
            "所选任务中有失效项，已整体停止生成；请移除失效项后重新确认",
            invalid_items=invalid,
        )

    pages = production_print_batch_pages(selected_cards)
    package_fingerprint = sha256(
        json.dumps(
            {
                "batch_id": batch_id,
                "selections": canonical,
                "cards": selected_cards,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "batch_id": batch_id,
        "supplier_order_id": None,
        "supplier_order_number": "批量待来料任务单",
        "status": "prepared",
        "status_label": "待来料",
        "plan_fingerprint": package_fingerprint,
        "package_fingerprint": package_fingerprint,
        "card_count": len(selected_cards),
        "page_count": len(pages),
        "review_required": any(
            bool(card.get("review_required")) for card in selected_cards
        ),
        "review_messages": sorted(
            {
                str(message)
                for card in selected_cards
                for message in card.get("review_messages") or []
                if str(message).strip()
            }
        ),
        "layout_overflow": False,
        "printable": True,
        "production_label_task_count": 0,
        "production_label_count": 0,
        "cards": selected_cards,
        "pages": pages,
    }
