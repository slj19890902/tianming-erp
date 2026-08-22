"""Composite-product BOM rules and immutable order snapshots.

N034 Phase A deliberately keeps the commercial order line on the parent
product.  BOM rows are master data, while sales-order BOM rows are an
immutable production preview; neither helper creates child order lines or
touches requisition, production, delivery, accounting, or inventory facts.

The model/migration phase is delivered separately.  The small column-alias
adapter below keeps this service compatible with the canonical model names
and makes an integration mismatch fail explicitly instead of silently losing
snapshot data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.audit import OperationLog
from app.models.mold_tool import MoldTool, MoldToolCustomer
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.master_data_versioning import apply_versioned_update
from app.services.product_specification import product_dimension_specification
from app.services.requisition_quantities import (
    DEFAULT_CUTTING_MODE,
    cutting_factor,
    normalize_cutting_mode,
)


class CompositeBOMError(ValueError):
    """Expected BOM validation/configuration failure with an HTTP status."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = dict(detail) if detail is not None else message


def _models() -> tuple[type[Any], type[Any]]:
    try:
        from app.models.product_bom import (
            ProductBomComponent,
            SalesOrderItemBomComponent,
        )
    except ImportError as error:  # pragma: no cover - only before model phase merge
        raise CompositeBOMError(
            "N034 BOM 模型尚未安装，不能读写组合品 BOM",
            503,
        ) from error
    return ProductBomComponent, SalesOrderItemBomComponent


def _column_name(model: type[Any], *candidates: str, required: bool = True) -> str | None:
    columns = set(model.__table__.columns.keys())
    for candidate in candidates:
        if candidate in columns:
            return candidate
    if required:
        raise CompositeBOMError(
            f"{model.__name__} 缺少字段：{' / '.join(candidates)}",
            500,
        )
    return None


def _mapped_value(row: Any, *candidates: str, default: Any = None) -> Any:
    for candidate in candidates:
        if hasattr(row, candidate):
            return getattr(row, candidate)
    return default


def _as_decimal(value: Any, *, label: str = "组件用量") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CompositeBOMError(f"{label}无效") from error
    if not result.is_finite() or result <= 0:
        raise CompositeBOMError(f"{label}必须大于0")
    if result != result.to_integral_value():
        raise CompositeBOMError(f"{label}必须是正整数")
    return result


def internal_component_code(parent_product_code: str, position: int) -> str:
    """Return the server-owned internal code (for example P100-S01)."""

    parent_code = (parent_product_code or "").strip()
    if not parent_code:
        raise CompositeBOMError("父产品编码不能为空")
    if position < 1 or position > 99:
        raise CompositeBOMError("组合品组件序号必须在1到99之间")
    return f"{parent_code}-S{position:02d}"


def validate_component_graph(
    *,
    parent_product_id: int,
    component_product_ids: Sequence[int],
    adjacency: Mapping[int, Iterable[int]],
) -> None:
    """Reject self-links, duplicates, nested composites, and any cycle."""

    if len(component_product_ids) != len(set(component_product_ids)):
        raise CompositeBOMError("同一组件不能在 BOM 中重复出现")
    if parent_product_id in component_product_ids:
        raise CompositeBOMError("组合品不能把自身作为组件")

    for component_id in component_product_ids:
        children = set(adjacency.get(component_id, ()))
        if children:
            raise CompositeBOMError("BOM 组件不能再是组合品（禁止嵌套 BOM）")

        pending = [component_id]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if current == parent_product_id:
                raise CompositeBOMError("BOM 关系会形成循环引用")
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency.get(current, ()))


def is_composite_product(product: Product | None) -> bool:
    return bool(product is not None and getattr(product, "is_composite", False))


def order_selectable_product_condition():
    """Return the authoritative product-picker visibility predicate.

    A BOM child stays editable in customer master data.  It is hidden from the
    order picker only when every active BOM parent uses the same inventory code,
    because the parent is then the single selectable order line.  A child with
    no active parent or at least one distinct-code parent remains selectable,
    regardless of its document display mode or legacy internal-component flag.
    Downstream workflows continue to use immutable order BOM snapshots and are
    not filtered by this predicate.
    """

    bom_model, _snapshot_model = _models()
    relation_parent = aliased(Product)
    active_parent_relation = exists(
        select(1)
        .select_from(bom_model)
        .join(
            relation_parent,
            relation_parent.id == bom_model.parent_product_id,
        )
        .where(
            bom_model.component_product_id == Product.id,
            relation_parent.is_active.is_(True),
        )
    )
    distinct_parent = aliased(Product)
    distinct_code_relation = exists(
        select(1)
        .select_from(bom_model)
        .join(
            distinct_parent,
            distinct_parent.id == bom_model.parent_product_id,
        )
        .where(
            bom_model.component_product_id == Product.id,
            distinct_parent.is_active.is_(True),
            func.lower(func.trim(distinct_parent.product_code))
            != func.lower(func.trim(Product.product_code)),
        )
    )
    return or_(
        ~active_parent_relation,
        distinct_code_relation,
    )


