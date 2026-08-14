from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def print_color_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from app.api.customers import router as customers_router
    from app.api.deps import get_current_user, get_db
    from app.api.master_data_versions import router as master_data_versions_router
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    monkeypatch.setenv("ERP_SECRET_KEY", "p1-51a-print-colors-tests-only")
    engine = create_sqlite_engine(tmp_path / "p1-51a-print-colors.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        admin = User(
            username="p1-51a-admin",
            password_hash="not-used",
            role="admin",
            real_name="P1-51A Admin",
            display_name="P1-51A Admin",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
        )
        session.add(admin)
        session.commit()
        admin_id = int(admin.id)

    app = FastAPI()
    app.include_router(customers_router, prefix="/api/master/customers")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(master_data_versions_router, prefix="/api/master-data")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    def override_current_user() -> User:
        with factory() as session:
            user = session.get(User, app.state.current_user_id)
            assert user is not None
            list(user.permission_overrides)
            return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.state.session_factory = factory
    app.state.current_user_id = admin_id
    app.state.admin_id = admin_id
    return app


def _create_customer(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/api/master/customers",
        json={
            "customer_number": 5100 + sum(ord(char) for char in suffix),
            "customer_code": f"P151A-{suffix}",
            "name": f"P1-51A客户-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _product_payload(customer_id: int, suffix: str, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "customer_id": customer_id,
        "product_code": f"P151A-P-{suffix}",
        "customer_material_code": f"P151A-C-{suffix}",
        "product_name": f"P1-51A 常用箱 {suffix}",
        "box_category": "normal",
        "box_style": "A1",
        "length_mm": 380,
        "width_mm": 260,
        "height_mm": 220,
        "remark": "印刷颜色之外的兄弟字段不得改动",
        "production_label_enabled": True,
        "production_label_units_per_label": 5,
        "print_content": "无印刷",
        "printing_colors": None,
        "printing_plate_mode": "no_plate",
    }
    payload.update(overrides)
    return payload


def _create_product(
    client: TestClient,
    customer_id: int,
    suffix: str,
    **overrides: object,
) -> dict:
    response = client.post(
        "/api/master/products",
        json=_product_payload(customer_id, suffix, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _update_payload(product: dict, **overrides: object) -> dict:
    from app.api.products import ProductPayload

    payload = {
        field_name: product.get(field_name)
        for field_name in ProductPayload.model_fields
        if field_name in product
    }
    payload.update(
        expected_version=product["version"],
        change_reason="P1-51A 印刷颜色专项验收",
    )
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "raw",
    [
        " 黑色,红色 ",
        "黑色，红色",
        "黑色、红色",
        "黑色/红色",
        "黑色／红色",
        "黑色|红色",
        "黑色｜红色",
        "黑色;红色",
        "黑色；红色",
        "黑色+红色",
        "黑色＋红色",
    ],
)
def test_shared_parser_accepts_explicit_legacy_separators_in_order(raw: str) -> None:
    from app.services.printing_colors import parse_printing_colors

    assert parse_printing_colors(raw) == ["黑色", "红色"]


def test_shared_normalizer_preserves_spot_color_names_and_enforces_contract() -> None:
    from app.services.printing_colors import (
        PrintingColorError,
        normalize_printing_colors,
        parse_printing_colors,
    )

    assert parse_printing_colors("  PANTONE 186 C  ") == ["PANTONE 186 C"]
    assert normalize_printing_colors("单色印刷", "  PANTONE 186 C  ") == "PANTONE 186 C"
    assert normalize_printing_colors("双色印刷", " 蓝色,黑色 ") == "蓝色＋黑色"
    assert normalize_printing_colors("三色印刷", "橙色|绿色/蓝色") == "橙色＋绿色＋蓝色"
    assert normalize_printing_colors("无印刷", "黑色") is None
    assert normalize_printing_colors("单色印刷", "专" * 40) == "专" * 40

    invalid_values = [
        ("单色印刷", None),
        ("单色印刷", ""),
        ("单色印刷", "黑色＋红色"),
        ("双色印刷", "黑色"),
        ("双色印刷", "黑色＋"),
        ("双色印刷", "黑色＋黑色"),
        ("三色印刷", "黑色＋红色"),
        ("单色印刷", "专" * 41),
    ]
    for print_content, value in invalid_values:
        with pytest.raises(PrintingColorError):
            normalize_printing_colors(print_content, value)


def test_api_does_not_silently_persist_single_color_suggestion(
    print_color_app: FastAPI,
) -> None:
    with TestClient(print_color_app) as client:
        customer = _create_customer(client, "NO-DEFAULT")
        response = client.post(
            "/api/master/products",
            json=_product_payload(
                customer["id"],
                "NO-DEFAULT",
                production_process="印刷",
                print_content="单色印刷",
                printing_colors=None,
            ),
        )

    assert response.status_code == 422, response.text
    assert "颜色" in response.text


@pytest.mark.parametrize(
    ("print_content", "raw", "canonical"),
    [
        ("单色印刷", "  PANTONE 186 C  ", "PANTONE 186 C"),
        ("双色印刷", " 蓝色,黑色 ", "蓝色＋黑色"),
        ("三色印刷", "橙色|绿色/蓝色", "橙色＋绿色＋蓝色"),
    ],
)
def test_api_canonicalizes_direct_print_colors_and_reopens_them(
    print_color_app: FastAPI,
    print_content: str,
    raw: str,
    canonical: str,
) -> None:
    suffix = str(len(canonical)) + print_content[:1]
    with TestClient(print_color_app) as client:
        customer = _create_customer(client, suffix)
        created = _create_product(
            client,
            customer["id"],
            suffix,
            print_content=print_content,
            printing_colors=raw,
            printing_plate_mode="no_plate",
        )
        reopened = client.get(f"/api/master/products/{created['id']}")

    assert created["printing_colors"] == canonical
    assert created["printing_plate_mode"] == "no_plate"
    assert created["printing_plate_1_id"] is None
    assert created["printing_plate_2_id"] is None
    assert created["printing_plate_3_id"] is None
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["printing_colors"] == canonical
    assert reopened.json()["production_label_enabled"] is True
    assert reopened.json()["production_label_units_per_label"] == 5


@pytest.mark.parametrize(
    ("print_content", "printing_colors"),
    [
        ("单色印刷", ""),
        ("双色印刷", "黑色"),
        ("双色印刷", "黑色＋黑色"),
        ("三色印刷", "黑色＋红色"),
        ("单色印刷", "专" * 41),
    ],
)
def test_api_rejects_missing_duplicate_count_and_length_errors_atomically(
    print_color_app: FastAPI,
    print_content: str,
    printing_colors: str,
) -> None:
    from app.models.product import Product

    suffix = str(sum(ord(char) for char in print_content + printing_colors))
    with TestClient(print_color_app) as client:
        customer = _create_customer(client, suffix)
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
    with print_color_app.state.session_factory() as session:
        assert session.query(Product).filter(Product.customer_id == customer["id"]).count() == 0


def test_save_reopen_version_conflict_and_history_restore_keep_color_order(
    print_color_app: FastAPI,
) -> None:
    with TestClient(print_color_app) as client:
        customer = _create_customer(client, "VERSION")
        original = _create_product(
            client,
            customer["id"],
            "VERSION",
            print_content="双色印刷",
            printing_colors="黑色+红色",
        )
        changed_response = client.put(
            f"/api/master/products/{original['id']}",
            json=_update_payload(
                original,
                print_content="三色印刷",
                printing_colors="蓝色、橙色、绿色",
            ),
        )
        assert changed_response.status_code == 200, changed_response.text
        changed = changed_response.json()

        stale = client.put(
            f"/api/master/products/{original['id']}",
            json=_update_payload(
                original,
                print_content="单色印刷",
                printing_colors="红色",
            ),
        )
        current = client.get(f"/api/master/products/{original['id']}")
        history = client.get(
            f"/api/master-data/product/{original['id']}/versions/1"
        )
        preview = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore-preview",
            json={"expected_version": changed["version"]},
        )
        assert preview.status_code == 200, preview.text
        bad_restore = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore",
            json={
                "expected_version": changed["version"],
                "reason": "P1-51A 验证历史恢复门禁",
                "confirmation_token": "invalid-token",
            },
        )
        after_bad_restore = client.get(f"/api/master/products/{original['id']}")
        restored_response = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore",
            json={
                "expected_version": changed["version"],
                "reason": "P1-51A 确认恢复印刷颜色",
                "confirmation_token": preview.json()["confirmation_token"],
            },
        )
        restored_product = client.get(f"/api/master/products/{original['id']}")

    assert changed["version"] == original["version"] + 1
    assert changed["printing_colors"] == "蓝色＋橙色＋绿色"
    assert stale.status_code == 409, stale.text
    assert current.json()["printing_colors"] == "蓝色＋橙色＋绿色"
    assert history.status_code == 200, history.text
    assert history.json()["snapshot"]["printing_colors"] == "黑色＋红色"
    assert bad_restore.status_code == 409, bad_restore.text
    assert after_bad_restore.json()["printing_colors"] == "蓝色＋橙色＋绿色"
    assert restored_response.status_code == 200, restored_response.text
    assert restored_product.status_code == 200, restored_product.text
    assert restored_product.json()["printing_colors"] == "黑色＋红色"
    assert restored_response.json()["version"] == changed["version"] + 1


