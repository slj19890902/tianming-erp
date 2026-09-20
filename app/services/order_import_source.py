from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.order import Order, OrderItem
from app.models.order_import_source import OrderImportSource, OrderImportSourceLine


SOURCE_HASH_LENGTH = 64


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    payload = json.dumps(
        _json_value(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_source_name(value: Any) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character >= " " and character != "\x7f")
    return name.strip()[:255] or "PDF来源"


def _line_values(item: Any) -> dict[str, Any]:
    getter = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
    return {
        "product_code": getter("product_code") or getter("raw_product_code"),
        "product_name": getter("product_name") or getter("raw_product_name"),
        "specification": (
            getter("specification")
            or getter("raw_spec_model")
            or getter("spec_model")
            or getter("spec")
        ),
        "quantity": getter("quantity"),
        "unit_price": getter("unit_price"),
    }


def signed_source_lines(draft: dict) -> list[dict[str, Any]]:
    """Build compact line identities that become part of the signed preview token."""

    result: list[dict[str, Any]] = []
    for position, item in enumerate(draft.get("items") or [], start=1):
        if not isinstance(item, dict):
            item = {}
        page = item.get("source_page") or item.get("page")
        try:
            page = int(page) if page is not None else None
        except (TypeError, ValueError):
            page = None
        line_label = item.get("line_no")
        line_label = str(line_label).strip()[:80] if line_label is not None else None
        raw_lines = item.get("raw_lines")
        result.append(
            {
                "position": position,
                "source_page": page if page and page > 0 else None,
                "source_line_label": line_label or None,
                "raw_line_hash": _digest(raw_lines) if raw_lines else None,
                "recognized_line_hash": _digest(_line_values(item)),
            }
        )
    return result


def payload_hash(payload: Any) -> str:
    values = payload.model_dump(
        mode="json",
        exclude={
            "idempotency_key",
            "email_attachment_id",
            "pdf_import_confirmation",
            "import_integrity_status",
            "import_integrity_errors",
            "mold_repair_confirmation_token",
        },
    )
    for item in values.get("items") or []:
        item.pop("client_line_id", None)
    return _digest(values)


@dataclass(frozen=True)
class ImportSourceContext:
    source_kind: str
    source_key: str
    source_hash: str
    source_name: str
    customer_id: int
    customer_po: str | None
    payload_hash: str
    email_attachment_id: int | None
    claims: dict[str, Any]
    signed_lines: list[dict[str, Any]]


def prepare(
    db: Session,
    payload: Any,
    user: Any,
    claims: dict[str, Any],
    email_context: dict[str, Any] | None,
) -> tuple[ImportSourceContext | None, OrderImportSource | None]:
    del user
    if not claims:
        return None, None

    source_hash = str(claims.get("source_hash") or "").strip().casefold()
    if len(source_hash) != SOURCE_HASH_LENGTH or any(
        character not in "0123456789abcdef" for character in source_hash
    ):
        raise HTTPException(409, "PDF 来源哈希无效，请从原文件重新预览")

    signed_lines = claims.get("source_lines")
    if not isinstance(signed_lines, list) or len(signed_lines) != len(payload.items or []):
        raise HTTPException(
            409,
            "PDF 来源明细与当前草稿不一致，请从原文件重新预览并核对明细",
        )
    if [line.get("position") for line in signed_lines if isinstance(line, dict)] != list(
        range(1, len(signed_lines) + 1)
    ):
        raise HTTPException(409, "PDF 来源行身份无效，请从原文件重新预览")

    attachment_id = (
        int(email_context["attachment_id"])
        if email_context is not None
        else None
    )
    source_kind = "email_attachment" if attachment_id is not None else "pdf_upload"
    source_key = str(attachment_id) if attachment_id is not None else source_hash
    digest = payload_hash(payload)
    context = ImportSourceContext(
        source_kind=source_kind,
        source_key=source_key,
        source_hash=source_hash,
        source_name=_safe_source_name(claims.get("source_name")),
        customer_id=int(payload.customer_id),
        customer_po=(payload.customer_po or "").strip() or None,
        payload_hash=digest,
        email_attachment_id=attachment_id,
        claims=claims,
        signed_lines=signed_lines,
    )
    existing = db.scalar(
        select(OrderImportSource)
        .options(selectinload(OrderImportSource.lines))
        .where(
            OrderImportSource.source_kind == source_kind,
            OrderImportSource.source_key == source_key,
        )
    )
    if existing is None:
        return context, None
    if existing.customer_id != context.customer_id:
        raise HTTPException(409, "该 PDF 来源已归属其他客户，不能重新建单")
    if existing.payload_hash != digest:
        raise HTTPException(
            409,
            "该 PDF 来源已建立订单，本次明细或处理方式与原保存不一致；请查看原订单处理改单",
        )
    if existing.order_id is None or db.get(Order, existing.order_id) is None:
        raise HTTPException(409, "该 PDF 来源的原订单已不存在，请人工核对来源记录")
    return context, existing


def replay_client_line_ids(source: OrderImportSource) -> dict[int, str | None]:
    result: dict[int, str | None] = {}
    for line in source.lines:
        if line.order_item_id is None:
            continue
        try:
            summary = json.loads(line.confirmation_summary_json)
        except (TypeError, ValueError):
            summary = {}
        result[line.order_item_id] = summary.get("client_line_id")
    return result


def audit_summary(db: Session, source: OrderImportSource | None) -> dict[str, Any] | None:
    if source is None:
        return None
    rows = list(
        db.scalars(
            select(OrderImportSourceLine)
            .where(OrderImportSourceLine.source_id == source.id)
            .order_by(OrderImportSourceLine.source_position)
        )
    )
    mappings: list[dict[str, Any]] = []
    for row in rows:
        try:
            summary = json.loads(row.confirmation_summary_json)
        except (TypeError, ValueError):
            summary = {}
        mappings.append(
            {
                "source_position": row.source_position,
                "source_page": row.source_page,
                "source_line_label": row.source_line_label,
                "order_item_id": row.order_item_id,
                "manual_revision": bool(summary.get("manual_revision")),
            }
        )
    return {
        "source_id": source.id,
        "source_kind": source.source_kind,
        "line_count": len(mappings),
        "manual_revision_count": sum(
            1 for mapping in mappings if mapping["manual_revision"]
        ),
        "line_mappings": mappings,
    }


def attach(
    db: Session,
    context: ImportSourceContext | None,
    order: Order,
    created_items: list[OrderItem],
    payload_items: list[Any],
    *,
    actor_id: int | None,
    override_reasons: list[str],
) -> OrderImportSource | None:
    if context is None:
        return None
    summary = {
        "recognition_status": context.claims.get("recognition_status"),
        "customer_route_status": context.claims.get("customer_route_status"),
        "customer_match_status": context.claims.get("customer_match_status"),
        "integrity_status": context.claims.get("integrity_status"),
        "override_reasons": list(override_reasons),
        "line_count": len(context.signed_lines),
    }
    source = OrderImportSource(
        source_kind=context.source_kind,
        source_key=context.source_key,
        source_hash=context.source_hash,
        source_name_snapshot=context.source_name,
        customer_id=context.customer_id,
        customer_po_snapshot=context.customer_po,
        order_id=order.id,
        email_attachment_id=context.email_attachment_id,
        payload_hash=context.payload_hash,
        confirmation_summary_json=json.dumps(summary, ensure_ascii=False),
        created_by=actor_id,
    )
    db.add(source)
    db.flush()
    for signed, order_item, submitted in zip(
        context.signed_lines,
        created_items,
        payload_items,
        strict=True,
    ):
        submitted_hash = _digest(_line_values(submitted))
        recognized_hash = str(signed["recognized_line_hash"])
        line_summary = {
            "client_line_id": (getattr(submitted, "client_line_id", None) or "").strip() or None,
            "manual_revision": submitted_hash != recognized_hash,
            "confirmed_product_id": getattr(submitted, "product_id", None),
            "confirmed_quantity": getattr(submitted, "quantity", None),
            "confirmed_unit_price": _json_value(getattr(submitted, "unit_price", None)),
        }
        db.add(
            OrderImportSourceLine(
                source_id=source.id,
                order_item_id=order_item.id,
                source_position=int(signed["position"]),
                source_page=signed.get("source_page"),
                source_line_label=signed.get("source_line_label"),
                raw_line_hash=signed.get("raw_line_hash"),
                recognized_line_hash=recognized_hash,
                submitted_line_hash=submitted_hash,
                confirmation_summary_json=json.dumps(line_summary, ensure_ascii=False),
            )
        )
    db.flush()
    return source