def _active_bom_rows(db: Session, parent_product_id: int | None = None) -> list[Any]:
    bom_model, _snapshot_model = _models()
    parent_field = _column_name(bom_model, "parent_product_id", "product_id")
    statement = select(bom_model)
    active_field = _column_name(bom_model, "is_active", required=False)
    if active_field is not None:
        statement = statement.where(getattr(bom_model, active_field).is_(True))
    if parent_product_id is not None:
        statement = statement.where(
            getattr(bom_model, parent_field) == parent_product_id
        )
    sort_field = _column_name(
        bom_model,
        "sort_order",
        "display_order",
        "sequence",
        "sequence_no",
        "component_sequence",
        required=False,
    )
    if sort_field is not None:
        statement = statement.order_by(getattr(bom_model, sort_field), bom_model.id)
    else:
        statement = statement.order_by(bom_model.id)
    return list(db.scalars(statement).all())


def _bom_row_values(row: Any, *, parent_code: str, fallback_position: int) -> dict[str, Any]:
    position = int(
        _mapped_value(
            row,
            "sort_order",
            "display_order",
            "sequence",
            "sequence_no",
            "component_sequence",
            default=fallback_position,
        )
        or fallback_position
    )
    return {
        "id": getattr(row, "id", None),
        "parent_product_id": int(
            _mapped_value(row, "parent_product_id", "product_id")
        ),
        "component_product_id": int(_mapped_value(row, "component_product_id")),
        "quantity_per_set": _as_decimal(
            _mapped_value(
                row,
                "quantity_per_set",
                "component_quantity",
                "quantity",
                "qty_per_set",
            )
        ),
        "display_order": position,
        "sort_order": position,
        "internal_component_code": _mapped_value(
            row,
            "internal_code",
            "internal_component_code",
            "component_code",
            default=internal_component_code(parent_code, position),
        )
        or internal_component_code(parent_code, position),
        "is_die_cut": bool(_mapped_value(row, "is_die_cut", default=False)),
        "die_cut_path": _mapped_value(row, "die_cut_path"),
        "mold_tool_id": _mapped_value(row, "mold_tool_id"),
        "mold_max_yield_per_sheet": _mapped_value(
            row, "mold_max_yield_per_sheet"
        ),
        "spare_sheet_quantity": int(
            _mapped_value(row, "spare_sheet_quantity", default=0) or 0
        ),
        "display_mode": _mapped_value(
            row, "display_mode", default="internal_only"
        ),
        "show_on_delivery": bool(
            _mapped_value(row, "show_on_delivery", default=True)
        ),
        "is_required": bool(_mapped_value(row, "is_required", default=True)),
        "remark": _mapped_value(row, "remark"),
    }


def _component_product_summary(product: Product) -> dict[str, Any]:
    spec = product_dimension_specification(product)
    return {
        "id": product.id,
        "customer_id": product.customer_id,
        "product_code": product.product_code,
        "product_name": product.product_name,
        "specification": spec,
        "material": product.legacy_material_text,
        "box_category": product.box_category,
        "production_process": product.production_process,
        "production_notes": product.production_notes,
        "mold_tool_id": product.mold_tool_id,
        "version": product.version,
        "is_active": product.is_active,
        "is_composite": bool(getattr(product, "is_composite", False)),
        "is_virtual_composite_parent": bool(
            getattr(product, "is_virtual_composite_parent", False)
        ),
        "is_internal_component": bool(
            getattr(product, "is_internal_component", False)
        ),
    }


def get_product_bom(db: Session, parent_product_id: int) -> dict[str, Any]:
    parent = db.get(Product, parent_product_id)
    if parent is None:
        raise CompositeBOMError("产品不存在", 404)
    rows = _active_bom_rows(db, parent_product_id)
    values = [
        _bom_row_values(
            row,
            parent_code=parent.product_code,
            fallback_position=index,
        )
        for index, row in enumerate(rows, start=1)
    ]
    component_ids = [value["component_product_id"] for value in values]
    products = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_(component_ids))
        ).all()
    } if component_ids else {}
    components: list[dict[str, Any]] = []
    for value in values:
        component = products.get(value["component_product_id"])
        components.append(
            {
                **value,
                "component": (
                    _component_product_summary(component) if component is not None else None
                ),
            }
        )
    return {
        "parent_product_id": parent.id,
        "parent_product_code": parent.product_code,
        "version": parent.version,
        "is_composite": bool(getattr(parent, "is_composite", bool(components))),
        "is_virtual_composite_parent": bool(
            getattr(parent, "is_virtual_composite_parent", False)
        ),
        "composite_fulfillment_mode": str(
            getattr(parent, "composite_fulfillment_mode", "component_delivery")
            or "component_delivery"
        ),
        "components": components,
    }


def _graph(rows: Sequence[Any]) -> dict[int, set[int]]:
    adjacency: dict[int, set[int]] = {}
    for row in rows:
        parent_id = int(_mapped_value(row, "parent_product_id", "product_id"))
        component_id = int(_mapped_value(row, "component_product_id"))
        adjacency.setdefault(parent_id, set()).add(component_id)
    return adjacency


