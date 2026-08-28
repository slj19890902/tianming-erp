from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.api.products import ProductPayload, ProductResponse
from app.models import Base
from app.models.order import OrderItem
from app.models.product import Product
from app.services.master_data_versioning import _PRODUCT_FIELDS


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "cp72v8x9z61_composite_business_modes.py"
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _create_parent_schema(path: Path) -> None:
    """Build the direct-parent contract without replaying unrelated history."""
    from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table, UniqueConstraint
    from app.core.database import create_sqlite_engine

    engine = create_sqlite_engine(path)
    metadata = MetaData()
    Table("alembic_version", metadata, Column("version_num", String, primary_key=True))
    Table("products", metadata, Column("id", Integer, primary_key=True))
    Table(
        "sales_order_items", metadata, Column("id", Integer, primary_key=True),
        Column("order_id", Integer, nullable=False), Column("product_id", Integer, nullable=False),
        Column("quantity", Integer, nullable=False), Column("unit_price", Numeric, nullable=False),
        Column("subtotal", Numeric, nullable=False), Column("material_status", String, nullable=False),
        Column("snapshot_product_name", String, nullable=False),
        Column("requisition_status", String, nullable=False),
        Column("special_process", String, nullable=False),
    )
    Table(
        "order_item_semi_requirements", metadata,
        Column("id", Integer, primary_key=True), Column("order_item_id", Integer, nullable=False),
        Column("component_type", String, nullable=False),
        Column("sales_order_item_bom_component_id", Integer),
        UniqueConstraint("order_item_id", "component_type", name="uq_order_item_semi_requirements_item_component"),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO alembic_version(version_num) VALUES ('co71v8x9z60')")
    engine.dispose()


def _product_payload(**overrides):
    data = {
        "customer_id": 1,
        "product_code": "MODE-001",
        "customer_material_code": "MODE-001",
        "product_name": "组合方式测试箱",
        "box_category": "normal",
    }
    data.update(overrides)
    return ProductPayload(**data)


def test_combination_mode_defaults_and_api_validation_are_explicit() -> None:
    assert Product.__table__.c.combination_mode.default.arg == "parent_priced_set"
    assert Product.__table__.c.combination_mode.server_default.arg == "parent_priced_set"
    assert ProductPayload.model_fields["combination_mode"].default == "parent_priced_set"
    assert ProductResponse.model_fields["combination_mode"].default == "parent_priced_set"
    assert _product_payload().combination_mode == "parent_priced_set"
    assert _product_payload(combination_mode="component_priced").combination_mode == "component_priced"
    with pytest.raises(ValueError):
        _product_payload(combination_mode="display_mode")


def test_product_mode_is_versioned_and_order_history_fields_are_guarded() -> None:
    assert "combination_mode" in _PRODUCT_FIELDS
    assert {
        "combination_mode_snapshot",
        "combination_role",
        "combination_group_key",
        "combination_parent_product_id",
        "combination_parent_name_snapshot",
        "combination_set_quantity_snapshot",
        "combination_quantity_per_set_snapshot",
    } <= set(OrderItem.__table__.c.keys())
    assert {
        "ck_sales_order_items_priced_component_source",
        "ck_sales_order_items_standalone_without_combination_source",
        "ck_sales_order_items_set_parent_source",
    } <= {
        constraint.name for constraint in OrderItem.__table__.constraints
    }
    parent_fk = next(iter(OrderItem.__table__.c.combination_parent_product_id.foreign_keys))
    assert parent_fk.ondelete == "SET NULL"


def test_migration_is_linear_fail_closed_and_has_no_business_writes() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    compile(source, str(MIGRATION), "exec")
    assert 'revision: str = "cp72v8x9z61"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "co71v8x9z60"' in source
    assert "parent_priced_set" in source and "component_priced" in source
    assert "priced_component" in source
    assert "combination_role <> 'standalone'" in source
    assert "禁止破坏性降级" in source
    for forbidden in ("INSERT INTO", "UPDATE sales_order_items", "DELETE FROM"):
        assert forbidden not in source


def _alembic_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_migration_round_trips_empty_isolated_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.core.database import create_sqlite_engine

    path = tmp_path / "composite-modes.sqlite3"
    _create_parent_schema(path)
    config = _alembic_config(monkeypatch, path)
    command.upgrade(config, "cp72v8x9z61")
    columns = {
        column["name"]
        for column in inspect(create_sqlite_engine(path)).get_columns("sales_order_items")
    }
    assert {
        "combination_mode_snapshot",
        "combination_role",
        "combination_group_key",
        "combination_parent_product_id",
        "combination_parent_name_snapshot",
        "combination_set_quantity_snapshot",
        "combination_quantity_per_set_snapshot",
    } <= columns
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "cp72v8x9z61"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, "co71v8x9z60")
    assert "combination_role" not in {
        column["name"]
        for column in inspect(create_sqlite_engine(path)).get_columns("sales_order_items")
    }


def test_migration_rejects_invalid_provenance_and_fails_closed_with_any_new_fact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "composite-modes-fail-closed.sqlite3"
    _create_parent_schema(path)
    config = _alembic_config(monkeypatch, path)
    command.upgrade(config, "cp72v8x9z61")
    base_values = (
        "order_id, product_id, quantity, unit_price, subtotal, material_status, "
        "snapshot_product_name, requisition_status, special_process"
    )
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sales_order_items "
                f"({base_values}, combination_role, combination_group_key) "
                "VALUES (1, 1, 1, 1, 1, 'pending', '测试产品', '未报料', '无', "
                "'standalone', 'forged-group')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sales_order_items "
                f"({base_values}, combination_mode_snapshot, combination_role) "
                "VALUES (1, 1, 1, 1, 1, 'pending', '测试产品', '未报料', '无', "
                "'component_priced', 'set_parent')"
            )
        connection.execute(
            "INSERT INTO sales_order_items "
            f"({base_values}, combination_mode_snapshot, combination_role) "
            "VALUES (1, 1, 1, 1, 1, 'pending', '测试产品', '未报料', '无', "
            "'parent_priced_set', 'set_parent')"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, "co71v8x9z60")


def test_product_bom_editor_separates_pricing_from_fulfillment_display_mode() -> None:
    bom_start = INDEX.index('<fieldset class="bom-editor-panel"')
    bom_end = INDEX.index("</fieldset>", bom_start) + len("</fieldset>")
    bom_block = INDEX[bom_start:bom_end]
    assert 'v-model="productForm.combination_mode"' in bom_block
    assert 'value="parent_priced_set"' in bom_block
    assert 'value="component_priced"' in bom_block
    assert 'combination_mode: "parent_priced_set"' in INDEX
    assert "combination_mode: f.combination_mode" in INDEX
    # P1-79 将客户单据展示范围提升为父件交付方式，并在保存 BOM 时统一落到组件；
    # 它仍必须与组合计价方式完全分离，不能把 combination_mode 写入 display_mode。
    assert "component.display_mode" in INDEX
    assert (
        'display_mode: this.productForm.composite_fulfillment_mode === "component_delivery" '
        '? "show_on_delivery" : "internal_only"'
    ) in INDEX
    assert (
        'show_on_delivery: this.productForm.composite_fulfillment_mode === "component_delivery"'
    ) in INDEX
    assert "display_mode: f.combination_mode" not in INDEX
