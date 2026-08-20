from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_p1_51a_common_box_print_colors import (
    _create_customer,
    _create_product,
    _product_payload,
    _update_payload,
    print_color_app,
)


def _direct_color_gap_ids(app: FastAPI) -> set[int]:
    from app.models.product import Product
    from app.services.printing_colors import parse_printing_colors

    required_counts = {"单色印刷": 1, "双色印刷": 2, "三色印刷": 3}
    with app.state.session_factory() as session:
        products = session.query(Product).filter(Product.is_active.is_(True)).all()
        return {
            int(product.id)
            for product in products
            if product.print_content in required_counts
            and len(parse_printing_colors(product.printing_colors))
            != required_counts[product.print_content]
        }


def test_current_save_chain_does_not_add_direct_print_color_gaps(
    print_color_app: FastAPI,
) -> None:
    """Stage-0 gate: current writes must not create or enlarge the audited gap set."""
    from app.models.product import Product

    with TestClient(print_color_app) as client:
        customer = _create_customer(client, "PHASE0-C2")

        invalid_cases = [
            ("C2-MISSING", "单色印刷", None),
            ("C2-DOUBLE-ONE", "双色印刷", "黑色"),
            ("C2-TRIPLE-TWO", "三色印刷", "黑色＋红色"),
        ]
        for suffix, print_content, printing_colors in invalid_cases:
            response = client.post(
                "/api/master/products",
                json=_product_payload(
                    customer["id"],
                    suffix,
                    print_content=print_content,
                    printing_colors=printing_colors,
                ),
            )
            assert response.status_code == 422, response.text

        valid_cases = [
            ("C2-SINGLE", "单色印刷", "  PANTONE 186 C  ", "PANTONE 186 C"),
            ("C2-DOUBLE", "双色印刷", "蓝色,黑色", "蓝色＋黑色"),
            ("C2-TRIPLE", "三色印刷", "橙色|绿色/蓝色", "橙色＋绿色＋蓝色"),
        ]
        valid_ids: set[int] = set()
        for suffix, print_content, raw_colors, canonical_colors in valid_cases:
            created = _create_product(
                client,
                customer["id"],
                suffix,
                print_content=print_content,
                printing_colors=raw_colors,
            )
            valid_ids.add(int(created["id"]))
            reopened = client.get(f"/api/master/products/{created['id']}")
            assert reopened.status_code == 200, reopened.text
            assert reopened.json()["printing_colors"] == canonical_colors

        with print_color_app.state.session_factory() as session:
            legacy = Product(
                customer_id=customer["id"],
                product_code="PHASE0-C2-LEGACY-BLANK",
                customer_material_code="PHASE0-C2-LEGACY-BLANK",
                product_name="阶段0匿名旧空颜色常用箱",
                box_category="normal",
                box_style="A1/0201 普通开槽箱",
                supply_mode="corrugated_production",
                print_content="单色印刷",
                printing_colors=None,
                printing_plate_mode="no_plate",
                remark="旧记录",
                is_active=True,
                version=1,
            )
            session.add(legacy)
            session.commit()
            legacy_id = int(legacy.id)

        before_gap_ids = _direct_color_gap_ids(print_color_app)
        assert before_gap_ids == {legacy_id}
        assert valid_ids.isdisjoint(before_gap_ids)

        legacy_response = client.get(f"/api/master/products/{legacy_id}")
        assert legacy_response.status_code == 200, legacy_response.text
        unrelated_update = client.put(
            f"/api/master/products/{legacy_id}",
            json=_update_payload(legacy_response.json(), remark="只修改匿名旧记录备注"),
        )
        assert unrelated_update.status_code == 200, unrelated_update.text
        assert unrelated_update.json()["printing_colors"] is None

        invalid_explicit_change = client.put(
            f"/api/master/products/{legacy_id}",
            json=_update_payload(
                unrelated_update.json(),
                print_content="双色印刷",
                printing_colors=None,
            ),
        )
        assert invalid_explicit_change.status_code == 422, invalid_explicit_change.text

    assert _direct_color_gap_ids(print_color_app) == before_gap_ids
