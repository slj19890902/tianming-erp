from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.product import Product
from app.models.xinzhen_carton_marking import (
    XinzhenCartonMarkingImportBatch,
    XinzhenCartonMarkingOrder,
    XinzhenCommonBoxRule,
    XinzhenPrintChangeoverStep,
    XinzhenPrintChangeoverStepItem,
    XinzhenPrintLayout,
    XinzhenPrintLayoutSlotValue,
    XinzhenResinTemplate,
    XinzhenResinTemplateSlot,
    XinzhenRubberTypeBlock,
)

DEFAULT_TEMPLATE_CODE = "XZ-HS-01"
DEFAULT_TEMPLATE_NAME = "新振箱唛树脂主版"
DEFAULT_TEMPLATE_SLOTS: tuple[tuple[str, str, str, str], ...] = (
    ("V1", "Customer Name", "customer_mark_name", "CUSTOMER_NAME"),
    ("V2", "Vendor Style#", "vendor_style_no", "STYLE"),
    ("V3", "Color", "color", "COLOR"),
    ("V4", "Size", "product_size", "SIZE"),
    ("V5", "Units", "units_per_carton", "UNITS"),
    ("V6", "Carton No.", "carton_no_range_text", "CARTON_NO"),
    ("V7", "Carton Qty", "carton_qty", "CARTON_QTY"),
    ("V8", "PO", "external_po_no", "PO"),
)
READY = "ready"
NEEDS_REVIEW = "needs_review"
ORDER_PENDING_RECEIPT = "pending_receipt"


@dataclass
class TemplateContext:
    template_id: int | None
    template_code: str | None
    template_name: str
    slots: list[dict[str, Any]]


@dataclass
class ImportAnalysis:
    customer: Customer
    source_file_name: str
    source_file_hash: str
    template: TemplateContext
    external_po_no: str | None
    header_total_carton_qty: int | None
    layout_total_carton_qty: int
    parsed_layout_count: int
    mismatch_count: int
    missing_common_box_count: int
    missing_rubber_block_count: int
    generated_changeover_step_count: int
    import_status: str
    order_status: str
    layouts: list[dict[str, Any]]
    slot_values: list[dict[str, Any]]
    changeover_steps: list[dict[str, Any]]
    changeover_step_items: list[dict[str, Any]]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_key(value: Any) -> str:
    return _normalize_text(value).casefold()


def _coerce_int(value: Any) -> int | None:
    text = _normalize_text(value)
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return None


def _parse_range_text(text: str | None) -> tuple[int | None, int | None, int | None]:
    raw = _normalize_text(text)
    if not raw:
        return None, None, None
    match = re.search(r"#\s*(\d+)\D+#?\s*(\d+)", raw)
    if not match:
        return None, None, None
    start = int(match.group(1))
    end = int(match.group(2))
    return start, end, end - start + 1


def _parse_summary_text(text: str | None) -> tuple[int | None, int | None]:
    raw = _normalize_text(text)
    if not raw:
        return None, None
    match = re.search(r"(\d+)\D*=\s*(\d+)", raw)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _normalize_dimension_text(text: str | None) -> str | None:
    raw = _normalize_text(text)
    if not raw:
        return None
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*[*xX脳]\s*(\d+(?:\.\d+)?)\s*[*xX脳]\s*(\d+(?:\.\d+)?)\s*cm",
        raw,
    )
    if not match:
        return None
    return f"{match.group(1)}*{match.group(2)}*{match.group(3)}cm"