def _seed_plate_bound_product(app: FastAPI, customer_id: int) -> tuple[int, list[int]]:
    from app.models.printing_plate import PrintingPlate
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.product_drawing import ProductDrawing

    with app.state.session_factory() as session:
        component = Product(
            customer_id=customer_id,
            product_code="P151A-BOM-COMPONENT",
            customer_material_code="P151A-BOM-COMPONENT",
            product_name="P1-51A BOM 内部组件",
            box_category="normal",
            box_style="A1/0201 普通开槽箱",
            supply_mode="corrugated_production",
            printing_plate_mode="no_plate",
            is_active=True,
            version=1,
        )
        product = Product(
            customer_id=customer_id,
            product_code="P151A-PLATE-BOUND",
            customer_material_code="P151A-PLATE-BOUND",
            product_name="P1-51A 原挂板常用箱",
            box_category="normal",
            box_style="A1/0201 普通开槽箱",
            supply_mode="corrugated_production",
            length_mm=380,
            width_mm=260,
            height_mm=220,
            print_content="多色印刷",
            printing_colors="黑色,红色/蓝色",
            printing_plate_mode="plate",
            plate_alignment_value_mm=1,
            plate_mount_value_mm=2,
            machine_set_length_mm=381,
            machine_set_width_mm=261,
            machine_set_height_mm=221,
            production_label_enabled=True,
            production_label_units_per_label=5,
            remark="不得丢失兄弟字段",
            is_active=True,
            version=1,
        )
        session.add_all([component, product])
        session.flush()
        plates = [
            PrintingPlate(
                plate_code=f"P151A-PLATE-{index}",
                customer_id=customer_id,
                plate_name=f"P1-51A 原挂板 {index}",
                color_name=color,
                rack_location=f"P151A-RACK-{index}",
                status="active",
                created_by=app.state.admin_id,
            )
            for index, color in enumerate(("黑色", "红色", "蓝色"), start=1)
        ]
        session.add_all(plates)
        session.flush()
        (
            product.printing_plate_1_id,
            product.printing_plate_2_id,
            product.printing_plate_3_id,
        ) = tuple(plate.id for plate in plates)
        session.add(
            ProductDrawing(
                product_id=product.id,
                image_path="product_drawings/p151a/original.png",
                thumbnail_path="product_drawings/p151a/thumbnail.png",
                uploaded_by=app.state.admin_id,
            )
        )
        session.add(
            ProductBomComponent(
                parent_product_id=product.id,
                component_product_id=component.id,
                quantity_per_set=2,
                display_order=0,
                internal_component_code="P151A-COMP-01",
                is_die_cut=False,
                spare_sheet_quantity=0,
                display_mode="internal_only",
                show_on_delivery=False,
                is_required=True,
            )
        )
        session.commit()
        return int(product.id), [int(plate.id) for plate in plates]


