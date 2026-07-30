"""supplier master and aliases

Revision ID: cw79v8x9z68
Revises: cv78v8x9z67
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cw79v8x9z68"
down_revision: Union[str, Sequence[str], None] = "cv78v8x9z67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUPPLIERS = (
    (1, "苏州嘉林亿", "苏州嘉林亿", "嘉林亿", True, 10),
    (2, "昆山鸣朋", "昆山鸣朋", "鸣朋", True, 20),
    (3, "苏州佳丰", "苏州佳丰", "佳丰", False, 90),
    (4, "胜源", "胜源", "胜源", True, 30),
    (5, "森林阳光", "森林阳光", "森林阳光", True, 40),
)

ALIASES = (
    (1, 1, "嘉林亿", "嘉林亿"),
    (2, 1, "苏州嘉林亿包装科技有限公司", "苏州嘉林亿包装科技有限公司"),
    (3, 2, "鸣朋", "鸣朋"),
    (4, 2, "昆山鸣朋纸板", "昆山鸣朋纸板"),
    (5, 2, "昆山鸣朋纸业有限公司", "昆山鸣朋纸业有限公司"),
    (6, 3, "佳丰", "佳丰"),
    (7, 3, "苏州佳丰纸业有限公司", "苏州佳丰纸业有限公司"),
)


def upgrade() -> None:
    op.create_table(
        "supplier_master_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("standard_name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("business_code", sa.String(length=50), nullable=True),
        sa.Column("normalized_business_code", sa.String(length=50), nullable=True),
        sa.Column("contact_name", sa.String(length=100), nullable=True),
        sa.Column("phone", sa.String(length=100), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_supplier_master_records_version",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_supplier_master_records_sort_order",
        ),
        sa.UniqueConstraint(
            "normalized_name",
            name="uq_supplier_master_records_normalized_name",
        ),
        sa.UniqueConstraint(
            "normalized_business_code",
            name="uq_supplier_master_records_normalized_business_code",
        ),
    )
    op.create_index(
        "ix_supplier_master_records_is_active",
        "supplier_master_records",
        ["is_active"],
    )
    op.create_table(
        "supplier_master_aliases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("alias_name", sa.String(length=200), nullable=False),
        sa.Column("normalized_alias", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["supplier_master_records.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "normalized_alias",
            name="uq_supplier_master_aliases_normalized_alias",
        ),
    )
    op.create_index(
        "ix_supplier_master_aliases_supplier_id",
        "supplier_master_aliases",
        ["supplier_id"],
    )

    supplier_table = sa.table(
        "supplier_master_records",
        sa.column("id", sa.Integer()),
        sa.column("standard_name", sa.String()),
        sa.column("normalized_name", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("version", sa.Integer()),
    )
    op.bulk_insert(
        supplier_table,
        [
            {
                "id": item[0],
                "standard_name": item[1],
                "normalized_name": item[2],
                "display_name": item[3],
                "is_active": item[4],
                "sort_order": item[5],
                "version": 1,
            }
            for item in SUPPLIERS
        ],
    )
    alias_table = sa.table(
        "supplier_master_aliases",
        sa.column("id", sa.Integer()),
        sa.column("supplier_id", sa.Integer()),
        sa.column("alias_name", sa.String()),
        sa.column("normalized_alias", sa.String()),
    )
    op.bulk_insert(
        alias_table,
        [
            {
                "id": item[0],
                "supplier_id": item[1],
                "alias_name": item[2],
                "normalized_alias": item[3],
            }
            for item in ALIASES
        ],
    )


def downgrade() -> None:
    connection = op.get_bind()
    current_suppliers = connection.execute(
        sa.text(
            """
            SELECT id, standard_name, normalized_name, display_name,
                   is_active, version, business_code, contact_name,
                   phone, remarks, sort_order
            FROM supplier_master_records
            ORDER BY id
            """
        )
    ).mappings().all()
    expected_suppliers = [
        {
            "id": item[0],
            "standard_name": item[1],
            "normalized_name": item[2],
            "display_name": item[3],
            "is_active": item[4],
            "version": 1,
            "business_code": None,
            "contact_name": None,
            "phone": None,
            "remarks": None,
            "sort_order": item[5],
        }
        for item in SUPPLIERS
    ]
    current_aliases = connection.execute(
        sa.text(
            """
            SELECT id, supplier_id, alias_name, normalized_alias
            FROM supplier_master_aliases
            ORDER BY id
            """
        )
    ).mappings().all()
    expected_aliases = [
        {
            "id": item[0],
            "supplier_id": item[1],
            "alias_name": item[2],
            "normalized_alias": item[3],
        }
        for item in ALIASES
    ]
    audit_exists = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM operation_logs
            WHERE resource = 'Supplier' OR entity_type = 'supplier'
            LIMIT 1
            """
        )
    ).first()
    if (
        [dict(row) for row in current_suppliers] != expected_suppliers
        or [dict(row) for row in current_aliases] != expected_aliases
        or audit_exists is not None
    ):
        raise RuntimeError(
            "供应商主档已产生业务事实或审计记录，禁止破坏性降级"
        )

    op.drop_index(
        "ix_supplier_master_aliases_supplier_id",
        table_name="supplier_master_aliases",
    )
    op.drop_table("supplier_master_aliases")
    op.drop_index(
        "ix_supplier_master_records_is_active",
        table_name="supplier_master_records",
    )
    op.drop_table("supplier_master_records")