def _validate_die_cut_mold(
    db: Session,
    product: Product,
    *,
    position: int,
    is_die_cut: bool,
    mold_tool_id: int | None,
) -> MoldTool | None:
    if not is_die_cut:
        return None
    selected_mold_id = mold_tool_id or product.mold_tool_id
    mold = db.get(MoldTool, selected_mold_id) if selected_mold_id else None
    if mold is None or not mold.is_active:
        raise CompositeBOMError(
            f"第{position}个模切组件必须选择启用中的模具"
        )
    if str(getattr(mold, "identity_status", "legacy_unset")) == "frozen":
        customer_link_id = db.scalar(
            select(MoldToolCustomer.id).where(
                MoldToolCustomer.mold_tool_id == mold.id,
                MoldToolCustomer.customer_id == product.customer_id,
            )
        )
        if customer_link_id is None:
            raise CompositeBOMError(
                f"第{position}个模切组件所选模具尚未关联该产品客户，"
                "请先到模具档案保存适用客户",
                409,
                detail={
                    "code": "MOLD_CUSTOMER_NOT_ASSOCIATED",
                    "position": position,
                },
            )
    return mold


def _relation_kwargs(
    model: type[Any],
    *,
    parent_product_id: int,
    component_product_id: int,
    quantity_per_set: Decimal,
    position: int,
    internal_code: str,
    actor_id: int | None,
    relation: Mapping[str, Any],
) -> dict[str, Any]:
    semantic_values = [
        (("parent_product_id", "product_id"), parent_product_id),
        (("component_product_id",), component_product_id),
        (("quantity_per_set", "component_quantity", "quantity", "qty_per_set"), quantity_per_set),
        (("display_order", "sort_order", "sequence", "sequence_no", "component_sequence"), position),
        (("internal_code", "internal_component_code", "component_code"), internal_code),
        (("is_die_cut",), relation["is_die_cut"]),
        (("die_cut_path",), relation["die_cut_path"]),
        (("mold_tool_id",), relation["mold_tool_id"]),
        (("mold_max_yield_per_sheet",), relation["mold_max_yield_per_sheet"]),
        (("spare_sheet_quantity",), relation["spare_sheet_quantity"]),
        (("display_mode",), relation.get("display_mode", "internal_only")),
        (("show_on_delivery",), bool(relation.get("show_on_delivery", True))),
        (("is_required",), bool(relation.get("is_required", True))),
        (("remark",), relation["remark"]),
        (("is_active",), True),
        (("created_by", "created_by_user_id"), actor_id),
    ]
    kwargs: dict[str, Any] = {}
    for aliases, value in semantic_values:
        field = _column_name(model, *aliases, required=False)
        if field is not None:
            kwargs[field] = value
    for aliases, _value in semantic_values[:3]:
        if not any(alias in kwargs for alias in aliases):
            raise CompositeBOMError(
                f"{model.__name__} 缺少必需 BOM 字段：{' / '.join(aliases)}",
                500,
            )
    return kwargs


