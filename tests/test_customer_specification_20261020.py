"""Customer wording is editable presentation, never a physical stock identity."""
import importlib.util
import json
import sqlite3
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from app.api.products import ProductPayload, _product_write_data
from app.models.product import Product
from app.services.product_specification import delivery_specification, resolved_order_specification
from app.services.finished_stock_identity import product_basis
from app.services.master_data_versioning import (
    apply_versioned_update, preview_versioned_update, revision_snapshot, snapshot_updates,
)
from desktop_assistant.reader_contract import target_readers
from test_master_data_versioning_p4_core import db, _customer, _user

ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_TEXT = "115*67*2.5/2.8cm 10本/盒 T5P/A THH10请在灰色处打勾"


def test_picker_summary_returns_customer_wording_instead_of_losing_it(tmp_path):
    from tests.test_p1_09c_product_query_scaling import _fixture
    from app.api.products import list_products
    from app.models.user import User
    from sqlalchemy import select
    engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    try:
        with factory() as session:
            product = session.scalar(select(Product).where(Product.product_code == "P1-09C-P-000"))
            product.customer_specification = CUSTOMER_TEXT
            session.commit()
            response = list_products(db=session, user=session.get(User, user_id),
                customer_id=product.customer_id, page=1, page_size=50,
                selection_context="order", response_mode="summary")
            assert response["items"][0]["customer_specification"] == CUSTOMER_TEXT
    finally:
        engine.dispose()


def test_order_editor_keeps_customer_text_and_preserves_quantity_price_and_inventory():
    node = shutil.which("node")
    assert node, "Node.js is required for the isolated frontend test"
    result = subprocess.run([node, str(ROOT / "tests/customer_specification.cjs"), str(ROOT)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_customer_text_changes_display_without_changing_physical_identity(db):
    from app.models.multilevel_bom import ProductBomProfile
    ProductBomProfile.__table__.create(db.get_bind())
    customer = _customer(db, "customer-spec")
    product = Product(customer_id=customer.id, product_code="21311299",
        customer_material_code="21311299", product_name="盒",
        length_mm=1150, width_mm=670, height_mm=25, box_category="normal", unit="只")
    db.add(product)
    db.flush()
    before = product_basis(product)
    assert delivery_specification(product) == "1150×670×25mm"
    product.customer_specification = CUSTOMER_TEXT
    assert delivery_specification(product) == CUSTOMER_TEXT
    assert resolved_order_specification(None, product) == CUSTOMER_TEXT
    assert resolved_order_specification("已冻结规格", product) == "已冻结规格"
    assert product_basis(product) == before
    product.customer_specification = ""
    assert delivery_specification(product) == ""
    assert product_basis(product) == before
    product.customer_specification = None
    assert delivery_specification(product) == "1150×670×25mm"


def test_old_product_payload_does_not_erase_customer_text_and_new_payload_validates():
    args = dict(customer_id=5, product_code="21311299", customer_material_code="21311299",
        product_name="盒", box_category="normal", box_style="A1", unit="只")
    admin = SimpleNamespace(role="admin", is_active=True)
    assert "customer_specification" not in _product_write_data(ProductPayload(**args), admin)
    for value in (None, "", CUSTOMER_TEXT):
        data = _product_write_data(ProductPayload(**args, customer_specification=value), admin)
        assert data["customer_specification"] == value
    with pytest.raises(ValueError):
        ProductPayload(**args, customer_specification="x" * 1001)


def test_customer_text_version_has_cas_audit_and_restorable_value(db):
    customer = _customer(db, "spec-history")
    actor = _user(db, "spec-admin")
    product = Product(customer_id=customer.id, product_code="P-SPEC",
        customer_material_code="P-SPEC", product_name="盒", box_category="normal", unit="只")
    db.add(product)
    db.flush()
    version = product.version
    updates = {"customer_specification": CUSTOMER_TEXT}
    preview = preview_versioned_update(db, object_type="product", entity=product,
        updates=updates, expected_version=version, user=actor)
    saved = apply_versioned_update(db, object_type="product", entity=product,
        updates=updates, expected_version=version, user=actor, reason="客户原表核实",
        source="test.customer_specification", confirmation_token=preview["confirmation_token"])
    assert revision_snapshot(saved)["customer_specification"] == CUSTOMER_TEXT
    assert snapshot_updates("product", saved)["customer_specification"] == CUSTOMER_TEXT
    assert saved.operation_log_id is not None
    with pytest.raises(HTTPException) as error:
        apply_versioned_update(db, object_type="product", entity=product,
            updates={"customer_specification": "另一规格"}, expected_version=version,
            user=actor, reason="过期保存", source="test.customer_specification")
    assert error.value.status_code == 409
    assert product.customer_specification == CUSTOMER_TEXT


@pytest.mark.parametrize("value", ["", CUSTOMER_TEXT])
def test_existing_customer_spec_requires_capable_reader(tmp_path, value):
    database = tmp_path / "reader.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE products (id INTEGER, customer_specification TEXT)")
        conn.execute("INSERT INTO products VALUES (1, ?)", (value,))
    with pytest.raises(ValueError, match="reader"):
        target_readers(database, {}, {}, None, None, factory=False)
    target_readers(database, {}, {"reader_capabilities": {"customer_specification_v1": 1}},
        None, None, factory=False)
    # A signed newer source still requires the reader even if current masters are cleared.
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE products SET customer_specification=NULL")
    with pytest.raises(ValueError, match="reader"):
        target_readers(database, {"reader_capabilities": {"customer_specification_v1": 1}},
            {}, None, None, factory=False)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE master_data_object_versions (object_type TEXT, snapshot_json TEXT)")
        conn.execute("INSERT INTO master_data_object_versions VALUES ('product', ?)",
            (json.dumps({"customer_specification": value}),))
    with pytest.raises(ValueError, match="reader"):
        target_readers(database, {}, {}, None, None, factory=False)


def test_migration_preserves_rows_and_refuses_loss_of_current_or_historical_text():
    spec = importlib.util.spec_from_file_location(
        "customer_spec_migration", ROOT / "alembic/versions/eu1020cs_customer_specification.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, length_mm NUMERIC)"))
        conn.execute(text("INSERT INTO products VALUES (328, 890)"))
        conn.execute(text("CREATE TABLE master_data_object_versions (object_type TEXT, snapshot_json TEXT)"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert conn.execute(text("SELECT * FROM products")).one() == (328, 890, None)
            conn.execute(text("UPDATE products SET customer_specification=''"))
            with pytest.raises(RuntimeError, match="禁止有损降级"):
                module.downgrade()
            conn.execute(text("UPDATE products SET customer_specification=NULL"))
            conn.execute(text("INSERT INTO master_data_object_versions VALUES ('product', :snapshot)"),
                {"snapshot": json.dumps({"customer_specification": CUSTOMER_TEXT})})
            with pytest.raises(RuntimeError, match="历史版本"):
                module.downgrade()
            # Only this synthetic test fixture is emptied to test a genuinely unused column.
            conn.execute(text("DELETE FROM master_data_object_versions"))
            module.downgrade()
            assert conn.execute(text("SELECT * FROM products")).one() == (328, 890)
    engine.dispose()
