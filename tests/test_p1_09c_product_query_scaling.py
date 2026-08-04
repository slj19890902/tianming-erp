from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


def _fixture(tmp_path: Path, *, visible_count: int):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.product_drawing import ProductDrawing
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1_09c_product_scaling.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="VISIBLE",
            name="Visible Product Customer",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="HIDDEN",
            name="Hidden Product Customer",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username="p1-09c-product-workshop",
            password_hash="not-used-by-direct-endpoint-test",
            role="workshop",
            real_name="P1 product scope",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))

        for index in range(visible_count):
            material = Material(
                code=f"P1M{index:03d}",
                layer_count=5,
                flute_type="BC",
                supplier_name=f"Supplier {index}",
                basis_weight_description="140/110/110/110/140",
            )
            mold = MoldTool(
                mold_code=f"P1-MOLD-{index:03d}",
                mold_name=f"P1 mold {index}",
                rack_location=f"R-{index:03d}",
                is_active=True,
            )
            db.add_all([material, mold])
            db.flush()
            product = Product(
                customer_id=visible.id,
                product_code=f"P1-09C-P-{index:03d}",
                customer_material_code=f"P1-09C-P-{index:03d}",
                product_name=f"Visible product {index}",
                material_id=material.id,
                mold_tool_id=mold.id,
                length_mm=Decimal("500"),
                width_mm=Decimal("300"),
                height_mm=Decimal("200"),
                box_category="normal",
                sale_unit_price=Decimal("3.2"),
                cost_unit_price=Decimal("1.1"),
                board_price=Decimal("0.9"),
                suggested_price=Decimal("3.5"),
            )
            db.add(product)
            db.flush()
            db.add(
                ProductDrawing(
                    product_id=product.id,
                    image_path=f"drawings/p1-{index}.png",
                    thumbnail_path=f"drawings/p1-{index}-thumb.png",
                )
            )

        hidden_material = Material(code="P1-HIDDEN-M", layer_count=3)
        db.add(hidden_material)
        db.flush()
        db.add(
            Product(
                customer_id=hidden.id,
                product_code="P1-09C-P-HIDDEN",
                customer_material_code="P1-09C-P-HIDDEN",
                product_name="Hidden product",
                material_id=hidden_material.id,
                box_category="normal",
                cost_unit_price=Decimal("99"),
            )
        )
        db.commit()
        return engine, factory, user.id


def _read_and_count(factory, user_id: int):
    from app.api.products import list_products
    from app.models.user import User

    engine = factory.kw["bind"]
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            response = list_products(db=db, user=user, page=1, page_size=50)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def _read_summary_and_count(factory, user_id: int):
    from app.api.products import list_products
    from app.models.user import User

    engine = factory.kw["bind"]
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            response = list_products(
                db=db,
                user=user,
                customer_id=1,
                page=1,
                page_size=50,
                response_mode="summary",
            )
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def _select_count(statements: list[str]) -> int:
    return sum(statement.startswith("select") for statement in statements)


def test_product_list_scope_redacts_cost_and_keeps_list_golden(tmp_path: Path) -> None:
    _engine, factory, user_id = _fixture(tmp_path, visible_count=2)
    response, statements = _read_and_count(factory, user_id)

    assert response["total"] == 2
    assert [row["product_code"] for row in response["items"]] == [
        "P1-09C-P-000",
        "P1-09C-P-001",
    ]
    first = response["items"][0]
    assert first["material_code"] == "P1M000"
    assert first["material_supplier_name"] == "Supplier 0"
    assert first["mold_tool"]["mold_code"] == "P1-MOLD-000"
    assert len(first["drawings"]) == 1
    assert first["drawings"][0]["image_path"].endswith(".png")
    assert "cost_unit_price" not in first
    assert "board_price" not in first
    assert "suggested_price" not in first
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in statements
    )


def test_product_list_query_growth_is_bounded(
    tmp_path: Path,
) -> None:
    _engine, small_factory, small_user_id = _fixture(
        tmp_path / "small", visible_count=1
    )
    _engine, large_factory, large_user_id = _fixture(
        tmp_path / "large", visible_count=20
    )
    _small, small_sql = _read_and_count(small_factory, small_user_id)
    _large, large_sql = _read_and_count(large_factory, large_user_id)

    assert _select_count(large_sql) <= _select_count(small_sql) + 2


def test_product_summary_omits_editor_only_relations_and_keeps_list_contract(
    tmp_path: Path,
) -> None:
    _engine, factory, user_id = _fixture(tmp_path, visible_count=2)
    response, statements = _read_summary_and_count(factory, user_id)

    assert response["total"] == 2
    first = response["items"][0]
    assert first["product_code"] == "P1-09C-P-000"
    assert first["material_code"] == "P1M000"
    assert first["material_supplier_name"] == "Supplier 0"
    assert first["readiness"]["status"] in {"资料已完善", "待完善"}
    assert first["version"] == 1
    assert "drawings" not in first
    assert "mold_tool" not in first
    assert "report_notes" not in first
    assert "cost_unit_price" not in first
    assert _select_count(statements) <= 6
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in statements
    )