def replace_product_bom(
    db: Session,
    *,
    parent_product_id: int,
    components: Sequence[Mapping[str, Any]],
    expected_version: int,
    user: User,
    change_reason: str | None = None,
) -> dict[str, Any]:
    """Atomically replace one parent BOM and advance the parent version."""

    bom_model, _snapshot_model = _models()
    parent = db.get(Product, parent_product_id)
    if parent is None:
        raise CompositeBOMError("产品不存在", 404)
    if int(parent.version) != expected_version:
        raise CompositeBOMError(
            "产品版本已变化，请刷新后重试",
            409,
            detail={
                "code": "MASTER_VERSION_CONFLICT",
                "expected_version": expected_version,
                "current_version": int(parent.version),
            },
        )
    if parent.deleted_at is not None or parent.purged_at is not None:
        raise CompositeBOMError("已删除产品不能维护 BOM", 409)
    if not parent.is_active:
        raise CompositeBOMError("已停用产品不能维护 BOM", 409)
    if bool(getattr(parent, "is_internal_component", False)):
        raise CompositeBOMError("内部组件不能同时作为组合品父产品")
    if len(components) > 99:
        raise CompositeBOMError("一个组合品最多允许99个组件")

    virtual_parent = bool(
        getattr(parent, "is_virtual_composite_parent", False)
    )
    fulfillment_mode = str(
        getattr(parent, "composite_fulfillment_mode", "component_delivery")
        or "component_delivery"
    )
    if fulfillment_mode not in {"parent_delivery", "component_delivery"}:
        raise CompositeBOMError("组合产品交付方式无效")
    if (
        getattr(parent, "combination_mode", "parent_priced_set")
        == "component_priced"
        and fulfillment_mode != "component_delivery"
    ):
        raise CompositeBOMError("组件分别计价时必须按子件交付")
    if fulfillment_mode == "component_delivery":
        # The parent is only a commercial/set identity in component-delivery
        # mode.  Labels belong to the physical child products, so retaining a
        # parent label policy would make the reported-items page ambiguous.
        parent.production_label_enabled = False
        parent.production_label_units_per_label = None
    if virtual_parent and not components:
        raise CompositeBOMError("虚拟组合套装至少需要一个真实组件")
    existing_rows = _active_bom_rows(db, parent.id)
    existing_by_component_id = {
        int(_mapped_value(row, "component_product_id")): row
        for row in existing_rows
    }

    normalized: list[dict[str, Any]] = []
    for position, component in enumerate(components, start=1):
        try:
            component_id = int(component["component_product_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise CompositeBOMError(f"第{position}个组件产品无效") from error
        normalized.append(
            {
                "component_product_id": component_id,
                "quantity_per_set": _as_decimal(
                    component.get("quantity_per_set"),
                    label=f"第{position}个组件用量",
                ),
                "display_order": position,
                "sort_order": position,
                "internal_component_code": internal_component_code(
                    parent.product_code, position
                ),
                "is_die_cut": (
                    False if virtual_parent else bool(component.get("is_die_cut", False))
                ),
                "mold_tool_id": (
                    None if virtual_parent else component.get("mold_tool_id")
                ),
                "mold_max_yield_per_sheet": (
                    None
                    if virtual_parent
                    else component.get("mold_max_yield_per_sheet")
                ),
                "spare_sheet_quantity": (
                    0
                    if virtual_parent
                    else int(component.get("spare_sheet_quantity", 0) or 0)
                ),
                "display_mode": (
                    "show_on_delivery"
                    if fulfillment_mode == "component_delivery"
                    else "internal_only"
                ),
                "show_on_delivery": fulfillment_mode == "component_delivery",
                "is_required": bool(component.get("is_required", True)),
                "remark": (str(component.get("remark") or "").strip() or None),
            }
        )
        if normalized[-1]["mold_tool_id"] is not None:
            try:
                normalized[-1]["mold_tool_id"] = int(
                    normalized[-1]["mold_tool_id"]
                )
            except (TypeError, ValueError) as error:
                raise CompositeBOMError(
                    f"第{position}个组件模具无效"
                ) from error
            if normalized[-1]["mold_tool_id"] <= 0:
                raise CompositeBOMError(f"第{position}个组件模具无效")
        if normalized[-1]["mold_max_yield_per_sheet"] is not None:
            try:
                normalized[-1]["mold_max_yield_per_sheet"] = int(
                    normalized[-1]["mold_max_yield_per_sheet"]
                )
            except (TypeError, ValueError) as error:
                raise CompositeBOMError(
                    f"第{position}个组件模切排版数无效"
                ) from error
            if normalized[-1]["mold_max_yield_per_sheet"] <= 0:
                raise CompositeBOMError(
                    f"第{position}个组件模切排版数必须大于0"
                )
        if normalized[-1]["spare_sheet_quantity"] < 0:
            raise CompositeBOMError(
                f"第{position}个组件加放片数不能小于0"
            )
        if normalized[-1]["display_mode"] not in {
            "internal_only",
            "show_on_delivery",
            "show_on_all_docs",
        }:
            raise CompositeBOMError(f"第{position}个组件单据显示方式无效")
        if not normalized[-1]["is_die_cut"] and (
            normalized[-1]["mold_tool_id"] is not None
            or normalized[-1]["mold_max_yield_per_sheet"] is not None
        ):
            raise CompositeBOMError(
                f"第{position}个非模切组件不能设置模具或模具最大产出"
            )

    component_ids = [row["component_product_id"] for row in normalized]
    all_rows = _active_bom_rows(db)
    adjacency = _graph(all_rows)
    # The current parent edges are being replaced, so they do not participate
    # in cycle/nesting validation of the proposed graph.
    adjacency.pop(parent.id, None)
    validate_component_graph(
        parent_product_id=parent.id,
        component_product_ids=component_ids,
        adjacency=adjacency,
    )

    products = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_(component_ids))
        ).all()
    } if component_ids else {}
    for position, component_id in enumerate(component_ids, start=1):
        component = products.get(component_id)
        if component is None:
            raise CompositeBOMError(f"第{position}个组件产品不存在")
        if component.customer_id != parent.customer_id:
            raise CompositeBOMError(f"第{position}个组件不属于父产品客户")
        if (
            not component.is_active
            or component.deleted_at is not None
            or component.purged_at is not None
        ):
            raise CompositeBOMError(f"第{position}个组件必须是启用中的产品")
        if bool(getattr(component, "is_composite", False)) or bool(
            getattr(component, "is_virtual_composite_parent", False)
        ):
            raise CompositeBOMError("BOM 组件不能再是组合品（禁止嵌套 BOM）")
        relation = normalized[position - 1]
        if virtual_parent:
            is_die_cut = bool(
                component.box_category == "die_cut"
                or component.mold_tool_id is not None
                or (component.die_cut_path or "").strip()
            )
            relation.update(
                {
                    "is_die_cut": is_die_cut,
                    "mold_tool_id": component.mold_tool_id if is_die_cut else None,
                    "mold_max_yield_per_sheet": (
                        cutting_factor(component.default_cutting_mode)
                        if is_die_cut
                        else None
                    ),
                    "spare_sheet_quantity": int(
                        _mapped_value(
                            existing_by_component_id.get(component_id),
                            "spare_sheet_quantity",
                            default=0,
                        )
                        or 0
                    ),
                    "display_mode": (
                        "show_on_delivery"
                        if relation["show_on_delivery"]
                        else "internal_only"
                    ),
                    "remark": None,
                }
            )
        mold = _validate_die_cut_mold(
            db,
            component,
            position=position,
            is_die_cut=relation["is_die_cut"],
            mold_tool_id=relation["mold_tool_id"],
        )
        relation.update(
            {
                "die_cut_path": (
                    component.die_cut_path if relation["is_die_cut"] else None
                ),
                "mold_tool_id": mold.id if mold is not None else None,
            }
        )

    before = get_product_bom(db, parent.id)
    comparison_fields = (
        "component_product_id",
        "quantity_per_set",
        "display_order",
        "internal_component_code",
        "is_die_cut",
        "die_cut_path",
        "mold_tool_id",
        "mold_max_yield_per_sheet",
        "spare_sheet_quantity",
        "display_mode",
        "show_on_delivery",
        "is_required",
        "remark",
    )

    def comparison_value(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(row.get(field) for field in comparison_fields)

    if (
        bool(before["is_composite"]) == bool(normalized)
        and [comparison_value(row) for row in before["components"]]
        == [comparison_value(row) for row in normalized]
    ):
        return before

    old_component_ids = set(existing_by_component_id)
    removed_rows = [
        row
        for component_id, row in existing_by_component_id.items()
        if component_id not in set(component_ids)
    ]
    product_updates: dict[str, Any] = {}
    if hasattr(Product, "is_composite"):
        product_updates["is_composite"] = bool(normalized)
    apply_versioned_update(
        db,
        object_type="product",
        entity=parent,
        updates=product_updates,
        expected_version=expected_version,
        user=user,
        reason=(change_reason or "").strip() or None,
        source="api.products.bom",
        action="bom_update",
        force_version=True,
    )

    for row in removed_rows:
        db.delete(row)
    db.flush()

    display_field = _column_name(
        bom_model,
        "display_order",
        "sort_order",
        "sequence",
        "sequence_no",
        "component_sequence",
    )
    kept_rows = [
        row
        for component_id, row in existing_by_component_id.items()
        if component_id in set(component_ids)
    ]
    staging_start = max(
        [int(getattr(row, display_field) or 0) for row in existing_rows] + [99]
    ) + 100
    for offset, row in enumerate(kept_rows):
        setattr(row, display_field, staging_start + offset)
    if kept_rows:
        db.flush()

    for row in normalized:
        values = _relation_kwargs(
            bom_model,
            parent_product_id=parent.id,
            component_product_id=row["component_product_id"],
            quantity_per_set=row["quantity_per_set"],
            position=row["display_order"],
            internal_code=row["internal_component_code"],
            actor_id=user.id,
            relation=row,
        )
        relation_row = existing_by_component_id.get(row["component_product_id"])
        if relation_row is None:
            relation_row = bom_model(**values)
            db.add(relation_row)
        else:
            for field, value in values.items():
                setattr(relation_row, field, value)
    db.flush()

    if hasattr(parent, "is_composite"):
        parent.is_composite = bool(normalized)
    for component in products.values():
        if hasattr(component, "is_internal_component"):
            component.is_internal_component = True
    removed_ids = old_component_ids - set(component_ids)
    if removed_ids and hasattr(Product, "is_internal_component"):
        remaining_component_ids = {
            int(_mapped_value(row, "component_product_id"))
            for row in _active_bom_rows(db)
        }
        for removed in db.scalars(
            select(Product).where(Product.id.in_(removed_ids))
        ).all():
            if removed.id not in remaining_component_ids:
                removed.is_internal_component = False
    db.flush()

    after = get_product_bom(db, parent.id)
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action="BOM_UPDATE",
            resource="master_data.product_bom",
            entity_type="product",
            entity_id=parent.id,
            description="更新组合品 BOM",
            details=json.dumps(
                {
                    "parent_product_id": parent.id,
                    "from_version": expected_version,
                    "to_version": parent.version,
                    "reason": (change_reason or "").strip() or None,
                    "before_components": before["components"],
                    "after_components": after["components"],
                },
                ensure_ascii=False,
                default=str,
            ),
        )
    )
    db.flush()
    return after