def _hash_layout(parts: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update("|".join(parts).encode("utf-8"))
    return digest.hexdigest()


class XinzhenCartonMarkingImportService:
    def __init__(self, db: Session):
        self.db = db

    def import_parsed_json(
        self,
        *,
        customer_id: int,
        source_file_name: str,
        source_file_hash: str,
        parsed_json: Any,
        mode: str,
        user_id: int | None,
    ) -> dict[str, Any]:
        normalized_mode = _normalize_key(mode)
        if normalized_mode not in {"dry_run", "apply"}:
            raise ValueError("mode must be dry_run or apply")
        customer = self._require_customer(customer_id)
        template = self._resolve_template(
            customer_id=customer.id,
            persist_defaults=normalized_mode == "apply",
        )
        analysis = self._analyze(
            customer=customer,
            template=template,
            source_file_name=_normalize_text(source_file_name),
            source_file_hash=_normalize_text(source_file_hash),
            parsed_json=parsed_json,
        )
        if normalized_mode == "dry_run":
            return self._serialize_preview(analysis, mode="dry_run")
        batch = self._persist_analysis(analysis, user_id=user_id)
        self.db.commit()
        return self.get_import_batch_detail(batch.id)

    def get_import_batch_detail(self, batch_id: int) -> dict[str, Any]:
        batch = self.db.get(XinzhenCartonMarkingImportBatch, batch_id)
        if batch is None:
            raise ValueError("新振箱唛导入批次不存在")
        return self._serialize_batch(batch)

    def get_mobile_view(self, batch_id: int) -> dict[str, Any]:
        batch = self.db.get(XinzhenCartonMarkingImportBatch, batch_id)
        if batch is None:
            raise ValueError("新振箱唛导入批次不存在")
        serialized = self._serialize_batch(batch)
        return serialized["mobile_read_model"]

    def _require_customer(self, customer_id: int) -> Customer:
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            raise ValueError("customer_id does not exist")
        return customer

    def _default_template_slots(self, template_id: int | None) -> list[dict[str, Any]]:
        slots: list[dict[str, Any]] = []
        for display_order, (slot_code, slot_name, field_name, block_type) in enumerate(
            DEFAULT_TEMPLATE_SLOTS,
            start=1,
        ):
            slots.append(
                {
                    "template_id": template_id,
                    "slot_code": slot_code,
                    "slot_name": slot_name,
                    "field_name": field_name,
                    "block_type": block_type,
                    "display_order": display_order,
                    "is_variable": True,
                }
            )
        return slots

    def _resolve_template(
        self,
        *,
        customer_id: int,
        persist_defaults: bool,
    ) -> TemplateContext:
        template = self.db.scalar(
            select(XinzhenResinTemplate)
            .where(
                XinzhenResinTemplate.customer_id == customer_id,
                XinzhenResinTemplate.is_active.is_(True),
            )
            .order_by(XinzhenResinTemplate.id.desc())
        )
        slots: list[dict[str, Any]] = []
        if template is not None:
            slot_rows = self.db.scalars(
                select(XinzhenResinTemplateSlot)
                .where(XinzhenResinTemplateSlot.template_id == template.id)
                .order_by(XinzhenResinTemplateSlot.display_order)
            ).all()
            slots = [
                {
                    "template_id": template.id,
                    "slot_code": row.slot_code,
                    "slot_name": row.slot_name,
                    "field_name": row.field_name,
                    "block_type": row.block_type,
                    "display_order": row.display_order,
                    "is_variable": row.is_variable,
                }
                for row in slot_rows
            ]
        if template is not None and slots:
            return TemplateContext(
                template_id=template.id,
                template_code=template.template_code,
                template_name=template.template_name,
                slots=slots,
            )
        if not persist_defaults:
            return TemplateContext(
                template_id=template.id if template is not None else None,
                template_code=template.template_code if template is not None else DEFAULT_TEMPLATE_CODE,
                template_name=template.template_name if template is not None else DEFAULT_TEMPLATE_NAME,
                slots=self._default_template_slots(template.id if template is not None else None),
            )
        if template is None:
            template = XinzhenResinTemplate(
                customer_id=customer_id,
                template_code=DEFAULT_TEMPLATE_CODE,
                template_name=DEFAULT_TEMPLATE_NAME,
                is_active=True,
            )
            self.db.add(template)
            self.db.flush()
        if not slots:
            for slot in self._default_template_slots(template.id):
                self.db.add(
                    XinzhenResinTemplateSlot(
                        template_id=template.id,
                        slot_code=slot["slot_code"],
                        slot_name=slot["slot_name"],
                        field_name=slot["field_name"],
                        block_type=slot["block_type"],
                        display_order=slot["display_order"],
                        is_variable=slot["is_variable"],
                    )
                )
            self.db.flush()
            slots = self._default_template_slots(template.id)
        return TemplateContext(
            template_id=template.id,
            template_code=template.template_code,
            template_name=template.template_name,
            slots=slots,
        )

    def _analyze(
        self,
        *,
        customer: Customer,
        template: TemplateContext,
        source_file_name: str,
        source_file_hash: str,
        parsed_json: Any,
    ) -> ImportAnalysis:
        workbook = self._get_workbook(parsed_json)
        extracted_layouts, header_totals = self._extract_raw_layouts(workbook)
        if not extracted_layouts:
            raise ValueError("No layouts found in parsed_json")
        common_box_rules = self._load_common_box_rules(customer.id)
        rubber_index = self._load_rubber_block_index(customer.id)
        preview_layouts = [
            self._build_layout_preview(
                source_file_name=source_file_name,
                template=template,
                customer_id=customer.id,
                common_box_rules=common_box_rules,
                layout_index=index,
                parsed_layout=parsed_layout,
            )
            for index, parsed_layout in enumerate(extracted_layouts, start=1)
        ]
        sorted_layouts = self._sort_layouts(preview_layouts)
        slot_values, slot_values_by_layout = self._build_slot_values(
            customer_id=customer.id,
            layouts=sorted_layouts,
            slots=template.slots,
            rubber_index=rubber_index,
        )
        changeover_steps, changeover_step_items = self._build_changeovers(
            layouts=sorted_layouts,
            slot_values_by_layout=slot_values_by_layout,
        )
        unique_header_totals = sorted({value for value in header_totals if value is not None})
        header_total = unique_header_totals[0] if unique_header_totals else None
        layout_total = sum(int(layout["carton_qty"] or 0) for layout in sorted_layouts)
        mismatch_count = 0
        if len(unique_header_totals) > 1:
            mismatch_count += 1
        elif header_total is not None and int(header_total) != int(layout_total):
            mismatch_count += 1
        parse_review_count = sum(
            1 for layout in sorted_layouts if layout["parse_status"] != "ok"
        )
        missing_common_box_count = sum(
            1 for layout in sorted_layouts if layout["box_match_status"] == "missing"
        )
        missing_rubber_block_count = sum(
            1 for slot in slot_values if slot["match_status"] == "missing"
        )
        missing_changeover_count = sum(
            1 for item in changeover_step_items if item["confirm_status"] == "missing"
        )
        import_status = READY
        if any(
            (
                mismatch_count > 0,
                parse_review_count > 0,
                missing_common_box_count > 0,
                missing_rubber_block_count > 0,
                missing_changeover_count > 0,
            )
        ):
            import_status = NEEDS_REVIEW
        order_status = (
            ORDER_PENDING_RECEIPT if import_status == READY else NEEDS_REVIEW
        )
        return ImportAnalysis(
            customer=customer,
            source_file_name=source_file_name,
            source_file_hash=source_file_hash,
            template=template,
            external_po_no=sorted_layouts[0]["external_po_no"] if sorted_layouts else None,
            header_total_carton_qty=header_total,
            layout_total_carton_qty=layout_total,
            parsed_layout_count=len(sorted_layouts),
            mismatch_count=mismatch_count,
            missing_common_box_count=missing_common_box_count,
            missing_rubber_block_count=missing_rubber_block_count,
            generated_changeover_step_count=len(changeover_steps),
            import_status=import_status,
            order_status=order_status,
            layouts=sorted_layouts,
            slot_values=slot_values,
            changeover_steps=changeover_steps,
            changeover_step_items=changeover_step_items,
        )

    def _get_workbook(self, parsed_json: Any) -> dict[str, Any]:
        roots = _as_list(parsed_json)
        if not roots:
            raise ValueError("parsed_json is empty")
        first = roots[0]
        if isinstance(first, dict) and "sheets" in first:
            return first
        raise ValueError("parsed_json does not contain workbook sheets")

    def _extract_raw_layouts(
        self,
        workbook: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[int | None]]:
        layouts: list[dict[str, Any]] = []
        header_totals: list[int | None] = []
        for sheet in _as_list(workbook.get("sheets")):
            if not isinstance(sheet, dict):
                continue
            sheet_name = _normalize_text(sheet.get("sheet_name") or sheet.get("name"))
            sheet_header_total = _coerce_int(sheet.get("order_total_carton_qty"))
            header_totals.append(sheet_header_total)
            for parsed_layout in _as_list(sheet.get("layouts")):
                if not isinstance(parsed_layout, dict):
                    continue
                enriched = dict(parsed_layout)
                enriched.setdefault("source_sheet_name", sheet_name)
                layouts.append(enriched)
        return layouts, header_totals

    def _load_common_box_rules(self, customer_id: int) -> list[dict[str, Any]]:
        rules = self.db.scalars(
            select(XinzhenCommonBoxRule)
            .where(
                XinzhenCommonBoxRule.customer_id == customer_id,
                XinzhenCommonBoxRule.is_active.is_(True),
            )
            .order_by(XinzhenCommonBoxRule.id)
        ).all()
        if not rules:
            return []
        product_ids = {rule.product_id for rule in rules}
        products = {
            product.id: product
            for product in self.db.scalars(
                select(Product).where(Product.id.in_(product_ids))
            ).all()
        }
        result: list[dict[str, Any]] = []
        for rule in rules:
            product = products.get(rule.product_id)
            if product is None or product.deleted_at is not None or not product.is_active:
                continue
            result.append(
                {
                    "product_id": rule.product_id,
                    "dimension_hint_text": _normalize_dimension_text(rule.dimension_hint_text),
                    "product_size": _normalize_text(rule.product_size),
                    "units_per_carton": rule.units_per_carton,
                    "vendor_style_no": _normalize_text(rule.vendor_style_no),
                    "color": _normalize_text(rule.color),
                }
            )
        return result

    def _load_rubber_block_index(
        self,
        customer_id: int,
    ) -> dict[tuple[str, str], XinzhenRubberTypeBlock]:
        rows = self.db.scalars(
            select(XinzhenRubberTypeBlock)
            .where(
                XinzhenRubberTypeBlock.customer_id == customer_id,
                XinzhenRubberTypeBlock.is_active.is_(True),
            )
            .order_by(XinzhenRubberTypeBlock.id)
        ).all()
        return {
            (_normalize_key(row.block_type), _normalize_key(row.block_text)): row
            for row in rows
        }

    def _build_layout_preview(
        self,
        *,
        source_file_name: str,
        template: TemplateContext,
        customer_id: int,
        common_box_rules: list[dict[str, Any]],
        layout_index: int,
        parsed_layout: dict[str, Any],
    ) -> dict[str, Any]:
        customer_mark_name = _normalize_text(
            parsed_layout.get("to_customer_name")
            or parsed_layout.get("customer_mark_name")
        )
        vendor_style_no = _normalize_text(parsed_layout.get("vendor_style_no"))
        color = _normalize_text(parsed_layout.get("color"))
        product_size = _normalize_text(parsed_layout.get("product_size"))
        units_per_carton = _coerce_int(parsed_layout.get("units_per_carton"))
        total_units = _coerce_int(parsed_layout.get("layout_total_units"))
        explicit_carton_qty = _coerce_int(parsed_layout.get("layout_carton_qty"))
        carton_no_range_text = _normalize_text(parsed_layout.get("carton_no_range_text"))
        summary_text = _normalize_text(parsed_layout.get("summary_text"))
        source_block_index = _coerce_int(parsed_layout.get("layout_index"))
        carton_no_start = _coerce_int(parsed_layout.get("carton_no_start"))
        carton_no_end = _coerce_int(parsed_layout.get("carton_no_end"))
        range_start, range_end, range_qty = _parse_range_text(carton_no_range_text)
        if carton_no_start is None:
            carton_no_start = range_start
        if carton_no_end is None:
            carton_no_end = range_end
        summary_units, summary_qty = _parse_summary_text(summary_text)
        if total_units is None:
            total_units = summary_units
        carton_qty = explicit_carton_qty
        if carton_qty is None:
            carton_qty = range_qty if range_qty is not None else summary_qty
        if carton_qty is None:
            carton_qty = 0
        issues: list[str] = []
        if range_qty is not None and carton_qty != range_qty:
            issues.append("range_text_carton_qty_mismatch")
        if summary_qty is not None and carton_qty != summary_qty:
            issues.append("summary_text_carton_qty_mismatch")
        if (
            units_per_carton is not None
            and total_units is not None
            and carton_qty > 0
            and units_per_carton * carton_qty != total_units
        ):
            issues.append("units_per_carton_mismatch")
        if carton_qty <= 0:
            issues.append("missing_layout_carton_qty")
        common_box = self._match_common_box(
            rules=common_box_rules,
            common_box_note=_normalize_text(parsed_layout.get("common_box_note")),
            product_size=product_size,
            units_per_carton=units_per_carton,
            vendor_style_no=vendor_style_no,
            color=color,
        )
        snapshot = {
            "customer_mark_name": customer_mark_name,
            "vendor_style_no": vendor_style_no,
            "color": color,
            "product_size": product_size,
            "units_per_carton": units_per_carton,
            "total_units": total_units,
            "carton_qty": carton_qty,
            "carton_no_range_text": carton_no_range_text,
            "external_po_no": _normalize_text(parsed_layout.get("external_po_no")),
        }
        hash_parts = [
            snapshot["external_po_no"] or "",
            customer_mark_name,
            vendor_style_no,
            color,
            product_size,
            str(units_per_carton or ""),
            str(total_units or ""),
            str(carton_qty),
            carton_no_range_text,
        ]
        return {
            "id": None,
            "layout_code": f"L{layout_index:03d}",
            "source_file_name": source_file_name,
            "source_sheet_name": _normalize_text(parsed_layout.get("source_sheet_name")),
            "source_block_index": source_block_index or layout_index,
            "external_po_no": snapshot["external_po_no"],
            "customer_mark_name": customer_mark_name,
            "vendor_style_no": vendor_style_no,
            "color": color,
            "product_size": product_size,
            "units_per_carton": units_per_carton,
            "total_units": total_units,
            "carton_qty": carton_qty,
            "carton_no_start": carton_no_start,
            "carton_no_end": carton_no_end,
            "carton_no_range_text": carton_no_range_text,
            "carton_qty_formula_text": summary_text,
            "resin_template_id": template.template_id,
            "common_box_id": common_box["common_box_id"],
            "box_match_status": common_box["box_match_status"],
            "box_match_strategy": common_box["box_match_strategy"],
            "common_box_dimension": common_box["common_box_dimension"],
            "print_content_snapshot": json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "layout_hash": _hash_layout(hash_parts),
            "sort_order": 0,
            "print_status": "pending",
            "parse_status": "ok" if not issues else NEEDS_REVIEW,
            "validation_issues": issues,
            "customer_id": customer_id,
        }

    def _match_common_box(
        self,
        *,
        rules: list[dict[str, Any]],
        common_box_note: str,
        product_size: str,
        units_per_carton: int | None,
        vendor_style_no: str,
        color: str,
    ) -> dict[str, Any]:
        hint = _normalize_dimension_text(common_box_note)
        if hint:
            for rule in rules:
                if rule["dimension_hint_text"] == hint:
                    return {
                        "common_box_id": rule["product_id"],
                        "box_match_status": "matched",
                        "box_match_strategy": "dimension_hint",
                        "common_box_dimension": hint,
                    }
        for rule in rules:
            if (
                _normalize_key(rule["product_size"]) == _normalize_key(product_size)
                and rule["units_per_carton"] == units_per_carton
            ):
                return {
                    "common_box_id": rule["product_id"],
                    "box_match_status": "matched",
                    "box_match_strategy": "size_units",
                    "common_box_dimension": rule["dimension_hint_text"],
                }
        for rule in rules:
            if (
                _normalize_key(rule["vendor_style_no"]) == _normalize_key(vendor_style_no)
                and _normalize_key(rule["color"]) == _normalize_key(color)
                and _normalize_key(rule["product_size"]) == _normalize_key(product_size)
            ):
                return {
                    "common_box_id": rule["product_id"],
                    "box_match_status": "matched",
                    "box_match_strategy": "style_color_size",
                    "common_box_dimension": rule["dimension_hint_text"],
                }
        return {
            "common_box_id": None,
            "box_match_status": "missing",
            "box_match_strategy": None,
            "common_box_dimension": None,
        }

    def _sort_layouts(self, layouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sorted_layouts = sorted(
            layouts,
            key=lambda row: (
                row["resin_template_id"] or 0,
                1 if row["common_box_id"] is None else 0,
                row["common_box_id"] or 0,
                _normalize_key(row["customer_mark_name"]),
                _normalize_key(row["product_size"]),
                row["units_per_carton"] or 0,
                _normalize_key(row["vendor_style_no"]),
                _normalize_key(row["color"]),
                row["carton_no_start"] if row["carton_no_start"] is not None else 10**9,
            ),
        )
        for index, layout in enumerate(sorted_layouts, start=1):
            layout["sort_order"] = index
        return sorted_layouts

    def _slot_value_for_layout(self, layout: dict[str, Any], field_name: str) -> str:
        value = layout.get(field_name)
        if value is None:
            return ""
        return _normalize_text(value)

    def _build_slot_values(
        self,
        *,
        customer_id: int,
        layouts: list[dict[str, Any]],
        slots: list[dict[str, Any]],
        rubber_index: dict[tuple[str, str], XinzhenRubberTypeBlock],
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        slot_values: list[dict[str, Any]] = []
        slot_values_by_layout: dict[str, list[dict[str, Any]]] = {}
        for layout in layouts:
            layout_slots: list[dict[str, Any]] = []
            for slot in slots:
                slot_value = self._slot_value_for_layout(layout, slot["field_name"])
                block = rubber_index.get(
                    (_normalize_key(slot["block_type"]), _normalize_key(slot_value))
                )
                layout_slot = {
                    "id": None,
                    "layout_code": layout["layout_code"],
                    "print_layout_id": None,
                    "slot_code": slot["slot_code"],
                    "slot_name": slot["slot_name"],
                    "field_name": slot["field_name"],
                    "block_type": slot["block_type"],
                    "display_order": slot["display_order"],
                    "slot_value": slot_value,
                    "rubber_block_id": block.id if block is not None else None,
                    "rubber_block_code": block.block_code if block is not None else None,
                    "rubber_block_storage_location": (
                        block.storage_location_text if block is not None else None
                    ),
                    "match_status": "matched" if block is not None else "missing",
                    "customer_id": customer_id,
                }
                layout_slots.append(layout_slot)
                slot_values.append(layout_slot)
            slot_values_by_layout[layout["layout_code"]] = layout_slots
        return slot_values, slot_values_by_layout

    def _build_changeovers(
        self,
        *,
        layouts: list[dict[str, Any]],
        slot_values_by_layout: dict[str, list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        steps: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        for index in range(len(layouts) - 1):
            from_layout = layouts[index]
            to_layout = layouts[index + 1]
            from_slots = {
                row["slot_code"]: row
                for row in slot_values_by_layout.get(from_layout["layout_code"], [])
            }
            to_slots = {
                row["slot_code"]: row
                for row in slot_values_by_layout.get(to_layout["layout_code"], [])
            }
            step_items: list[dict[str, Any]] = []
            for slot_code, from_slot in from_slots.items():
                to_slot = to_slots.get(slot_code)
                if to_slot is None:
                    continue
                if _normalize_text(from_slot["slot_value"]) == _normalize_text(
                    to_slot["slot_value"]
                ):
                    continue
                confirm_status = (
                    "needs_change"
                    if to_slot["match_status"] == "matched"
                    else "missing"
                )
                step_item = {
                    "id": None,
                    "step_no": index + 1,
                    "changeover_step_id": None,
                    "slot_code": slot_code,
                    "slot_name": from_slot["slot_name"],
                    "old_value": from_slot["slot_value"],
                    "new_value": to_slot["slot_value"],
                    "rubber_block_id": to_slot["rubber_block_id"],
                    "rubber_block_storage_location": to_slot[
                        "rubber_block_storage_location"
                    ],
                    "confirm_status": confirm_status,
                }
                step_items.append(step_item)
                items.append(step_item)
            if any(item["confirm_status"] == "missing" for item in step_items):
                status = NEEDS_REVIEW
            elif step_items:
                status = READY
            else:
                status = "no_change"
            steps.append(
                {
                    "id": None,
                    "order_id": None,
                    "from_layout_id": None,
                    "to_layout_id": None,
                    "from_layout_code": from_layout["layout_code"],
                    "to_layout_code": to_layout["layout_code"],
                    "step_no": index + 1,
                    "changed_slot_count": len(step_items),
                    "status": status,
                }
            )
        return steps, items

    def _persist_analysis(
        self,
        analysis: ImportAnalysis,
        *,
        user_id: int | None,
    ) -> XinzhenCartonMarkingImportBatch:
        order = XinzhenCartonMarkingOrder(
            order_number=self._next_order_number(),
            customer_id=analysis.customer.id,
            external_po_no=analysis.external_po_no,
            source_file_name=analysis.source_file_name,
            header_total_carton_qty=analysis.header_total_carton_qty,
            total_carton_qty=analysis.layout_total_carton_qty,
            layout_count=analysis.parsed_layout_count,
            status=analysis.order_status,
            resin_template_id=analysis.template.template_id,
            created_by=user_id,
        )
        self.db.add(order)
        self.db.flush()
        batch = XinzhenCartonMarkingImportBatch(
            batch_number=self._next_batch_number(),
            customer_id=analysis.customer.id,
            order_id=order.id,
            source_file_name=analysis.source_file_name,
            source_file_hash=analysis.source_file_hash,
            parsed_layout_count=analysis.parsed_layout_count,
            header_total_carton_qty=analysis.header_total_carton_qty,
            layout_total_carton_qty=analysis.layout_total_carton_qty,
            mismatch_count=analysis.mismatch_count,
            missing_common_box_count=analysis.missing_common_box_count,
            missing_rubber_block_count=analysis.missing_rubber_block_count,
            generated_changeover_step_count=analysis.generated_changeover_step_count,
            import_status=analysis.import_status,
            created_by=user_id,
        )
        self.db.add(batch)
        self.db.flush()
        layout_id_by_code: dict[str, int] = {}
        for preview in analysis.layouts:
            layout = XinzhenPrintLayout(
                import_batch_id=batch.id,
                order_id=order.id,
                layout_code=preview["layout_code"],
                source_file_name=preview["source_file_name"],
                source_sheet_name=preview["source_sheet_name"],
                source_block_index=preview["source_block_index"],
                external_po_no=preview["external_po_no"],
                customer_mark_name=preview["customer_mark_name"],
                vendor_style_no=preview["vendor_style_no"],
                color=preview["color"],
                product_size=preview["product_size"],
                units_per_carton=preview["units_per_carton"],
                total_units=preview["total_units"],
                carton_qty=preview["carton_qty"],
                carton_no_start=preview["carton_no_start"],
                carton_no_end=preview["carton_no_end"],
                carton_no_range_text=preview["carton_no_range_text"],
                carton_qty_formula_text=preview["carton_qty_formula_text"],
                resin_template_id=analysis.template.template_id,
                common_box_id=preview["common_box_id"],
                box_match_status=preview["box_match_status"],
                box_match_strategy=preview["box_match_strategy"],
                common_box_dimension=preview["common_box_dimension"],
                print_content_snapshot=preview["print_content_snapshot"],
                layout_hash=preview["layout_hash"],
                sort_order=preview["sort_order"],
                print_status=preview["print_status"],
                parse_status=preview["parse_status"],
            )
            self.db.add(layout)
            self.db.flush()
            layout_id_by_code[preview["layout_code"]] = layout.id
        step_id_by_no: dict[int, int] = {}
        for preview in analysis.changeover_steps:
            step = XinzhenPrintChangeoverStep(
                order_id=order.id,
                from_layout_id=layout_id_by_code[preview["from_layout_code"]],
                to_layout_id=layout_id_by_code[preview["to_layout_code"]],
                step_no=preview["step_no"],
                changed_slot_count=preview["changed_slot_count"],
                status=preview["status"],
            )
            self.db.add(step)
            self.db.flush()
            step_id_by_no[preview["step_no"]] = step.id
        for preview in analysis.slot_values:
            self.db.add(
                XinzhenPrintLayoutSlotValue(
                    print_layout_id=layout_id_by_code[preview["layout_code"]],
                    slot_code=preview["slot_code"],
                    slot_name=preview["slot_name"],
                    field_name=preview["field_name"],
                    block_type=preview["block_type"],
                    display_order=preview["display_order"],
                    slot_value=preview["slot_value"],
                    rubber_block_id=preview["rubber_block_id"],
                    rubber_block_code=preview["rubber_block_code"],
                    rubber_block_storage_location=preview[
                        "rubber_block_storage_location"
                    ],
                    match_status=preview["match_status"],
                )
            )
        for preview in analysis.changeover_step_items:
            self.db.add(
                XinzhenPrintChangeoverStepItem(
                    changeover_step_id=step_id_by_no[preview["step_no"]],
                    slot_code=preview["slot_code"],
                    slot_name=preview["slot_name"],
                    old_value=preview["old_value"],
                    new_value=preview["new_value"],
                    rubber_block_id=preview["rubber_block_id"],
                    rubber_block_storage_location=preview[
                        "rubber_block_storage_location"
                    ],
                    confirm_status=preview["confirm_status"],
                )
            )
        self.db.flush()
        return batch

    def _next_order_number(self) -> str:
        return f"XZCM-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6].upper()}"

    def _next_batch_number(self) -> str:
        return f"XZIM-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6].upper()}"

    def _serialize_preview(
        self,
        analysis: ImportAnalysis,
        *,
        mode: str,
    ) -> dict[str, Any]:
        response = {
            "mode": mode,
            "import_batch_id": None,
            "order_id": None,
            "parsed_layout_count": analysis.parsed_layout_count,
            "header_total_carton_qty": analysis.header_total_carton_qty,
            "layout_total_carton_qty": analysis.layout_total_carton_qty,
            "mismatch_count": analysis.mismatch_count,
            "missing_common_box_count": analysis.missing_common_box_count,
            "missing_rubber_block_count": analysis.missing_rubber_block_count,
            "generated_changeover_step_count": analysis.generated_changeover_step_count,
            "import_status": analysis.import_status,
        }
        response["import_batch"] = {
            "id": None,
            "batch_number": None,
            "customer_id": analysis.customer.id,
            "customer_name": analysis.customer.name,
            "source_file_name": analysis.source_file_name,
            "source_file_hash": analysis.source_file_hash,
            "order_id": None,
            "parsed_layout_count": analysis.parsed_layout_count,
            "header_total_carton_qty": analysis.header_total_carton_qty,
            "layout_total_carton_qty": analysis.layout_total_carton_qty,
            "mismatch_count": analysis.mismatch_count,
            "missing_common_box_count": analysis.missing_common_box_count,
            "missing_rubber_block_count": analysis.missing_rubber_block_count,
            "generated_changeover_step_count": analysis.generated_changeover_step_count,
            "import_status": analysis.import_status,
        }
        response["order"] = {
            "id": None,
            "order_number": None,
            "customer_id": analysis.customer.id,
            "customer_name": analysis.customer.name,
            "external_po_no": analysis.external_po_no,
            "source_file_name": analysis.source_file_name,
            "header_total_carton_qty": analysis.header_total_carton_qty,
            "total_carton_qty": analysis.layout_total_carton_qty,
            "layout_count": analysis.parsed_layout_count,
            "status": analysis.order_status,
            "resin_template_id": analysis.template.template_id,
        }
        response["print_layouts"] = [
            self._serialize_layout_preview(layout) for layout in analysis.layouts
        ]
        response["print_layout_slot_values"] = [
            self._serialize_slot_preview(row) for row in analysis.slot_values
        ]
        response["print_changeover_steps"] = [
            self._serialize_step_preview(row) for row in analysis.changeover_steps
        ]
        response["print_changeover_step_items"] = [
            self._serialize_step_item_preview(row)
            for row in analysis.changeover_step_items
        ]
        response["confirmation_page"] = self._build_confirmation_page(
            file_name=analysis.source_file_name,
            customer_name=analysis.customer.name,
            external_po_no=analysis.external_po_no,
            header_total_carton_qty=analysis.header_total_carton_qty,
            layout_total_carton_qty=analysis.layout_total_carton_qty,
            mismatch_count=analysis.mismatch_count,
            missing_common_box_count=analysis.missing_common_box_count,
            missing_rubber_block_count=analysis.missing_rubber_block_count,
            import_status=analysis.import_status,
            layouts=response["print_layouts"],
            slot_values=response["print_layout_slot_values"],
        )
        response["mobile_read_model"] = self._build_mobile_read_model(
            customer=analysis.customer,
            external_po_no=analysis.external_po_no,
            total_carton_qty=analysis.layout_total_carton_qty,
            layout_count=analysis.parsed_layout_count,
            resin_template_id=analysis.template.template_id,
            missing_common_box_count=analysis.missing_common_box_count,
            missing_rubber_block_count=analysis.missing_rubber_block_count,
            layouts=response["print_layouts"],
            changeover_steps=response["print_changeover_steps"],
            changeover_items=response["print_changeover_step_items"],
        )
        return response

    def _serialize_batch(
        self,
        batch: XinzhenCartonMarkingImportBatch,
    ) -> dict[str, Any]:
        customer = self._require_customer(batch.customer_id)
        order = self.db.get(XinzhenCartonMarkingOrder, batch.order_id)
        if order is None:
            raise ValueError("导入批次关联订单不存在")
        layouts = self.db.scalars(
            select(XinzhenPrintLayout)
            .where(XinzhenPrintLayout.import_batch_id == batch.id)
            .order_by(XinzhenPrintLayout.sort_order, XinzhenPrintLayout.id)
        ).all()
        layout_ids = [layout.id for layout in layouts]
        slots = self.db.scalars(
            select(XinzhenPrintLayoutSlotValue)
            .where(XinzhenPrintLayoutSlotValue.print_layout_id.in_(layout_ids))
            .order_by(
                XinzhenPrintLayoutSlotValue.print_layout_id,
                XinzhenPrintLayoutSlotValue.display_order,
                XinzhenPrintLayoutSlotValue.id,
            )
        ).all() if layout_ids else []
        steps = self.db.scalars(
            select(XinzhenPrintChangeoverStep)
            .where(XinzhenPrintChangeoverStep.order_id == order.id)
            .order_by(XinzhenPrintChangeoverStep.step_no, XinzhenPrintChangeoverStep.id)
        ).all()
        step_ids = [step.id for step in steps]
        step_items = self.db.scalars(
            select(XinzhenPrintChangeoverStepItem)
            .where(XinzhenPrintChangeoverStepItem.changeover_step_id.in_(step_ids))
            .order_by(
                XinzhenPrintChangeoverStepItem.changeover_step_id,
                XinzhenPrintChangeoverStepItem.id,
            )
        ).all() if step_ids else []
        serialized_layouts = [self._serialize_layout_record(layout) for layout in layouts]
        serialized_slots = [
            self._serialize_slot_record(row, layouts_by_id={layout.id: layout for layout in layouts})
            for row in slots
        ]
        layouts_by_id = {layout.id: layout for layout in layouts}
        serialized_steps = [
            self._serialize_step_record(step, layouts_by_id=layouts_by_id) for step in steps
        ]
        serialized_step_items = [
            self._serialize_step_item_record(item) for item in step_items
        ]
        response = {
            "mode": "apply",
            "import_batch_id": batch.id,
            "order_id": order.id,
            "parsed_layout_count": batch.parsed_layout_count,
            "header_total_carton_qty": batch.header_total_carton_qty,
            "layout_total_carton_qty": batch.layout_total_carton_qty,
            "mismatch_count": batch.mismatch_count,
            "missing_common_box_count": batch.missing_common_box_count,
            "missing_rubber_block_count": batch.missing_rubber_block_count,
            "generated_changeover_step_count": batch.generated_changeover_step_count,
            "import_status": batch.import_status,
            "import_batch": {
                "id": batch.id,
                "batch_number": batch.batch_number,
                "customer_id": batch.customer_id,
                "customer_name": customer.name,
                "source_file_name": batch.source_file_name,
                "source_file_hash": batch.source_file_hash,
                "order_id": batch.order_id,
                "parsed_layout_count": batch.parsed_layout_count,
                "header_total_carton_qty": batch.header_total_carton_qty,
                "layout_total_carton_qty": batch.layout_total_carton_qty,
                "mismatch_count": batch.mismatch_count,
                "missing_common_box_count": batch.missing_common_box_count,
                "missing_rubber_block_count": batch.missing_rubber_block_count,
                "generated_changeover_step_count": batch.generated_changeover_step_count,
                "import_status": batch.import_status,
            },
            "order": {
                "id": order.id,
                "order_number": order.order_number,
                "customer_id": order.customer_id,
                "customer_name": customer.name,
                "external_po_no": order.external_po_no,
                "source_file_name": order.source_file_name,
                "header_total_carton_qty": order.header_total_carton_qty,
                "total_carton_qty": order.total_carton_qty,
                "layout_count": order.layout_count,
                "status": order.status,
                "resin_template_id": order.resin_template_id,
            },
            "print_layouts": serialized_layouts,
            "print_layout_slot_values": serialized_slots,
            "print_changeover_steps": serialized_steps,
            "print_changeover_step_items": serialized_step_items,
        }
        response["confirmation_page"] = self._build_confirmation_page(
            file_name=batch.source_file_name,
            customer_name=customer.name,
            external_po_no=order.external_po_no,
            header_total_carton_qty=batch.header_total_carton_qty,
            layout_total_carton_qty=batch.layout_total_carton_qty,
            mismatch_count=batch.mismatch_count,
            missing_common_box_count=batch.missing_common_box_count,
            missing_rubber_block_count=batch.missing_rubber_block_count,
            import_status=batch.import_status,
            layouts=serialized_layouts,
            slot_values=serialized_slots,
        )
        response["mobile_read_model"] = self._build_mobile_read_model(
            customer=customer,
            external_po_no=order.external_po_no,
            total_carton_qty=order.total_carton_qty,
            layout_count=order.layout_count,
            resin_template_id=order.resin_template_id,
            missing_common_box_count=batch.missing_common_box_count,
            missing_rubber_block_count=batch.missing_rubber_block_count,
            layouts=serialized_layouts,
            changeover_steps=serialized_steps,
            changeover_items=serialized_step_items,
        )
        return response

    def _serialize_layout_preview(self, layout: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": layout["id"],
            "layout_code": layout["layout_code"],
            "source_file_name": layout["source_file_name"],
            "source_sheet_name": layout["source_sheet_name"],
            "source_block_index": layout["source_block_index"],
            "external_po_no": layout["external_po_no"],
            "customer_mark_name": layout["customer_mark_name"],
            "vendor_style_no": layout["vendor_style_no"],
            "color": layout["color"],
            "product_size": layout["product_size"],
            "units_per_carton": layout["units_per_carton"],
            "total_units": layout["total_units"],
            "carton_qty": layout["carton_qty"],
            "carton_no_start": layout["carton_no_start"],
            "carton_no_end": layout["carton_no_end"],
            "carton_no_range_text": layout["carton_no_range_text"],
            "carton_qty_formula_text": layout["carton_qty_formula_text"],
            "resin_template_id": layout["resin_template_id"],
            "common_box_id": layout["common_box_id"],
            "box_match_status": layout["box_match_status"],
            "box_match_strategy": layout["box_match_strategy"],
            "common_box_dimension": layout["common_box_dimension"],
            "print_content_snapshot": json.loads(layout["print_content_snapshot"]),
            "layout_hash": layout["layout_hash"],
            "sort_order": layout["sort_order"],
            "print_status": layout["print_status"],
            "parse_status": layout["parse_status"],
            "validation_issues": list(layout["validation_issues"]),
        }

    def _serialize_slot_preview(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "layout_code": row["layout_code"],
            "print_layout_id": row["print_layout_id"],
            "slot_code": row["slot_code"],
            "slot_name": row["slot_name"],
            "field_name": row["field_name"],
            "block_type": row["block_type"],
            "display_order": row["display_order"],
            "slot_value": row["slot_value"],
            "rubber_block_id": row["rubber_block_id"],
            "rubber_block_code": row["rubber_block_code"],
            "rubber_block_storage_location": row["rubber_block_storage_location"],
            "match_status": row["match_status"],
        }

    def _serialize_step_preview(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "order_id": row["order_id"],
            "from_layout_id": row["from_layout_id"],
            "to_layout_id": row["to_layout_id"],
            "from_layout_code": row["from_layout_code"],
            "to_layout_code": row["to_layout_code"],
            "step_no": row["step_no"],
            "changed_slot_count": row["changed_slot_count"],
            "status": row["status"],
        }

    def _serialize_step_item_preview(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "changeover_step_id": row["changeover_step_id"],
            "step_no": row["step_no"],
            "slot_code": row["slot_code"],
            "slot_name": row["slot_name"],
            "old_value": row["old_value"],
            "new_value": row["new_value"],
            "rubber_block_id": row["rubber_block_id"],
            "rubber_block_storage_location": row["rubber_block_storage_location"],
            "confirm_status": row["confirm_status"],
        }

    def _serialize_layout_record(self, layout: XinzhenPrintLayout) -> dict[str, Any]:
        return {
            "id": layout.id,
            "layout_code": layout.layout_code,
            "source_file_name": layout.source_file_name,
            "source_sheet_name": layout.source_sheet_name,
            "source_block_index": layout.source_block_index,
            "external_po_no": layout.external_po_no,
            "customer_mark_name": layout.customer_mark_name,
            "vendor_style_no": layout.vendor_style_no,
            "color": layout.color,
            "product_size": layout.product_size,
            "units_per_carton": layout.units_per_carton,
            "total_units": layout.total_units,
            "carton_qty": layout.carton_qty,
            "carton_no_start": layout.carton_no_start,
            "carton_no_end": layout.carton_no_end,
            "carton_no_range_text": layout.carton_no_range_text,
            "carton_qty_formula_text": layout.carton_qty_formula_text,
            "resin_template_id": layout.resin_template_id,
            "common_box_id": layout.common_box_id,
            "box_match_status": layout.box_match_status,
            "box_match_strategy": layout.box_match_strategy,
            "common_box_dimension": layout.common_box_dimension,
            "print_content_snapshot": (
                json.loads(layout.print_content_snapshot)
                if layout.print_content_snapshot
                else {}
            ),
            "layout_hash": layout.layout_hash,
            "sort_order": layout.sort_order,
            "print_status": layout.print_status,
            "parse_status": layout.parse_status,
            "validation_issues": [],
        }

    def _serialize_slot_record(
        self,
        row: XinzhenPrintLayoutSlotValue,
        *,
        layouts_by_id: dict[int, XinzhenPrintLayout],
    ) -> dict[str, Any]:
        layout = layouts_by_id[row.print_layout_id]
        return {
            "id": row.id,
            "layout_code": layout.layout_code,
            "print_layout_id": row.print_layout_id,
            "slot_code": row.slot_code,
            "slot_name": row.slot_name,
            "field_name": row.field_name,
            "block_type": row.block_type,
            "display_order": row.display_order,
            "slot_value": row.slot_value or "",
            "rubber_block_id": row.rubber_block_id,
            "rubber_block_code": row.rubber_block_code,
            "rubber_block_storage_location": row.rubber_block_storage_location,
            "match_status": row.match_status,
        }

    def _serialize_step_record(
        self,
        step: XinzhenPrintChangeoverStep,
        *,
        layouts_by_id: dict[int, XinzhenPrintLayout],
    ) -> dict[str, Any]:
        return {
            "id": step.id,
            "order_id": step.order_id,
            "from_layout_id": step.from_layout_id,
            "to_layout_id": step.to_layout_id,
            "from_layout_code": layouts_by_id[step.from_layout_id].layout_code,
            "to_layout_code": layouts_by_id[step.to_layout_id].layout_code,
            "step_no": step.step_no,
            "changed_slot_count": step.changed_slot_count,
            "status": step.status,
        }

    def _serialize_step_item_record(
        self,
        row: XinzhenPrintChangeoverStepItem,
    ) -> dict[str, Any]:
        return {
            "id": row.id,
            "changeover_step_id": row.changeover_step_id,
            "slot_code": row.slot_code,
            "slot_name": row.slot_name,
            "old_value": row.old_value,
            "new_value": row.new_value,
            "rubber_block_id": row.rubber_block_id,
            "rubber_block_storage_location": row.rubber_block_storage_location,
            "confirm_status": row.confirm_status,
        }

    def _build_confirmation_page(
        self,
        *,
        file_name: str,
        customer_name: str,
        external_po_no: str | None,
        header_total_carton_qty: int | None,
        layout_total_carton_qty: int,
        mismatch_count: int,
        missing_common_box_count: int,
        missing_rubber_block_count: int,
        import_status: str,
        layouts: list[dict[str, Any]],
        slot_values: list[dict[str, Any]],
    ) -> dict[str, Any]:
        missing_counts: dict[str, int] = {}
        for row in slot_values:
            if row["match_status"] == "missing":
                missing_counts[row["layout_code"]] = (
                    missing_counts.get(row["layout_code"], 0) + 1
                )
        confirmation_layouts = []
        for layout in layouts:
            confirmation_layouts.append(
                {
                    "layout_code": layout["layout_code"],
                    "carton_no_range_text": layout["carton_no_range_text"],
                    "carton_qty": layout["carton_qty"],
                    "vendor_style_no": layout["vendor_style_no"],
                    "color": layout["color"],
                    "product_size": layout["product_size"],
                    "units_per_carton": layout["units_per_carton"],
                    "box_match_status": layout["box_match_status"],
                    "parse_status": layout["parse_status"],
                    "missing_slot_count": missing_counts.get(layout["layout_code"], 0),
                }
            )
        return {
            "file_name": file_name,
            "customer_name": customer_name,
            "po": external_po_no,
            "header_total_carton_qty": header_total_carton_qty,
            "layout_total_carton_qty": layout_total_carton_qty,
            "layout_count": len(layouts),
            "mismatch_count": mismatch_count,
            "missing_common_box_count": missing_common_box_count,
            "missing_rubber_block_count": missing_rubber_block_count,
            "import_status": import_status,
            "layouts": confirmation_layouts,
        }

    def _build_mobile_read_model(
        self,
        *,
        customer: Customer,
        external_po_no: str | None,
        total_carton_qty: int,
        layout_count: int,
        resin_template_id: int | None,
        missing_common_box_count: int,
        missing_rubber_block_count: int,
        layouts: list[dict[str, Any]],
        changeover_steps: list[dict[str, Any]],
        changeover_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        items_by_step: dict[int | None, list[dict[str, Any]]] = {}
        for item in changeover_items:
            items_by_step.setdefault(item["changeover_step_id"], []).append(item)
        changeover_views = []
        for step in changeover_steps:
            step_items = items_by_step.get(step["id"], [])
            if not step_items:
                step_items = [
                    item
                    for item in changeover_items
                    if item.get("step_no") == step["step_no"]
                ]
            changeover_views.append(
                {
                    "step_no": step["step_no"],
                    "from_layout_code": step["from_layout_code"],
                    "to_layout_code": step["to_layout_code"],
                    "changed_slot_count": step["changed_slot_count"],
                    "status": step["status"],
                    "items": step_items,
                }
            )
        return {
            "order_overview": {
                "customer_id": customer.id,
                "customer_name": customer.name,
                "external_po_no": external_po_no,
                "total_carton_qty": total_carton_qty,
                "layout_count": layout_count,
                "resin_template_id": resin_template_id,
                "missing_common_box_count": missing_common_box_count,
                "missing_rubber_block_count": missing_rubber_block_count,
                "current_print_progress": 0,
            },
            "current_layouts": [
                {
                    "id": layout["id"],
                    "layout_code": layout["layout_code"],
                    "sort_order": layout["sort_order"],
                    "carton_qty": layout["carton_qty"],
                    "carton_no_range_text": layout["carton_no_range_text"],
                    "vendor_style_no": layout["vendor_style_no"],
                    "color": layout["color"],
                    "product_size": layout["product_size"],
                    "units_per_carton": layout["units_per_carton"],
                    "common_box_id": layout["common_box_id"],
                    "box_match_status": layout["box_match_status"],
                    "parse_status": layout["parse_status"],
                }
                for layout in layouts
            ],
            "changeover_views": changeover_views,
        }
