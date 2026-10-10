"""Read-only paper figures, keyed by real order/BOM/purchase ownership."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePath
import posixpath

from sqlalchemy import exists, select

from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.production import ProductionTask
from app.services.drawing_binding import bound_task_release
from app.services.mobile_product_drawings import DrawingSource, _engineering_reference as _mobile_engineering_reference
from app.services.product_drawings import PRINT_ARTWORK_PREFIX, engineering_drawing_condition


def _engineering_reference(reference):
    if not _mobile_engineering_reference(reference):
        return False
    # Historical pointers must not relabel an artwork original by changing
    # Windows path case or adding in-root traversal segments.
    normalized = posixpath.normpath(reference.replace("\\", "/").removeprefix("private:")).casefold()
    artwork_folder = PRINT_ARTWORK_PREFIX.removeprefix("private:").rstrip("/")
    return normalized != artwork_folder and not normalized.startswith(artwork_folder + "/")


@dataclass(frozen=True)
class PaperDrawing:
    key: str
    reference: str
    thumbnail: str | None
    product_id: int
    product_code: str | None
    basis: str
    source_label: str
    name: str = "图纸"

    def public(self, owner_type: str, owner_id: int) -> dict:
        url = f"/api/requisition/production-paper-drawings/{owner_type}/{owner_id}/{self.key}"
        return dict(key=self.key, product_id=self.product_id, product_code=self.product_code,
                    name=self.name, kind="pdf" if PurePath(self.reference).suffix.lower() == ".pdf" else "image",
                    preview_url=url + "/preview", original_url=url + "/original",
                    basis=self.basis, source_label=self.source_label)

    def source(self) -> DrawingSource:
        return DrawingSource(self.key, self.reference, name=self.name, thumbnail=self.thumbnail)


def _reference(db, product, reference, basis, label):
    if not _engineering_reference(reference):
        return []
    # An exact historical pointer may still have a retained derived thumbnail.
    attachment = db.scalar(select(ProductDrawing).where(
        ProductDrawing.product_id == product.id, ProductDrawing.image_path == reference,
        engineering_drawing_condition()).order_by(ProductDrawing.id.desc()).limit(1))
    key = "frozen-" + hashlib.sha256(reference.encode("utf-8")).hexdigest()[:24]
    return [PaperDrawing(key, reference, attachment.thumbnail_path if attachment else None,
                         product.id, product.product_code, basis, label)]


def _references(db, product):
    attachments = db.scalars(select(ProductDrawing).where(
        ProductDrawing.product_id == product.id, engineering_drawing_condition()).order_by(
        ProductDrawing.uploaded_at.desc(), ProductDrawing.id.desc())).all()
    sources = [PaperDrawing(f"attachment-{row.id}", row.image_path, row.thumbnail_path,
                        product.id, product.product_code, "current_reference", "参考图", f"图纸{index + 1}")
            for index, row in enumerate(attachments) if _engineering_reference(row.image_path)]
    if not sources and _engineering_reference(product.die_cut_path):
        sources = _reference(db, product, product.die_cut_path, "current_reference", "参考图")
    return sources


def order_sources(db, item, component=None, *, managed=False):
    product_id = component.component_product_id if component is not None else item.product_id
    product = db.get(Product, product_id) if product_id else None
    if product is None or product.customer_id != item.order.customer_id:
        return []
    if component is not None and component.sales_order_item_id != item.id:
        return []
    if managed:
        return []
    # A parent's order-only drawing never stands in for a BOM child's figure.
    reference = component.snapshot_die_cut_path if component is not None else item.drawing_file
    if reference:
        return _reference(db, product, reference,
                          "order_bom_snapshot" if component is not None else "order_attachment",
                          "任务图" if component is not None else "订单图")
    return _references(db, product)


def stock_sources(db, item):
    product = db.get(Product, item.reference_product_id or item.product_id)
    customer_id = item.customer_id or item.order.customer_id
    if product is None or product.customer_id != customer_id:
        return []
    from app.services.stock_purchase_identity import resolve
    from app.services.warehouse_inventory import WarehouseInventoryError
    try:
        identity = resolve(db, item, product)
    except (WarehouseInventoryError, ValueError, TypeError, KeyError, AttributeError):
        return []  # Incomplete historical evidence cannot be replaced by today's drawing.
    if not isinstance(identity, dict) or not isinstance(identity.get("fields"), dict):
        return []
    frozen = bool(item.production_snapshot_json) or identity.get("source") == "master_history"
    reference = identity["fields"].get("die_cut_path") if frozen else None
    if reference:
        return _reference(db, product, reference, identity["source"], "任务图")
    return _references(db, product)


def managed_public(drawing, product_code):
    return dict(key=f"release-{drawing['release_id']}", product_id=drawing["product_id"],
                product_code=product_code, name=" ".join(filter(None, [drawing.get("number"), drawing.get("revision")])) or "图纸",
                kind="image", preview_url=drawing["svg_urls"]["structure"],
                original_url=drawing["pdf_url"], basis="task_release", source_label="任务图")


def order_paper_drawings(db, item, component=None, managed_drawing=None):
    if managed_drawing:
        code = component.snapshot_component_product_code if component is not None else item.snapshot_product_code
        return [managed_public(managed_drawing, code)]
    owner_type, owner_id = ("bom-component", component.id) if component is not None else ("order-item", item.id)
    return [row.public(owner_type, owner_id) for row in order_sources(db, item, component)]


def order_has_managed_drawing(db, item, component=None):
    query = select(ProductionTask).where(ProductionTask.order_item_id == item.id)
    query = query.where(ProductionTask.sales_order_item_bom_component_id == component.id) if component is not None else query.where(ProductionTask.sales_order_item_bom_component_id.is_(None))
    task = db.scalar(query)
    return task is not None and bound_task_release(db, task.id) is not None


def has_printable_order_source(db, item, component=None):
    """A requisition reader cannot guess an unreported order's attachment."""
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
    from app.models.requisition import RequisitionItem
    from app.models.product_bom import RequisitionItemBomSource
    if component is not None:
        return db.scalar(select(RequisitionItem.id).join(RequisitionItemBomSource,
            RequisitionItemBomSource.requisition_item_id == RequisitionItem.id).where(
            RequisitionItem.order_item_id == item.id, RequisitionItem.status.in_(["有效", "已入库"]),
            RequisitionItemBomSource.sales_order_item_bom_component_id == component.id).limit(1)) is not None
    return db.scalar(select(SupplierRequisitionOrderItem.id).join(SupplierRequisitionOrder,
        SupplierRequisitionOrder.id == SupplierRequisitionOrderItem.supplier_order_id).where(
        SupplierRequisitionOrderItem.order_item_id == item.id,
        SupplierRequisitionOrderItem.product_id == item.product_id,
        SupplierRequisitionOrderItem.status == "active", SupplierRequisitionOrder.status == "confirmed").limit(1)) is not None


def has_posted_paper_receipt(db, *, item=None, component=None, stock_item=None):
    """Incoming-only readers see only the exact received source, never plans."""
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.product_bom import RequisitionItemBomSource
    query = select(IncomingReceiptItem.id).where(IncomingReceiptItem.status == "posted")
    if stock_item is not None:
        query = query.where(IncomingReceiptItem.stock_replenishment_item_id == stock_item.id)
    elif component is not None:
        query = query.join(RequisitionItemBomSource,
            RequisitionItemBomSource.requisition_item_id == IncomingReceiptItem.requisition_item_id).where(
            IncomingReceiptItem.order_item_id == item.id,
            RequisitionItemBomSource.sales_order_item_bom_component_id == component.id)
    else:
        query = query.where(IncomingReceiptItem.order_item_id == item.id, ~exists(select(
            RequisitionItemBomSource.id).where(
                RequisitionItemBomSource.requisition_item_id == IncomingReceiptItem.requisition_item_id)))
    return db.scalar(query.limit(1)) is not None