def _snapshot_kwargs(
    model: type[Any],
    *,
    order_item: OrderItem,
    parent: Product,
    component: Product,
    relation: Mapping[str, Any],
) -> dict[str, Any]:
    required_quantity = Decimal(int(order_item.quantity)) * relation["quantity_per_set"]
    selected_mold = relation.get("_mold_tool")
    if (
        selected_mold is None
        and relation.get("mold_tool_id") == component.mold_tool_id
    ):
        selected_mold = component.mold_tool
    spec = product_dimension_specification(component)
    semantic_values = [
        (("sales_order_item_id", "order_item_id"), order_item.id),
        (("parent_product_id",), parent.id),
        (("component_product_id",), component.id),
        (("parent_product_version",), parent.version),
        (("component_product_version", "snapshot_product_version", "product_version"), component.version),
        (("snapshot_schema_version",), 4),
        (("quantity_per_set", "component_quantity", "qty_per_set"), relation["quantity_per_set"]),
        (("required_piece_quantity", "required_quantity", "total_component_quantity", "snapshot_quantity"), required_quantity),
        (("display_order", "sort_order", "sequence", "sequence_no", "component_sequence"), relation["display_order"]),
        (("internal_code", "internal_component_code", "component_code"), relation["internal_component_code"]),
        (("order_set_quantity", "parent_quantity", "ordered_set_quantity", "ordered_quantity"), order_item.quantity),
        (("product_bom_component_id",), relation["id"]),
        (("is_die_cut",), relation["is_die_cut"]),
        (("snapshot_die_cut_path",), relation["die_cut_path"]),
        (("snapshot_mold_tool_id", "mold_tool_id"), relation["mold_tool_id"]),
        (("snapshot_mold_tool_code",), selected_mold.mold_code if selected_mold is not None else None),
        (("snapshot_mold_tool_name",), selected_mold.mold_name if selected_mold is not None else None),
        (("mold_max_yield_per_sheet",), relation["mold_max_yield_per_sheet"]),
        (("spare_sheet_quantity",), relation["spare_sheet_quantity"]),
        (("display_mode",), relation.get("display_mode", "internal_only")),
        (("show_on_delivery",), bool(relation.get("show_on_delivery", True))),
        (("is_required",), bool(relation.get("is_required", True))),
        (("remark",), relation["remark"]),
        (("snapshot_component_product_code", "snapshot_product_code", "component_product_code_snapshot", "snapshot_component_code"), component.product_code),
        (("snapshot_component_product_name", "snapshot_product_name", "component_product_name_snapshot", "snapshot_component_name"), component.product_name),
        (("snapshot_component_spec", "snapshot_spec", "component_spec_snapshot"), spec),
        (("snapshot_component_material", "snapshot_material", "component_material_snapshot"), component.material.code if component.material is not None else component.legacy_material_text),
        (("snapshot_component_material_id",), component.material_id),
        (("snapshot_component_supplier_name",), component.material.supplier_name if component.material is not None else None),
        (("snapshot_component_layer_count",), component.material.layer_count if component.material is not None else component.layer_count),
        (("snapshot_component_flute_type",), component.flute_type or (component.material.flute_type if component.material is not None else None)),
        (("snapshot_component_box_category",), component.box_category),
        (("snapshot_component_box_style",), component.box_style),
        (
            ("snapshot_component_default_cutting_mode",),
            (
                normalize_cutting_mode(component.default_cutting_mode)
                if (component.box_style or "").strip()
                in {"衬板", "平卡", "模切内盒", "隔板", "刀卡"}
                else DEFAULT_CUTTING_MODE
            ),
        ),
        (("snapshot_component_production_process", "snapshot_production_process", "component_process_snapshot"), component.production_process),
        (("snapshot_component_production_notes",), component.production_notes),
        (("snapshot_component_report_length_mm",), component.report_length_mm),
        (("snapshot_component_report_width_mm",), component.report_width_mm),
        (("snapshot_component_crease_type",), component.crease_type),
        (("snapshot_component_crease_left_mm",), component.crease_left_mm),
        (("snapshot_component_crease_middle_mm",), component.crease_middle_mm),
        (("snapshot_component_crease_right_mm",), component.crease_right_mm),
        (("snapshot_component_report_notes",), component.report_notes),
        (("snapshot_component_base_report_length_mm",), component.base_report_length_mm),
        (("snapshot_component_base_report_width_mm",), component.base_report_width_mm),
        (("snapshot_component_base_crease_type",), component.base_crease_type),
        (("snapshot_component_base_crease_left_mm",), component.base_crease_left_mm),
        (("snapshot_component_base_crease_middle_mm",), component.base_crease_middle_mm),
        (("snapshot_component_base_crease_right_mm",), component.base_crease_right_mm),
        (("snapshot_component_base_report_notes",), component.base_report_notes),
        (("snapshot_component_splice_mode",), component.splice_mode),
        (("snapshot_component_pieces_per_box",), component.pieces_per_box),
        (("snapshot_component_flap_mm",), component.flap_mm),
    ]
    kwargs: dict[str, Any] = {}
    for aliases, value in semantic_values:
        field = _column_name(model, *aliases, required=False)
        if field is not None:
            kwargs[field] = value
    required_aliases = (
        ("sales_order_item_id", "order_item_id"),
        ("component_product_id",),
        ("quantity_per_set", "component_quantity", "qty_per_set"),
        (
            "required_piece_quantity",
            "required_quantity",
            "total_component_quantity",
            "snapshot_quantity",
        ),
    )
    for aliases in required_aliases:
        if not any(alias in kwargs for alias in aliases):
            raise CompositeBOMError(
                f"{model.__name__} 缺少必需快照字段：{' / '.join(aliases)}",
                500,
            )
    return kwargs


