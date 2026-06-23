from decimal import Decimal

import pandas as pd


def test_postgres_models_expose_required_tables_and_restrict_foreign_keys():
    from phase1_postgres.models import Base

    tables = Base.metadata.tables

    required = {
        "users",
        "customers",
        "suppliers",
        "materials",
        "flute_types",
        "box_type_rules",
        "score_line_rules",
        "cutting_width_rules",
        "products",
        "orders",
        "order_items",
    }
    assert required.issubset(tables.keys())

    product_customer_fk = next(iter(tables["products"].c.customer_id.foreign_keys))
    assert product_customer_fk.column.table.name == "customers"
    assert product_customer_fk.ondelete == "RESTRICT"

    order_customer_fk = next(iter(tables["orders"].c.customer_id.foreign_keys))
    assert order_customer_fk.column.table.name == "customers"
    assert order_customer_fk.ondelete == "RESTRICT"

    item_order_fk = next(iter(tables["order_items"].c.order_id.foreign_keys))
    assert item_order_fk.column.table.name == "orders"
    assert item_order_fk.ondelete == "CASCADE"


def test_clean_text_trims_and_normalizes_null_like_values():
    from phase1_postgres.etl_pipeline import clean_text

    assert clean_text("  天华包装  ") == "天华包装"
    assert clean_text(None) is None
    assert clean_text(float("nan")) is None
    assert clean_text("NULL") is None
    assert clean_text("   ") is None


def test_transform_customers_creates_unknown_customer_and_deduplicates_codes():
    from phase1_postgres.etl_pipeline import UNKNOWN_CUSTOMER_CODE, transform_customers

    source = pd.DataFrame(
        [
            {"ID": 10, "编码": "  A001 ", "名称": " 天华 "},
            {"ID": 11, "编码": "A001", "名称": "天华重复"},
            {"ID": 12, "编码": None, "名称": None},
        ]
    )

    rows, legacy_map = transform_customers(source)

    assert rows[0]["customer_code"] == UNKNOWN_CUSTOMER_CODE
    assert rows[0]["name"] == "未知客户/历史归档"
    assert legacy_map[10] != legacy_map[11]
    assert rows[1]["customer_code"] == "A001"
    assert rows[2]["customer_code"] == "A001-11"
    assert rows[3]["name"] == "未命名客户-12"


def test_transform_products_routes_orphans_to_unknown_customer():
    from phase1_postgres.etl_pipeline import transform_products

    source = pd.DataFrame(
        [
            {
                "ID": 501,
                "客户_FK": 10,
                "客户料号": " P001 ",
                "品名": " 001A外箱 ",
                "材质": "K=A",
                "长": 450,
                "宽": 340,
                "高": 300,
                "平方价": Decimal("5.50"),
            },
            {
                "ID": 502,
                "客户_FK": 999,
                "客户料号": None,
                "品名": None,
                "材质": None,
                "长": None,
                "宽": None,
                "高": None,
                "平方价": None,
            },
        ]
    )

    rows, legacy_map = transform_products(
        source,
        customer_legacy_id_map={10: 100},
        unknown_customer_id=1,
    )

    assert rows[0]["customer_id"] == 100
    assert rows[0]["product_code"] == "P001"
    assert rows[0]["product_name"] == "001A外箱"
    assert rows[0]["default_material_text"] == "K=A"
    assert rows[1]["customer_id"] == 1
    assert rows[1]["product_code"] == "LEGACY-502"
    assert rows[1]["product_name"] == "未命名常用箱-502"
    assert legacy_map == {501: None, 502: None}
