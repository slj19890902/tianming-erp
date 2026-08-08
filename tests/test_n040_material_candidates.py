"""N040 customer material candidate and selection-trace regression contract.

These tests verify the implemented backend contract from
``N040_READONLY_AUDIT_AND_DESIGN_20260719.md`` using only a disposable SQLite
database.  The asserted response fields are public contract and must not be
replaced with implicit frontend-only state.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def n040_app(tmp_path: Path) -> Generator[tuple[FastAPI, dict[str, int], object], None, None]:
    """Create a complete but isolated N040 API surface backed by SQLite."""
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "n040-material-candidates.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        admin = User(
            username="n040-admin",
            password_hash=hash_password("N040Pass123!"),
            role="admin",
            real_name="N040 Admin",
            must_change_password=False,
        )
        scoped_user = User(
            username="n040-scoped",
            password_hash=hash_password("N040Scoped123!"),
            role="workshop",
            real_name="N040 Scoped User",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(customer_number=401, customer_code="N040-A", name="N040 Customer A")
        other_customer = Customer(
            customer_number=402,
            customer_code="N040-B",
            name="N040 Customer B",
        )
        suppliers = [
            Supplier(
                standard_name=name,
                normalized_name=normalize_supplier_identity(name),
                is_active=True,
                version=1,
            )
            for name in (
                "Original supplier",
                "Supplier A",
                "Supplier B",
                "Supplier C",
                "Supplier D",
            )
        ]
        db.add_all([admin, scoped_user, customer, other_customer, *suppliers])
        db.flush()
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=scoped_user.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserCustomerScope(
                    user_id=scoped_user.id,
                    customer_id=customer.id,
                    assigned_by=admin.id,
                ),
            ]
        )

        initial_material = Material(
            code="N040-ORIGINAL-3B",
            supplier_name="Original supplier",
            layer_count=3,
            flute_type="B",
            quote_price=Decimal("1.10"),
            is_active=True,
        )
        supplier_a_material = Material(
            code="N040-A-3B",
            supplier_name="Supplier A",
            layer_count=3,
            flute_type="B",
            quote_price=Decimal("1.20"),
            is_active=True,
        )
        supplier_b_material = Material(
            code="N040-B-3B",
            supplier_name="Supplier B",
            layer_count=3,
            flute_type="B",
            quote_price=Decimal("1.30"),
            is_active=True,
        )
        incompatible_layer_material = Material(
            code="N040-C-5BE",
            supplier_name="Supplier C",
            layer_count=5,
            flute_type="BE",
            quote_price=Decimal("1.00"),
            is_active=True,
        )
        incompatible_flute_material = Material(
            code="N040-D-3E",
            supplier_name="Supplier D",
            layer_count=3,
            flute_type="E",
            quote_price=Decimal("0.90"),
            is_active=True,
        )
        db.add_all(
            [
                initial_material,
                supplier_a_material,
                supplier_b_material,
                incompatible_layer_material,
                incompatible_flute_material,
            ]
        )
        db.flush()

        product = Product(
            customer_id=customer.id,
            product_code="N040-BOX-A",
            customer_material_code="N040-BOX-A",
            product_name="N040 carton A",
            material_id=initial_material.id,
            layer_count=3,
            flute_type="B",
            box_category="normal",
            legacy_material_text="9CCC9",
        )
        same_customer_product = Product(
            customer_id=customer.id,
            product_code="N040-BOX-A-OTHER",
            customer_material_code="N040-BOX-A-OTHER",
            product_name="N040 carton A other product",
            material_id=initial_material.id,
            layer_count=3,
            flute_type="B",
            box_category="normal",
            legacy_material_text="9CCC9",
        )
        other_product = Product(
            customer_id=other_customer.id,
            product_code="N040-BOX-B",
            customer_material_code="N040-BOX-B",
            product_name="N040 carton B",
            material_id=initial_material.id,
            layer_count=3,
            flute_type="B",
            box_category="normal",
            legacy_material_text="9CCC9",
        )
        db.add_all([product, same_customer_product, other_product])
        db.flush()

        order = Order(
            order_number="N040-ORDER-A",
            customer_id=customer.id,
            order_date=date(2026, 7, 19),
            total_amount=Decimal("0"),
            created_by=admin.id,
        )
        other_order = Order(
            order_number="N040-ORDER-B",
            customer_id=other_customer.id,
            order_date=date(2026, 7, 19),
            total_amount=Decimal("0"),
            created_by=admin.id,
        )
        db.add_all([order, other_order])
        db.flush()

        items = [
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number=f"N040-A-{sequence}",
                quantity=1,
                unit_price=Decimal("1"),
                subtotal=Decimal("1"),
                snapshot_product_name="N040 carton A",
                snapshot_product_code="N040-BOX-A",
                snapshot_material="9CCC9",
                snapshot_original_material_code="9CCC9",
                material_id=initial_material.id,
                snapshot_supplier_name="Original supplier",
                layer_count=3,
                flute_type="B",
            )
            for sequence in range(1, 7)
        ]
        other_item = OrderItem(
            order_id=other_order.id,
            product_id=other_product.id,
            item_order_number="N040-B-1",
            quantity=1,
            unit_price=Decimal("1"),
            subtotal=Decimal("1"),
            snapshot_product_name="N040 carton B",
            snapshot_product_code="N040-BOX-B",
            snapshot_material="9CCC9",
            snapshot_original_material_code="9CCC9",
            material_id=initial_material.id,
            snapshot_supplier_name="Original supplier",
            layer_count=3,
            flute_type="B",
        )
        same_customer_other_item = OrderItem(
            order_id=order.id,
            product_id=same_customer_product.id,
            item_order_number="N040-A-OTHER-1",
            quantity=1,
            unit_price=Decimal("1"),
            subtotal=Decimal("1"),
            snapshot_product_name="N040 carton A other product",
            snapshot_product_code="N040-BOX-A-OTHER",
            snapshot_material="9CCC9",
            snapshot_original_material_code="9CCC9",
            material_id=initial_material.id,
            snapshot_supplier_name="Original supplier",
            layer_count=3,
            flute_type="B",
        )
        db.add_all([*items, same_customer_other_item, other_item])
        db.commit()
        ids = {
            "customer": customer.id,
            "other_customer": other_customer.id,
            "product": product.id,
            "same_customer_product": same_customer_product.id,
            "same_customer_other_item": same_customer_other_item.id,
            "other_product": other_product.id,
            "first_item": items[0].id,
            "second_item": items[1].id,
            "third_item": items[2].id,
            "fourth_item": items[3].id,
            "other_item": other_item.id,
            "initial_material": initial_material.id,
            "supplier_a_material": supplier_a_material.id,
            "supplier_b_material": supplier_b_material.id,
            "incompatible_layer_material": incompatible_layer_material.id,
            "incompatible_flute_material": incompatible_flute_material.id,
            "scoped_user": scoped_user.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, factory
    finally:
        engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "n040-admin", "password": "N040Pass123!"},
    )
    assert response.status_code == 200


def _login_scoped(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "n040-scoped", "password": "N040Scoped123!"},
    )
    assert response.status_code == 200


def _candidate_payload(
    customer_id: int,
    material_id: int,
    *,
    supplier_name: str,
    manual_priority: int = 0,
) -> dict:
    return {
        "customer_id": customer_id,
        "original_material_code": "9CCC9",
        "supplier_name": supplier_name,
        "actual_material_id": material_id,
        "manual_priority": manual_priority,
        "is_active": True,
        "source": "n040-regression",
        "notes": "temporary SQLite regression fixture",
    }


def _create_candidate(client: TestClient, payload: dict) -> int:
    response = client.post("/api/requisition/material-candidates", json=payload)
    assert response.status_code == 201, response.text
    candidate = response.json()
    assert isinstance(candidate["id"], int)
    return candidate["id"]


def _item_candidates(client: TestClient, item_id: int) -> list[dict]:
    response = client.get(f"/api/requisition/pending/{item_id}/material-candidates")
    assert response.status_code == 200, response.text
    payload = response.json()
    if isinstance(payload, list):
        return payload
    assert payload["item_id"] == item_id
    candidates = payload.get("candidates", payload.get("items"))
    assert isinstance(candidates, list)
    return candidates


def _choose_material(
    client: TestClient,
    item_id: int,
    *,
    candidate_id: int | None,
    material_id: int,
    source_type: str = "manual",
    source_reference: str = "N040 manual confirmation",
    selection_reason: str = "operator chose verified supplier material",
    sync_product: bool = True,
) -> dict:
    context_response = client.get(
        f"/api/requisition/pending/{item_id}/material-candidates"
    )
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    payload = {
        "material_id": material_id,
        "candidate_id": candidate_id,
        "source_type": source_type,
        "source_reference": source_reference,
        "selection_reason": selection_reason,
        "sync_product": sync_product,
        "product_expected_version": (
            context.get("product_version") if sync_product else None
        ),
        "product_change_reason": (
            "报料人工修改材质并同步常用箱" if sync_product else None
        ),
    }
    response = client.put(
        f"/api/requisition/pending/{item_id}/material",
        json=payload,
    )
    if response.status_code == 409:
        detail = response.json().get("detail") or {}
        if detail.get("code") == "MASTER_CHANGE_CONFIRMATION_REQUIRED":
            payload["product_confirmation_token"] = detail["confirmation_token"]
            response = client.put(
                f"/api/requisition/pending/{item_id}/material",
                json=payload,
            )
    assert response.status_code == 200, response.text
    return response.json()


def test_same_customer_original_code_allows_multiple_candidates_but_never_leaks_to_another_customer(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, _factory = n040_app
    with TestClient(app) as client:
        _login(client)
        candidate_a = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_a_material"],
                supplier_name="Supplier A",
            ),
        )
        candidate_b = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_b_material"],
                supplier_name="Supplier B",
            ),
        )
        other_candidate = _create_candidate(
            client,
            _candidate_payload(
                ids["other_customer"],
                ids["supplier_a_material"],
                supplier_name="Other customer supplier",
            ),
        )

        customer_candidates = _item_candidates(client, ids["first_item"])
        assert {row["id"] for row in customer_candidates} == {candidate_a, candidate_b}
        assert {row["supplier_name"] for row in customer_candidates} == {
            "Supplier A",
            "Supplier B",
        }
        assert other_candidate not in {row["id"] for row in customer_candidates}

        other_candidates = _item_candidates(client, ids["other_item"])
        assert [row["id"] for row in other_candidates] == [other_candidate]


def test_candidate_list_excludes_incompatible_layer_and_flute(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, _factory = n040_app
    with TestClient(app) as client:
        _login(client)
        compatible_id = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_a_material"],
                supplier_name="Supplier A",
            ),
        )
        layer_mismatch_id = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["incompatible_layer_material"],
                supplier_name="Supplier C",
            ),
        )
        flute_mismatch_id = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["incompatible_flute_material"],
                supplier_name="Supplier D",
            ),
        )

        candidates = _item_candidates(client, ids["first_item"])
        candidate_ids = {row["id"] for row in candidates}
        assert compatible_id in candidate_ids
        assert layer_mismatch_id not in candidate_ids
        assert flute_mismatch_id not in candidate_ids
        assert all(row["layer_count"] == 3 and row["flute_type"] == "B" for row in candidates)


def test_history_count_then_recent_use_controls_explainable_candidate_order(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, _factory = n040_app
    with TestClient(app) as client:
        _login(client)
        candidate_a = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_a_material"],
                supplier_name="Supplier A",
            ),
        )
        candidate_b = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_b_material"],
                supplier_name="Supplier B",
            ),
        )

        # Both candidates are selected twice.  B is selected later, so it wins
        # the documented count tie by its more recent immutable selection.
        _choose_material(
            client, ids["first_item"], candidate_id=candidate_a,
            material_id=ids["supplier_a_material"],
        )
        _choose_material(
            client, ids["second_item"], candidate_id=candidate_a,
            material_id=ids["supplier_a_material"],
        )
        _choose_material(
            client, ids["third_item"], candidate_id=candidate_b,
            material_id=ids["supplier_b_material"],
        )
        _choose_material(
            client, ids["fourth_item"], candidate_id=candidate_b,
            material_id=ids["supplier_b_material"],
        )

        candidates = _item_candidates(client, ids["first_item"])
        assert [row["id"] for row in candidates[:2]] == [candidate_b, candidate_a]
        for row in candidates[:2]:
            assert row["history_count"] == 2
            assert row["last_used_at"] is not None
            assert row["reference_price"] is not None
            assert row["recommendation_reason"]


def test_manual_selection_preserves_original_snapshot_appends_history_and_syncs_product(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, factory = n040_app
    with TestClient(app) as client:
        _login(client)
        candidate_a = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_a_material"],
                supplier_name="Supplier A",
            ),
        )
        candidate_b = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_b_material"],
                supplier_name="Supplier B",
            ),
        )

        first_choice = _choose_material(
            client,
            ids["first_item"],
            candidate_id=candidate_a,
            material_id=ids["supplier_a_material"],
            source_type="manual",
            source_reference="phone confirmation 2026-07-19",
            selection_reason="customer-approved Supplier A stock",
        )
        assert first_choice["material_id"] == ids["supplier_a_material"]

        # A later actual-material correction must not rewrite the original
        # customer code nor mutate the first selection-history record.
        _choose_material(
            client,
            ids["first_item"],
            candidate_id=candidate_b,
            material_id=ids["supplier_b_material"],
            source_type="manual",
            source_reference="later supplier confirmation",
            selection_reason="Supplier A unavailable",
        )
        repeated = _choose_material(
            client,
            ids["first_item"],
            candidate_id=candidate_b,
            material_id=ids["supplier_b_material"],
            source_type="manual",
            source_reference="later supplier confirmation",
            selection_reason="Supplier A unavailable",
        )
        assert repeated["selection_history_id"] is None
        assert repeated["message"] == "材质未变化，无需重复保存"
        history_response = client.get(
            f"/api/requisition/pending/{ids['first_item']}/material-history"
        )
        assert history_response.status_code == 200, history_response.text
        history_payload = history_response.json()
        history = history_payload if isinstance(history_payload, list) else history_payload["history"]
        assert len(history) == 2
        assert history[0]["candidate_id"] == candidate_a
        assert history[0]["original_material_code_snapshot"] == "9CCC9"
        assert history[0]["selected_material_id"] == ids["supplier_a_material"]
        assert history[0]["selected_material_code_snapshot"] == "N040-A-3B"
        assert history[0]["selected_supplier_name_snapshot"] == "Supplier A"
        assert history[0]["source_type"] == "manual"
        assert history[0]["source_reference"] == "phone confirmation 2026-07-19"
        assert history[0]["selection_reason"] == "customer-approved Supplier A stock"
        assert history[0]["sync_product"] is True
        assert history[0]["selected_by_name"] == "N040 Admin"
        assert history[1]["candidate_id"] == candidate_b
        assert history[1]["selected_material_id"] == ids["supplier_b_material"]

    with factory() as db:
        from app.models.order import OrderItem
        from app.models.product import Product

        item = db.get(OrderItem, ids["first_item"])
        product = db.get(Product, ids["product"])
        assert item is not None and product is not None
        assert item.snapshot_original_material_code == "9CCC9"
        assert item.snapshot_material == "N040-B-3B"
        assert product.material_id == ids["supplier_b_material"]
        assert product.layer_count == 3
        assert product.flute_type == "B"


def test_product_material_context_uses_only_this_product_and_prefers_official_facts(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, factory = n040_app
    with TestClient(app) as client:
        _login(client)
        candidate_a = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_a_material"],
                supplier_name="Supplier A",
            ),
        )
        candidate_b = _create_candidate(
            client,
            _candidate_payload(
                ids["customer"],
                ids["supplier_b_material"],
                supplier_name="Supplier B",
            ),
        )
        _choose_material(
            client,
            ids["second_item"],
            candidate_id=candidate_a,
            material_id=ids["supplier_a_material"],
        )

        with factory() as db:
            from app.models.order import OrderItem
            from app.models.requisition import Requisition, RequisitionItem
            from app.models.supplier_requisition_order import (
                SupplierRequisitionOrder,
                SupplierRequisitionOrderItem,
            )

            confirmed = SupplierRequisitionOrder(
                order_number="SRO-N040-CONFIRMED",
                supplier_name="Supplier A",
                material_id=ids["supplier_a_material"],
                layer_count=3,
                flute_type="B",
                total_quantity=100,
                requisition_qty=100,
                status="confirmed",
                created_at=datetime(2026, 7, 18, 9, 30),
            )
            voided = SupplierRequisitionOrder(
                order_number="SRO-N040-VOIDED",
                supplier_name="Supplier B",
                material_id=ids["supplier_b_material"],
                layer_count=3,
                flute_type="B",
                total_quantity=50,
                requisition_qty=50,
                status="voided",
                created_at=datetime(2026, 7, 18, 10, 30),
            )
            other_product_order = SupplierRequisitionOrder(
                order_number="SRO-N040-OTHER-PRODUCT",
                supplier_name="Supplier B",
                material_id=ids["supplier_b_material"],
                layer_count=3,
                flute_type="B",
                total_quantity=70,
                requisition_qty=70,
                status="confirmed",
                created_at=datetime(2026, 7, 18, 11, 30),
            )
            db.add_all([confirmed, voided, other_product_order])
            db.flush()
            db.add_all(
                [
                    SupplierRequisitionOrderItem(
                        supplier_order_id=confirmed.id,
                        order_item_id=ids["first_item"],
                        product_id=ids["product"],
                        material_id=ids["supplier_a_material"],
                        material_code_snapshot="N040-A-3B",
                        supplier_name_snapshot="Supplier A",
                        layer_count_snapshot=3,
                        flute_type_snapshot="B",
                        order_number="N040-A-1",
                        quantity=100,
                        requisition_qty=100,
                    ),
                    SupplierRequisitionOrderItem(
                        supplier_order_id=voided.id,
                        order_item_id=ids["third_item"],
                        product_id=ids["product"],
                        material_id=ids["supplier_b_material"],
                        material_code_snapshot="N040-B-3B",
                        supplier_name_snapshot="Supplier B",
                        layer_count_snapshot=3,
                        flute_type_snapshot="B",
                        order_number="N040-A-3",
                        quantity=50,
                        requisition_qty=50,
                    ),
                    SupplierRequisitionOrderItem(
                        supplier_order_id=other_product_order.id,
                        order_item_id=ids["same_customer_other_item"],
                        product_id=ids["same_customer_product"],
                        material_id=ids["supplier_b_material"],
                        material_code_snapshot="N040-B-3B",
                        supplier_name_snapshot="Supplier B",
                        layer_count_snapshot=3,
                        flute_type_snapshot="B",
                        order_number="N040-A-OTHER-1",
                        quantity=70,
                        requisition_qty=70,
                    ),
                ]
            )

            legacy = Requisition(
                requisition_number="MR-N040-LEGACY",
                requisition_date=date(2026, 7, 17),
                supplier_name="Legacy supplier",
                status="confirmed",
            )
            filtered_merge = Requisition(
                requisition_number="MR-N040-MERGED",
                requisition_date=date(2026, 7, 16),
                supplier_name="Merged supplier",
                status="confirmed",
            )
            db.add_all([legacy, filtered_merge])
            db.flush()
            first_item = db.get(OrderItem, ids["first_item"])
            second_item = db.get(OrderItem, ids["second_item"])
            assert first_item is not None and second_item is not None
            db.add_all(
                [
                    # Same order_item as the official record: must be de-duplicated.
                    RequisitionItem(
                        requisition_id=legacy.id,
                        order_item_id=first_item.id,
                        requisition_qty=100,
                        cardboard_len=Decimal("1000"),
                        cardboard_width=Decimal("800"),
                        material_snapshot="DUPLICATE-LEGACY",
                        product_name_snapshot="N040 carton A",
                        status="confirmed",
                    ),
                    RequisitionItem(
                        requisition_id=legacy.id,
                        order_item_id=second_item.id,
                        requisition_qty=20,
                        cardboard_len=Decimal("1000"),
                        cardboard_width=Decimal("800"),
                        material_snapshot="LEGACY-3B",
                        product_name_snapshot="N040 carton A",
                        status="confirmed",
                    ),
                    RequisitionItem(
                        requisition_id=filtered_merge.id,
                        order_item_id=ids["fourth_item"],
                        requisition_qty=10,
                        cardboard_len=Decimal("1000"),
                        cardboard_width=Decimal("800"),
                        material_snapshot="SHOULD-NOT-APPEAR",
                        product_name_snapshot="N040 carton A",
                        status="merged_pending",
                    ),
                ]
            )
            db.commit()

        response = client.get(
            f"/api/requisition/products/{ids['product']}/material-context"
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["product_id"] == ids["product"]
        assert payload["product_version"] >= 1
        assert payload["customer_id"] == ids["customer"]
        assert payload["original_material_code"] == "9CCC9"

        history = payload["requisition_history"]
        document_numbers = [row["document_no"] for row in history]
        assert "SRO-N040-CONFIRMED" in document_numbers
        assert "SRO-N040-VOIDED" in document_numbers
        assert "MR-N040-LEGACY" in document_numbers
        assert "SRO-N040-OTHER-PRODUCT" not in document_numbers
        assert "MR-N040-MERGED" not in document_numbers
        assert all(row["material_code"] != "DUPLICATE-LEGACY" for row in history)
        official = next(
            row for row in history if row["document_no"] == "SRO-N040-CONFIRMED"
        )
        assert official["supplier_name"] == "Supplier A"
        assert official["material_code"] == "N040-A-3B"
        assert official["document_date"].startswith("2026-07-18")
        assert official["document_date"].endswith("Z")
        assert official["is_effective"] is True
        assert next(
            row for row in history if row["document_no"] == "SRO-N040-VOIDED"
        )["is_effective"] is False

        candidates = {row["candidate_id"]: row for row in payload["candidates"]}
        assert candidates[candidate_a]["effective_use_count"] == 1
        assert candidates[candidate_a]["history_count"] == 1
        assert candidates[candidate_a]["last_document_no"] == "SRO-N040-CONFIRMED"
        assert candidates[candidate_a]["last_used_at"].endswith("Z")
        assert candidates[candidate_a]["last_requisition_at"].endswith("Z")
        assert candidates[candidate_a]["recommendation_reason"]
        assert candidates[candidate_b]["effective_use_count"] == 0
        assert candidates[candidate_b]["last_requisition_at"] is None
        assert len(payload["manual_selection_history"]) == 1


def test_product_material_context_promotes_each_formal_material_and_compares_current_cost(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, factory = n040_app
    with factory() as db:
        from app.models.material import Material
        from app.models.order import OrderItem
        from app.models.product import Product
        from app.models.supplier_requisition_order import (
            SupplierRequisitionOrder,
            SupplierRequisitionOrderItem,
        )

        material_a = db.get(Material, ids["supplier_a_material"])
        material_b = db.get(Material, ids["supplier_b_material"])
        product = db.get(Product, ids["product"])
        first_item = db.get(OrderItem, ids["first_item"])
        second_item = db.get(OrderItem, ids["second_item"])
        assert all((material_a, material_b, product, first_item, second_item))
        material_a.basis_weight_description = "190g/135g/55g"
        material_a.paper_composition = "A kraft / A flute / A liner"
        material_a.price_unit = "元/㎡"
        material_a.quote_date = date(2026, 8, 1)
        material_b.basis_weight_description = "170g/120g/140g"
        material_b.paper_composition = "B liner / B flute / B liner"
        material_b.price_unit = "元/㎡"
        material_b.quote_date = date(2026, 8, 2)
        # The same order item was formally requisitioned twice with different
        # suppliers.  Its mutable order snapshot now reflects the later B
        # choice, so the earlier A occurrence must fall back to A's material
        # master instead of being mislabeled with B's weight.
        product.material_id = material_b.id
        first_item.material_id = material_b.id
        first_item.snapshot_material = material_b.code
        first_item.snapshot_supplier_name = material_b.supplier_name
        first_item.snapshot_weight = material_b.basis_weight_description

        first_order = SupplierRequisitionOrder(
            order_number="SRO-N040-FIRST-SUPPLIER",
            supplier_name=material_a.supplier_name,
            material_id=material_a.id,
            layer_count=3,
            flute_type="B",
            total_quantity=30,
            requisition_qty=30,
            status="confirmed",
            created_at=datetime(2026, 8, 4, 8, 0),
        )
        second_order = SupplierRequisitionOrder(
            order_number="SRO-N040-SECOND-SUPPLIER",
            supplier_name=material_b.supplier_name,
            material_id=material_b.id,
            layer_count=3,
            flute_type="B",
            total_quantity=40,
            requisition_qty=40,
            status="confirmed",
            created_at=datetime(2026, 8, 6, 8, 0),
        )
        db.add_all([first_order, second_order])
        db.flush()
        db.add_all(
            [
                SupplierRequisitionOrderItem(
                    supplier_order_id=first_order.id,
                    order_item_id=first_item.id,
                    product_id=product.id,
                    material_id=material_a.id,
                    material_code_snapshot=material_a.code,
                    supplier_name_snapshot=material_a.supplier_name,
                    layer_count_snapshot=3,
                    flute_type_snapshot="B",
                    order_number=first_item.item_order_number,
                    quantity=30,
                    requisition_qty=30,
                ),
                SupplierRequisitionOrderItem(
                    supplier_order_id=second_order.id,
                    order_item_id=first_item.id,
                    product_id=product.id,
                    material_id=material_b.id,
                    material_code_snapshot=material_b.code,
                    supplier_name_snapshot=material_b.supplier_name,
                    layer_count_snapshot=3,
                    flute_type_snapshot="B",
                    order_number=first_item.item_order_number,
                    quantity=40,
                    requisition_qty=40,
                ),
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.get(
            f"/api/requisition/products/{ids['product']}/material-context"
        )
        assert response.status_code == 200, response.text
        payload = response.json()

    effective_history = [
        row for row in payload["requisition_history"] if row["is_effective"]
    ]
    assert [row["document_no"] for row in effective_history] == [
        "SRO-N040-SECOND-SUPPLIER",
        "SRO-N040-FIRST-SUPPLIER",
    ]
    assert len({row["document_item_id"] for row in effective_history}) == 2
    assert {row["material_code"] for row in effective_history} == {
        "N040-A-3B",
        "N040-B-3B",
    }
    assert all(row["basis_weight_description"] for row in effective_history)
    history_by_material = {row["material_code"]: row for row in effective_history}
    assert history_by_material["N040-A-3B"]["weight_source"] == "current_material_master"
    assert history_by_material["N040-B-3B"]["weight_source"] == "order_snapshot"
    assert all(row["current_effective_price"] is not None for row in effective_history)
    assert all(row["current_price_scope"] == "current_reference" for row in effective_history)

    by_material = {row["material_id"]: row for row in payload["candidates"]}
    candidate_a = by_material[ids["supplier_a_material"]]
    candidate_b = by_material[ids["supplier_b_material"]]
    assert "formal_requisition_history" in candidate_a["candidate_source_types"]
    assert "formal_requisition_history" in candidate_b["candidate_source_types"]
    assert candidate_a["history_count"] == 1
    assert candidate_b["history_count"] == 1
    assert candidate_a["basis_weight_description"] == "190g/135g/55g"
    assert candidate_b["basis_weight_description"] == "170g/120g/140g"
    assert candidate_a["total_basis_weight_gsm"] == 380
    assert candidate_b["total_basis_weight_gsm"] == 430
    assert candidate_a["effective_price"] == 1.2
    assert candidate_b["effective_price"] == 1.3
    assert candidate_a["price_difference_to_lowest"] == 0
    assert candidate_b["price_difference_to_lowest"] == pytest.approx(0.1)
    assert candidate_a["price_scope"] == "current_reference"
    assert candidate_b["price_scope"] == "current_reference"


def test_product_material_context_keeps_unmatched_requisition_selection_in_material_trajectory(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, factory = n040_app
    with factory() as db:
        from app.models.customer_material import CustomerMaterialSelectionHistory
        from app.models.material import Material
        from app.models.order import OrderItem
        from app.models.product import Product
        from app.models.supplier_requisition_order import (
            SupplierRequisitionOrder,
            SupplierRequisitionOrderItem,
        )

        material_a = db.get(Material, ids["supplier_a_material"])
        material_b = db.get(Material, ids["supplier_b_material"])
        product = db.get(Product, ids["product"])
        item = db.get(OrderItem, ids["first_item"])
        assert all((material_a, material_b, product, item))
        material_a.basis_weight_description = "190g/135g/55g"
        material_a.price_unit = "元/㎡"
        material_b.basis_weight_description = "235g/155g/185g"
        material_b.price_unit = "元/㎡"
        product.material_id = material_b.id
        item.material_id = material_b.id
        item.snapshot_material = material_b.code
        item.snapshot_supplier_name = material_b.supplier_name
        item.snapshot_weight = material_b.basis_weight_description

        formal = SupplierRequisitionOrder(
            order_number="SRO-N040-OLD-FORMAL",
            supplier_name=material_a.supplier_name,
            material_id=material_a.id,
            layer_count=3,
            flute_type="B",
            total_quantity=50,
            requisition_qty=50,
            status="confirmed",
            created_at=datetime(2026, 8, 4, 8, 0),
        )
        db.add(formal)
        db.flush()
        db.add_all(
            [
                SupplierRequisitionOrderItem(
                    supplier_order_id=formal.id,
                    order_item_id=item.id,
                    product_id=product.id,
                    material_id=material_a.id,
                    material_code_snapshot=material_a.code,
                    supplier_name_snapshot=material_a.supplier_name,
                    layer_count_snapshot=3,
                    flute_type_snapshot="B",
                    order_number=item.item_order_number,
                    quantity=50,
                    requisition_qty=50,
                ),
                CustomerMaterialSelectionHistory(
                    customer_id=ids["customer"],
                    order_item_id=item.id,
                    product_id=product.id,
                    original_material_code_snapshot="9CCC9",
                    normalized_original_material_code_snapshot="9CCC9",
                    selected_material_id=material_b.id,
                    selected_material_code_snapshot=material_b.code,
                    selected_supplier_name_snapshot=material_b.supplier_name,
                    layer_count_snapshot=3,
                    flute_type_snapshot="B",
                    source_type="manual",
                    source_reference="second supplier selection",
                    selection_reason="second supplier used for this common box",
                    sync_product=True,
                    selected_at=datetime(2026, 8, 6, 9, 0),
                ),
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.get(
            f"/api/requisition/products/{ids['product']}/material-context"
        )
        assert response.status_code == 200, response.text
        payload = response.json()

    assert [row["material_code"] for row in payload["requisition_history"]] == [
        "N040-A-3B"
    ]
    trajectory = payload["material_history"]
    assert [row["material_code"] for row in trajectory] == [
        "N040-B-3B",
        "N040-A-3B",
    ]
    selection = trajectory[0]
    assert selection["source_type"] == "material_selection"
    assert selection["is_formal_requisition"] is False
    assert selection["document_status"] == "报料选材记录（未匹配正式报料单）"
    assert selection["requisition_qty"] is None
    assert selection["basis_weight_description"] == "235g/155g/185g"
    assert selection["total_basis_weight_gsm"] == 575
    assert selection["current_effective_price"] == 1.3

    candidates = {row["material_id"]: row for row in payload["candidates"]}
    assert set(candidates) >= {
        ids["supplier_a_material"],
        ids["supplier_b_material"],
    }
    assert candidates[ids["supplier_a_material"]]["history_count"] == 1
    assert candidates[ids["supplier_a_material"]]["last_requisition_at"].endswith("Z")
    assert candidates[ids["supplier_b_material"]]["selection_history_count"] == 1
    assert candidates[ids["supplier_b_material"]]["last_requisition_at"] is None
    assert "requisition_material_selection" in candidates[
        ids["supplier_b_material"]
    ]["candidate_source_types"]


def test_product_material_context_enforces_customer_scope_and_hides_cost_fields(
    n040_app: tuple[FastAPI, dict[str, int], object],
) -> None:
    app, ids, _factory = n040_app
    with TestClient(app) as client:
        _login_scoped(client)

        allowed = client.get(
            f"/api/requisition/products/{ids['product']}/material-context"
        )
        assert allowed.status_code == 200, allowed.text
        assert "reference_price" not in allowed.text
        assert "price_unit" not in allowed.text
        assert "effective_price" not in allowed.text
        assert "price_difference_to_lowest" not in allowed.text
        assert "current_effective_price" not in allowed.text

        denied = client.get(
            f"/api/requisition/products/{ids['other_product']}/material-context"
        )
        assert denied.status_code == 403

        pending_candidates = client.get(
            f"/api/requisition/pending/{ids['first_item']}/material-candidates"
        )
        assert pending_candidates.status_code == 200, pending_candidates.text
        assert "reference_price" not in pending_candidates.text
        assert "price_unit" not in pending_candidates.text