def _snapshot_rows(db: Session, order_item_id: int) -> list[Any]:
    _bom_model, snapshot_model = _models()
    item_field = _column_name(snapshot_model, "sales_order_item_id", "order_item_id")
    statement = select(snapshot_model).where(
        getattr(snapshot_model, item_field) == order_item_id
    )
    sort_field = _column_name(
        snapshot_model,
        "display_order",
        "sort_order",
        "sequence",
        "sequence_no",
        "component_sequence",
        required=False,
    )
    if sort_field is not None:
        statement = statement.order_by(getattr(snapshot_model, sort_field), snapshot_model.id)
    else:
        statement = statement.order_by(snapshot_model.id)
    return list(db.scalars(statement).all())


def _snapshot_response(row: Any, *, fallback_position: int) -> dict[str, Any]:
    internal_code = _mapped_value(
        row, "internal_code", "internal_component_code", "component_code"
    )
    display_order = _mapped_value(
        row,
        "display_order",
        "sort_order",
        "sequence",
        "sequence_no",
        "component_sequence",
        default=fallback_position,
    )
    required_piece_quantity = _mapped_value(
        row,
        "required_piece_quantity",
        "required_quantity",
        "total_component_quantity",
        "snapshot_quantity",
    )
    snapshot_product_code = _mapped_value(
        row,
        "snapshot_component_product_code",
        "snapshot_product_code",
        "component_product_code_snapshot",
        "snapshot_component_code",
    )
    snapshot_product_name = _mapped_value(
        row,
        "snapshot_component_product_name",
        "snapshot_product_name",
        "component_product_name_snapshot",
        "snapshot_component_name",
    )
    snapshot_spec = _mapped_value(
        row, "snapshot_component_spec", "snapshot_spec", "component_spec_snapshot"
    )
    snapshot_material = _mapped_value(
        row,
        "snapshot_component_material",
        "snapshot_material",
        "component_material_snapshot",
    )
    snapshot_mold_tool_id = _mapped_value(
        row, "snapshot_mold_tool_id", "mold_tool_id"
    )
    snapshot_mold_tool_code = _mapped_value(row, "snapshot_mold_tool_code")
    snapshot_mold_tool_name = _mapped_value(row, "snapshot_mold_tool_name")
    return {
        "id": getattr(row, "id", None),
        "product_bom_component_id": _mapped_value(
            row, "product_bom_component_id"
        ),
        "component_product_id": _mapped_value(row, "component_product_id"),
        "internal_component_code": internal_code,
        "internal_code": internal_code,
        "display_order": display_order,
        "sort_order": display_order,
        "quantity_per_set": _mapped_value(
            row, "quantity_per_set", "component_quantity", "qty_per_set"
        ),
        "required_piece_quantity": required_piece_quantity,
        "required_quantity": required_piece_quantity,
        "order_set_quantity": _mapped_value(
            row, "order_set_quantity", "parent_quantity", "ordered_set_quantity", "ordered_quantity"
        ),
        "snapshot_component_product_code": snapshot_product_code,
        "snapshot_product_code": snapshot_product_code,
        "product_code": snapshot_product_code,
        "code": snapshot_product_code,
        "snapshot_component_product_name": snapshot_product_name,
        "snapshot_product_name": snapshot_product_name,
        "product_name": snapshot_product_name,
        "name": snapshot_product_name,
        "snapshot_component_spec": snapshot_spec,
        "snapshot_spec": snapshot_spec,
        "snapshot_component_material": snapshot_material,
        "snapshot_material": snapshot_material,
        "snapshot_component_material_id": _mapped_value(
            row, "snapshot_component_material_id"
        ),
        "snapshot_component_report_length_mm": _mapped_value(
            row, "snapshot_component_report_length_mm"
        ),
        "snapshot_component_report_width_mm": _mapped_value(
            row, "snapshot_component_report_width_mm"
        ),
        "snapshot_component_box_style": _mapped_value(
            row, "snapshot_component_box_style"
        ),
        "snapshot_component_default_cutting_mode": _mapped_value(
            row,
            "snapshot_component_default_cutting_mode",
            default="一开一",
        )
        or "一开一",
        "snapshot_product_version": _mapped_value(
            row, "snapshot_product_version", "component_product_version", "product_version"
        ),
        "is_die_cut": bool(_mapped_value(row, "is_die_cut", default=False)),
        "snapshot_mold_tool_id": snapshot_mold_tool_id,
        "mold_tool_id": snapshot_mold_tool_id,
        "snapshot_mold_tool_code": snapshot_mold_tool_code,
        "mold_tool_code": snapshot_mold_tool_code,
        "snapshot_mold_tool_name": snapshot_mold_tool_name,
        "mold_tool_name": snapshot_mold_tool_name,
        "mold_max_yield_per_sheet": _mapped_value(
            row, "mold_max_yield_per_sheet"
        ),
        "spare_sheet_quantity": int(
            _mapped_value(row, "spare_sheet_quantity", default=0) or 0
        ),
        "display_mode": _mapped_value(
            row, "display_mode", default="internal_only"
        ),
        "show_on_delivery": bool(
            _mapped_value(row, "show_on_delivery", default=True)
        ),
        "is_required": bool(_mapped_value(row, "is_required", default=True)),
        "remark": _mapped_value(row, "remark"),
        "snapshot_component_supplier_name": _mapped_value(
            row, "snapshot_component_supplier_name"
        ),
        "snapshot_component_layer_count": _mapped_value(
            row, "snapshot_component_layer_count"
        ),
        "snapshot_component_flute_type": _mapped_value(
            row, "snapshot_component_flute_type"
        ),
    }


