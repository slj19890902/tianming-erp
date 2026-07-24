from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_now_naive
from app.models.historical_requisition import HistoricalRequisitionMap
from app.models.migration import MigrationEntityMap
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User


def has_historical_references(db: Session, product_id: int) -> bool:
    order_item_count = db.scalar(
        select(func.count())
        .select_from(OrderItem)
        .where(
            or_(
                OrderItem.product_id == product_id,
                OrderItem.combination_parent_product_id == product_id,
            )
        )
    )
    historical_map_count = db.scalar(
        select(func.count())
        .select_from(HistoricalRequisitionMap)
        .where(HistoricalRequisitionMap.product_id == product_id)
    )
    migration_map_count = db.scalar(
        select(func.count())
        .select_from(MigrationEntityMap)
        .where(
            MigrationEntityMap.target_id == product_id,
            or_(
                MigrationEntityMap.entity_type == "product",
                MigrationEntityMap.target_table == "products",
            ),
        )
    )
    return any(
        count and count > 0
        for count in (order_item_count, historical_map_count, migration_map_count)
    )


def move_to_trash(product: Product, user: User) -> None:
    product.is_active = False
    product.deleted_at = beijing_now_naive()
    product.deleted_by = user.id
    product.purged_at = None


def restore_from_trash(product: Product) -> None:
    product.is_active = True
    product.deleted_at = None
    product.deleted_by = None
    product.purged_at = None


def archive_purged_product(product: Product, user: User) -> None:
    product.is_active = False
    product.deleted_by = user.id
    product.purged_at = beijing_now_naive()
