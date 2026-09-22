"""Bind a published drawing to a newly created production task once."""
from decimal import Decimal, InvalidOperation
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.drawing_design import DrawingRelease, ProductionTaskDrawing, ProductionTaskDrawingAdoption
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.order import Order, OrderItem
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.drawing_switch import drawing_v2_enabled


_EXPLICIT_DIMENSIONS = re.compile(
    r"\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)"
    r"(?:\s*[xX×*]\s*(\d+(?:\.\d+)?))?\s*(?:mm)?\s*", re.IGNORECASE)


def _snapshot_dimensions(specification: str | None) -> tuple[Decimal, ...] | None:
    match = _EXPLICIT_DIMENSIONS.fullmatch(specification or "")
    if match is None:
        return None
    values = tuple(Decimal(value) for value in match.groups() if value is not None)
    return values if all(value > 0 for value in values) else None


def _release_matches_dimensions(release: DrawingRelease, dimensions: tuple[Decimal, ...]) -> bool:
    # A two-dimensional order establishes only a liner. Do not infer box height
    # from product defaults, thickness, reporting fields, or descriptive text.
    if len(dimensions) == 2 and release.template_key != "liner_v1":
        return False
    try:
        frozen = json.loads(release.manifest_json)["product_dimensions"]
        fields = ("length_mm", "width_mm", "height_mm")[:len(dimensions)]
        return all(Decimal(str(frozen[field])) == value for field, value in zip(fields, dimensions))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def _task_drawing_context(db: Session, task: ProductionTask | None):
    item = db.get(OrderItem, task.order_item_id) if task else None
    order = db.get(Order, item.order_id) if item else None
    component_id = task.sales_order_item_bom_component_id if task else None
    component = db.get(SalesOrderItemBomComponent, component_id) if component_id else None
    if order is None or (component_id and
                         (component is None or component.sales_order_item_id != item.id)):
        raise HTTPException(409, "任务图纸绑定与订单客户或组件不一致，请核对")
    return item, order, component


def bind_new_task_drawing(db: Session, task: ProductionTask, product: Product | None,
                          *, source_is_new: bool = False) -> None:
    # Only the transaction freezing a new order source can choose its current
    # release. A repaired historical task has no frozen design version; equal
    # product versions or dimensions cannot establish its intended drawing.
    if not source_is_new or product is None or not drawing_v2_enabled():
        return
    item, order, component = _task_drawing_context(db, task)
    expected_product_id = component.component_product_id if component else item.product_id
    if product.id != expected_product_id or product.customer_id != order.customer_id:
        raise HTTPException(409, "任务图纸绑定与订单客户或组件不一致，请核对")
    # Order-only artwork and the BOM's frozen source have not been explicitly
    # adopted into the managed release chain. Keep their existing precedence.
    if item.drawing_file or (component is not None and component.snapshot_die_cut_path):
        return
    query = select(DrawingRelease).where(DrawingRelease.product_id == product.id,
                                        DrawingRelease.customer_id == product.customer_id)
    if component is not None:
        # The order's frozen component version is authoritative, not the
        # product currently on screen when a deferred task is first created.
        if component.component_product_version is None:
            return
        release = db.scalar(query.where(DrawingRelease.product_version == component.component_product_version)
                            .order_by(DrawingRelease.id.desc()).limit(1))
    else:
        dimensions = _snapshot_dimensions(item.snapshot_spec)
        if dimensions is None:
            return  # Unknown legacy evidence stays usable; explicit adoption remains available.
        # New ordinary sources use the current product version in this same
        # transaction. Matching L×W×H alone cannot prove unchanged material,
        # thickness, or mold facts from an older product version.
        query = query.where(DrawingRelease.product_version == product.version)
        release = next((candidate for candidate in db.scalars(query.order_by(DrawingRelease.id.desc())).all()
                        if _release_matches_dimensions(candidate, dimensions)), None)
    if release is not None:
        db.add(ProductionTaskDrawing(task_id=task.id, release_id=release.id))


def bound_task_release(db: Session, task_id: int) -> DrawingRelease | None:
    event = db.scalar(select(ProductionTaskDrawingAdoption)
                      .where(ProductionTaskDrawingAdoption.task_id == task_id)
                      .order_by(ProductionTaskDrawingAdoption.id.desc()).limit(1))
    release = db.get(DrawingRelease, event.new_release_id) if event else db.scalar(select(DrawingRelease).join(ProductionTaskDrawing,
                    ProductionTaskDrawing.release_id == DrawingRelease.id)
                    .where(ProductionTaskDrawing.task_id == task_id))
    if release is None:
        if event is not None:
            raise HTTPException(409, "任务图纸采用记录的发布版缺失，请核对")
        return None
    task = db.get(ProductionTask, task_id)
    item, order, component = _task_drawing_context(db, task)
    if event is not None and (event.confirmed_not_issued != 1 or
                             event.resulting_task_version != event.expected_task_version + 1 or
                             event.resulting_task_version > task.version):
        raise HTTPException(409, "任务图纸采用记录与任务版本不一致，请核对")
    expected_product = component.component_product_id if component else item.product_id
    if release.customer_id != order.customer_id or release.product_id != expected_product:
        raise HTTPException(409, "任务图纸绑定与订单客户或组件不一致，请核对")
    return release