def get_order_item_bom_components_by_item_ids(
    db: Session,
    order_item_ids: Iterable[int],
) -> dict[int, list[dict[str, Any]]]:
    """Load immutable BOM previews for many order items with one query."""

    item_ids = sorted({int(item_id) for item_id in order_item_ids})
    if not item_ids:
        return {}
    _bom_model, snapshot_model = _models()
    item_field = _column_name(snapshot_model, "sales_order_item_id", "order_item_id")
    sort_field = _column_name(
        snapshot_model,
        "display_order",
        "sort_order",
        "sequence",
        "sequence_no",
        "component_sequence",
        required=False,
    )
    from app.models.product_bom import SalesOrderItemBomDemandAdjustment

    adjustment_totals = (
        select(
            SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id.label(
                "snapshot_id"
            ),
            func.coalesce(
                func.sum(
                    SalesOrderItemBomDemandAdjustment.delta_order_set_quantity
                ),
                0,
            ).label("delta_sets"),
            func.coalesce(
                func.sum(
                    SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
                ),
                0,
            ).label("delta_pieces"),
        )
        .group_by(
            SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
        )
        .subquery()
    )
    statement = (
        select(
            snapshot_model,
            func.coalesce(adjustment_totals.c.delta_sets, 0),
            func.coalesce(adjustment_totals.c.delta_pieces, 0),
        )
        .outerjoin(
            adjustment_totals,
            adjustment_totals.c.snapshot_id == snapshot_model.id,
        )
        .where(getattr(snapshot_model, item_field).in_(item_ids))
    )
    if sort_field is not None:
        statement = statement.order_by(
            getattr(snapshot_model, item_field),
            getattr(snapshot_model, sort_field),
            snapshot_model.id,
        )
    else:
        statement = statement.order_by(
            getattr(snapshot_model, item_field), snapshot_model.id
        )
    result: dict[int, list[dict[str, Any]]] = {item_id: [] for item_id in item_ids}
    for row, delta_sets, delta_pieces in db.execute(statement).all():
        item_id = int(_mapped_value(row, "sales_order_item_id", "order_item_id"))
        components = result.setdefault(item_id, [])
        component = _snapshot_response(row, fallback_position=len(components) + 1)
        component["effective_order_set_quantity"] = (
            int(component["order_set_quantity"]) + int(delta_sets or 0)
        )
        component["effective_required_piece_quantity"] = (
            int(component["required_piece_quantity"]) + int(delta_pieces or 0)
        )
        components.append(component)
    return result