@pytest.mark.parametrize(
    ("print_content", "printing_colors"),
    [
        ("无印刷", "请忽略这个客户端旧值"),
        ("双色印刷", "红色＋黑色"),
    ],
)
def test_no_print_and_direct_print_unlink_plates_without_deleting_evidence(
    print_color_app: FastAPI,
    print_content: str,
    printing_colors: str,
) -> None:
    from app.models.printing_plate import PrintingPlate
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.product_drawing import ProductDrawing

    suffix = "NO-PRINT" if print_content == "无印刷" else "DIRECT"
    with TestClient(print_color_app) as client:
        customer = _create_customer(client, suffix)
        product_id, plate_ids = _seed_plate_bound_product(
            print_color_app, customer["id"]
        )
        before = client.get(f"/api/master/products/{product_id}")
        assert before.status_code == 200, before.text
        assert before.json()["print_content"] == "多色印刷"
        assert before.json()["printing_colors"] == "黑色,红色/蓝色"
        first_response = client.put(
            f"/api/master/products/{product_id}",
            json=_update_payload(
                before.json(),
                print_content=print_content,
                printing_colors=printing_colors,
                printing_plate_mode="no_plate",
            ),
        )
        assert first_response.status_code == 409, first_response.text
        detail = first_response.json()["detail"]
        assert detail["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
        response = client.put(
            f"/api/master/products/{product_id}",
            json=_update_payload(
                before.json(),
                print_content=print_content,
                printing_colors=printing_colors,
                printing_plate_mode="no_plate",
                confirmation_token=detail["confirmation_token"],
            ),
        )

    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["printing_colors"] == (
        None if print_content == "无印刷" else "红色＋黑色"
    )
    assert saved["printing_plate_mode"] == "no_plate"
    assert [saved[f"printing_plate_{index}_id"] for index in (1, 2, 3)] == [
        None,
        None,
        None,
    ]
    assert saved["length_mm"] == "380.00"
    assert saved["width_mm"] == "260.00"
    assert saved["height_mm"] == "220.00"
    assert saved["remark"] == "不得丢失兄弟字段"
    assert saved["production_label_enabled"] is True
    assert saved["production_label_units_per_label"] == 5
    with print_color_app.state.session_factory() as session:
        stored = session.get(Product, product_id)
        assert stored is not None
        assert session.query(PrintingPlate).filter(PrintingPlate.id.in_(plate_ids)).count() == 3
        assert session.query(ProductDrawing).filter_by(product_id=product_id).count() == 1
        assert (
            session.query(ProductBomComponent)
            .filter_by(parent_product_id=product_id)
            .count()
            == 1
        )


def test_legacy_blank_direct_color_remains_readable_without_backfill(
    print_color_app: FastAPI,
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    with print_color_app.state.session_factory() as session:
        customer = Customer(
            customer_number=5199,
            customer_code="P151A-LEGACY",
            name="P1-51A 旧空颜色客户",
        )
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P151A-LEGACY-BLANK",
            customer_material_code="P151A-LEGACY-BLANK",
            product_name="P1-51A 旧空颜色常用箱",
            box_category="normal",
            box_style="A1/0201 普通开槽箱",
            supply_mode="corrugated_production",
            print_content="单色印刷",
            printing_colors=None,
            printing_plate_mode="no_plate",
            is_active=True,
            version=1,
        )
        session.add(product)
        session.commit()
        product_id = int(product.id)

    with TestClient(print_color_app) as client:
        response = client.get(f"/api/master/products/{product_id}")

    assert response.status_code == 200, response.text
    assert response.json()["print_content"] == "单色印刷"
    assert response.json()["printing_colors"] is None
    with print_color_app.state.session_factory() as session:
        assert session.get(Product, product_id).printing_colors is None


def test_legacy_multi_color_text_survives_unrelated_update_without_rewrite(
    print_color_app: FastAPI,
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    legacy_raw = "黑色,红色／蓝色|绿色"
    with print_color_app.state.session_factory() as session:
        customer = Customer(
            customer_number=5198,
            customer_code="P151A-LEGACY-MULTI",
            name="P1-51A 旧多色客户",
        )
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P151A-LEGACY-MULTI",
            customer_material_code="P151A-LEGACY-MULTI",
            product_name="P1-51A 旧多色常用箱",
            box_category="normal",
            box_style="A1/0201 普通开槽箱",
            supply_mode="corrugated_production",
            print_content="多色印刷",
            printing_colors=legacy_raw,
            printing_plate_mode="no_plate",
            splice_mode="single",
            pieces_per_box=1,
            default_cutting_mode="一开一",
            flap_mm=30,
            remark="原备注",
            is_active=True,
            version=1,
        )
        session.add(product)
        session.commit()
        product_id = int(product.id)

    with TestClient(print_color_app) as client:
        before = client.get(f"/api/master/products/{product_id}")
        assert before.status_code == 200, before.text
        assert before.json()["printing_colors"] == legacy_raw
        updated = client.put(
            f"/api/master/products/{product_id}",
            json=_update_payload(before.json(), remark="只修改备注"),
        )
        reopened = client.get(f"/api/master/products/{product_id}")

    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["remark"] == "只修改备注"
    assert updated.json()["print_content"] == "多色印刷"
    assert updated.json()["printing_colors"] == legacy_raw
    assert reopened.json()["printing_colors"] == legacy_raw


def test_product_color_update_preserves_permission_and_customer_scope_guards(
    print_color_app: FastAPI,
) -> None:
    from app.models.access_control import UserCustomerScope
    from app.models.user import User

    with TestClient(print_color_app) as client:
        allowed_customer = _create_customer(client, "SCOPE-YES")
        denied_customer = _create_customer(client, "SCOPE-NO")
        denied_product = _create_product(
            client,
            denied_customer["id"],
            "SCOPE-NO",
            print_content="单色印刷",
            printing_colors="黑色",
        )
        with print_color_app.state.session_factory() as session:
            sales = User(
                username="p1-51a-sales",
                password_hash="not-used",
                role="sales",
                real_name="P1-51A Sales",
                is_active=True,
                must_change_password=False,
                customer_access_mode="selected",
            )
            workshop = User(
                username="p1-51a-workshop",
                password_hash="not-used",
                role="workshop",
                real_name="P1-51A Workshop",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
            )
            session.add_all([sales, workshop])
            session.flush()
            session.add(
                UserCustomerScope(
                    user_id=sales.id,
                    customer_id=allowed_customer["id"],
                    assigned_by=print_color_app.state.admin_id,
                )
            )
            session.commit()
            sales_id = int(sales.id)
            workshop_id = int(workshop.id)

        payload = _update_payload(
            denied_product,
            print_content="双色印刷",
            printing_colors="黑色＋红色",
        )
        print_color_app.state.current_user_id = sales_id
        scope_denied = client.put(
            f"/api/master/products/{denied_product['id']}", json=payload
        )
        print_color_app.state.current_user_id = workshop_id
        permission_denied = client.put(
            f"/api/master/products/{denied_product['id']}", json=payload
        )
        print_color_app.state.current_user_id = print_color_app.state.admin_id
        reopened = client.get(f"/api/master/products/{denied_product['id']}")

    assert scope_denied.status_code == 403, scope_denied.text
    assert permission_denied.status_code == 403, permission_denied.text
    assert reopened.json()["version"] == denied_product["version"]
    assert reopened.json()["printing_colors"] == "黑色"


def test_common_box_ui_exposes_ordered_colors_without_persisting_suggestion() -> None:
    index = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert '<option value="无印刷">无印刷</option>' in index
    assert '<option value="单色印刷">单色印刷</option>' in index
    assert '<option value="双色印刷">双色印刷</option>' in index
    assert '<option value="三色印刷">三色印刷</option>' in index
    assert '<option value="挂板印刷">挂板印刷</option>' in index
    assert 'maxlength="40"' in index
    for color in ("黑色", "红色", "蓝色", "橙色", "绿色"):
        assert f'<option value="{color}"></option>' in index
    assert 'class="select product-printing-color-preset"' in index
    for value, label in (("黑色＋红色", "黑＋红"), ("黑色＋绿色", "黑＋绿"), ("黑色＋蓝色", "黑＋蓝")):
        assert f'<option value="{value}">{label}</option>' in index
    assert "颜色待完善" in index
    assert "printing_colors:" in index
    assert "_printing_colors: []" in index
    assert "join('＋')" in index or 'join("＋")' in index
