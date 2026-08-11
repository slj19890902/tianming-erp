"""add direct external packaging profile to products

Revision ID: eb10v8x9z99
Revises: ea09v8x9z98
Create Date: 2026-08-11
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "eb10v8x9z99"
down_revision: Union[str, Sequence[str], None] = "ea09v8x9z98"
branch_labels = None
depends_on = None
NAMING_CONVENTION = {"ck": "ck_%(table_name)s_%(constraint_name)s"}
SUPPLIER_CATEGORIES = "'corrugated_board','paper_corner_guard','coated_board','printed_folding_carton','epe_cushion','hollow_board','other_packaging'"
EXTERNAL_CATEGORIES = "'paper_corner_guard','coated_board','printed_folding_carton','epe_cushion','hollow_board','other_packaging'"

def upgrade() -> None:
    with op.batch_alter_table("products", recreate="always") as batch:
        batch.add_column(sa.Column("supply_mode", sa.String(30), server_default="corrugated_production", nullable=False))
        batch.add_column(sa.Column("external_packaging_category_code", sa.String(50), nullable=True))
        batch.add_column(sa.Column("external_packaging_specification_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("external_packaging_specification_summary", sa.String(500), nullable=True))
        batch.add_column(sa.Column("external_packaging_purchase_unit", sa.String(20), nullable=True))
        batch.add_column(sa.Column("external_packaging_candidate_snapshot_json", sa.Text(), nullable=True))
        batch.create_check_constraint("ck_products_supply_mode", "supply_mode IN ('corrugated_production','external_purchase','mixed_bom')")
        batch.create_check_constraint(
            "ck_products_external_supply_profile",
            "((supply_mode = 'external_purchase' AND external_packaging_category_code IS NOT NULL AND external_packaging_specification_json IS NOT NULL AND external_packaging_specification_summary IS NOT NULL AND external_packaging_purchase_unit IS NOT NULL AND external_packaging_candidate_snapshot_json IS NOT NULL) OR (supply_mode <> 'external_purchase' AND external_packaging_category_code IS NULL AND external_packaging_specification_json IS NULL AND external_packaging_specification_summary IS NULL AND external_packaging_purchase_unit IS NULL AND external_packaging_candidate_snapshot_json IS NULL))",
        )
    with op.batch_alter_table("supplier_supply_categories", recreate="always") as batch:
        batch.drop_constraint("ck_supplier_supply_categories_code", type_="check")
        batch.create_check_constraint("ck_supplier_supply_categories_code", f"category_code IN ({SUPPLIER_CATEGORIES})")
    with op.batch_alter_table("external_packaging_products", recreate="always") as batch:
        batch.drop_constraint("ck_external_packaging_products_category", type_="check")
        batch.create_check_constraint("ck_external_packaging_products_category", f"category_code IN ({EXTERNAL_CATEGORIES})")

def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text("SELECT COUNT(*) FROM products WHERE supply_mode <> 'corrugated_production' OR external_packaging_category_code IS NOT NULL OR external_packaging_candidate_snapshot_json IS NOT NULL")).scalar_one()
    hollow_supplier = bind.execute(sa.text("SELECT COUNT(*) FROM supplier_supply_categories WHERE category_code='hollow_board'")).scalar_one()
    hollow_product = bind.execute(sa.text("SELECT COUNT(*) FROM external_packaging_products WHERE category_code='hollow_board'")).scalar_one()
    if used or hollow_supplier or hollow_product:
        raise RuntimeError("禁止破坏性降级：外购包材常用箱、中空板供应类别或产品目录已产生业务事实")
    with op.batch_alter_table("external_packaging_products", recreate="always") as batch:
        batch.drop_constraint("ck_external_packaging_products_category", type_="check")
        batch.create_check_constraint("ck_external_packaging_products_category", "category_code IN ('paper_corner_guard','coated_board','printed_folding_carton','epe_cushion','other_packaging')")
    with op.batch_alter_table("supplier_supply_categories", recreate="always") as batch:
        batch.drop_constraint("ck_supplier_supply_categories_code", type_="check")
        batch.create_check_constraint("ck_supplier_supply_categories_code", "category_code IN ('corrugated_board','paper_corner_guard','coated_board','printed_folding_carton','epe_cushion','other_packaging')")
    with op.batch_alter_table("products", recreate="always") as batch:
        batch.drop_constraint("ck_products_external_supply_profile", type_="check")
        batch.drop_constraint("ck_products_supply_mode", type_="check")
        batch.drop_column("external_packaging_candidate_snapshot_json")
        batch.drop_column("external_packaging_purchase_unit")
        batch.drop_column("external_packaging_specification_summary")
        batch.drop_column("external_packaging_specification_json")
        batch.drop_column("external_packaging_category_code")
        batch.drop_column("supply_mode")