def create_order_item_bom_snapshots(
    db: Session,
    *,
    order_item: OrderItem,
    parent_product: Product,
) -> list[dict[str, Any]]:
    """Insert immutable component snapshots for one new composite parent line."""

    if not is_composite_product(parent_product):
        return []
    existing = _snapshot_rows(db, order_item.id)
    if existing:
        return [
            _snapshot_response(row, fallback_position=index)
            for index, row in enumerate(existing, start=1)
        ]

    _bom_model, snapshot_model = _models()
    relations = _active_bom_rows(db, parent_product.id)
    if not relations:
        raise CompositeBOMError("组合品没有有效 BOM，不能创建订单", 409)
    relation_values = [
        _bom_row_values(
            row,
            parent_code=parent_product.product_code,
            fallback_position=index,
        )
        for index, row in enumerate(relations, start=1)
    ]
    component_ids = [row["component_product_id"] for row in relation_values]
    products = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_(component_ids))
        ).all()
    }
    for position, relation in enumerate(relation_values, start=1):
        component = products.get(relation["component_product_id"])
        if (
            component is None
            or component.customer_id != parent_product.customer_id
            or not component.is_active
            or component.deleted_at is not None
            or component.purged_at is not None
            or is_composite_product(component)
            or bool(getattr(component, "is_virtual_composite_parent", False))
        ):
            raise CompositeBOMError(
                f"组合品第{position}个组件已失效，不能创建订单",
                409,
            )
        if not relation["is_die_cut"] and (
            relation["mold_tool_id"] is not None
            or relation["mold_max_yield_per_sheet"] is not None
        ):
            raise CompositeBOMError(
                f"组合品第{position}个非模切组件的模具配置无效，请先刷新 BOM",
                409,
            )
        mold = _validate_die_cut_mold(
            db,
            component,
            position=position,
            is_die_cut=bool(relation["is_die_cut"]),
            mold_tool_id=relation["mold_tool_id"],
        )
        relation["mold_tool_id"] = mold.id if mold is not None else None
        relation["_mold_tool"] = mold
        db.add(
            snapshot_model(
                **_snapshot_kwargs(
                    snapshot_model,
                    order_item=order_item,
                    parent=parent_product,
                    component=component,
                    relation=relation,
                )
            )
        )
    db.flush()
    return get_order_item_bom_preview(db, order_item.id)["components"]


def get_order_item_bom_preview(db: Session, order_item_id: int) -> dict[str, Any]:
    item = db.get(OrderItem, order_item_id)
    if item is None:
        raise CompositeBOMError("订单明细不存在", 404)
    rows = _snapshot_rows(db, order_item_id)
    components = [
        _snapshot_response(row, fallback_position=index)
        for index, row in enumerate(rows, start=1)
    ]
    if components:
        from app.services.composite_bom_workflow import effective_component_demands

        effective_by_snapshot = {
            demand.snapshot_id: demand
            for demand in effective_component_demands(db, order_item_id)
        }
        for component in components:
            demand = effective_by_snapshot.get(component["id"])
            if demand is None:
                continue
            component["effective_order_set_quantity"] = demand.effective_sets
            component["effective_required_piece_quantity"] = (
                demand.required_piece_quantity
            )
    return {
        "order_item_id": item.id,
        "parent_product_id": item.product_id,
        "ordered_sets": item.quantity,
        "effective_order_sets": item.quantity,
        "is_composite": bool(components),
        "components": components,
    }


def raise_http(error: CompositeBOMError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)
