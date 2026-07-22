from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import os
import sys
import types
from urllib.parse import quote

import jwt
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


def _image_bytes(image_format: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, image_format)
    return output.getvalue()


TINY_PNG = _image_bytes("PNG")
TINY_WEBP = _image_bytes("WEBP")


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text


def _order_payload(customer_id: int, product_id: int, draft_reference: str) -> dict:
    return {
        "customer_id": customer_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "unit_price": "9.00",
                "temp_drawing_file": draft_reference,
                "drawing_save_option": "order_only",
            }
        ],
    }


@pytest.fixture()
def formal_private_drawing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    try:
        __import__("cv2")
    except ModuleNotFoundError:
        # The formal app imports an optional OCR module that is unrelated to
        # drawing authorization. Keep this integration test independent from
        # the workstation's OpenCV installation.
        monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))
    import app.services.product_drawings as drawing_service
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.main import create_app
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_drawing import ProductDrawing
    from app.models.user import User

    database_path = tmp_path / "formal-private-drawings.sqlite3"
    private_drawing_root = tmp_path / "private" / "drawings"
    private_draft_root = tmp_path / "private" / "order_drafts"
    legacy_drawing_root = tmp_path / "legacy-static" / "uploads" / "drawings"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_DRAWING_DIR", str(private_drawing_root))
    monkeypatch.setenv("ERP_ORDER_DRAFT_DRAWING_DIR", str(private_draft_root))
    monkeypatch.setenv("ERP_ORDER_DRAFT_TTL_SECONDS", "3600")
    monkeypatch.setattr(drawing_service, "LEGACY_DRAWING_ROOT", legacy_drawing_root)
    private_drawing_root.mkdir(parents=True)
    legacy_drawing_root.mkdir(parents=True)

    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user_a = User(
            username="n032-user-a",
            password_hash=hash_password("UserAPass123!"),
            role="sales",
            real_name="User A",
            must_change_password=False,
            customer_access_mode="selected",
        )
        user_b = User(
            username="n032-user-b",
            password_hash=hash_password("UserBPass123!"),
            role="sales",
            real_name="User B",
            must_change_password=False,
            customer_access_mode="selected",
        )
        user_no_view = User(
            username="n032-no-view",
            password_hash=hash_password("NoViewPass123!"),
            role="sales",
            real_name="No View",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(
            customer_number=3201,
            customer_code="N032-A",
            name="N032 Customer A",
        )
        customer_b = Customer(
            customer_number=3202,
            customer_code="N032-B",
            name="N032 Customer B",
        )
        product_a = Product(
            customer=customer_a,
            product_code="N032-A-PRODUCT",
            customer_material_code="N032-A-PRODUCT",
            product_name="N032 A carton",
            box_category="normal",
            sale_unit_price=Decimal("9.00"),
        )
        product_b = Product(
            customer=customer_b,
            product_code="N032-B-PRODUCT",
            customer_material_code="N032-B-PRODUCT",
            product_name="N032 B carton",
            box_category="normal",
            sale_unit_price=Decimal("9.00"),
        )
        db.add_all(
            [
                user_a,
                user_b,
                user_no_view,
                customer_a,
                customer_b,
                product_a,
                product_b,
            ]
        )
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=user_a.id, customer_id=customer_a.id),
                UserCustomerScope(user_id=user_b.id, customer_id=customer_b.id),
                UserCustomerScope(
                    user_id=user_no_view.id,
                    customer_id=customer_a.id,
                ),
                UserPermissionOverride(
                    user_id=user_no_view.id,
                    permission_code="orders.view",
                    is_allowed=False,
                    granted_by=user_a.id,
                ),
                UserPermissionOverride(
                    user_id=user_no_view.id,
                    permission_code="products.view",
                    is_allowed=False,
                    granted_by=user_a.id,
                ),
            ]
        )
        order_a = Order(
            order_number="N032-ORDER-A",
            customer_id=customer_a.id,
            order_date=date(2026, 7, 22),
            total_amount=Decimal("9.00"),
            created_by=user_a.id,
        )
        order_b = Order(
            order_number="N032-ORDER-B",
            customer_id=customer_b.id,
            order_date=date(2026, 7, 22),
            total_amount=Decimal("9.00"),
            created_by=user_b.id,
        )
        db.add_all([order_a, order_b])
        db.flush()
        legacy_filename = "legacy-order-a.webp"
        private_filename = "private-product-b.webp"
        ambiguous_filename = "ambiguous.webp"
        db.add_all(
            [
                OrderItem(
                    order_id=order_a.id,
                    product_id=product_a.id,
                    quantity=1,
                    unit_price=Decimal("9.00"),
                    subtotal=Decimal("9.00"),
                    snapshot_product_name=product_a.product_name,
                    drawing_file=f"/static/uploads/drawings/{legacy_filename}",
                ),
                OrderItem(
                    order_id=order_a.id,
                    product_id=product_a.id,
                    quantity=1,
                    unit_price=Decimal("9.00"),
                    subtotal=Decimal("9.00"),
                    snapshot_product_name=product_a.product_name,
                    drawing_file=f"/static/uploads/drawings/{ambiguous_filename}",
                ),
                ProductDrawing(
                    product_id=product_b.id,
                    image_path=f"/static/uploads/drawings/{private_filename}",
                    thumbnail_path=f"/static/uploads/drawings/{private_filename}",
                    uploaded_by=user_b.id,
                ),
                ProductDrawing(
                    product_id=product_b.id,
                    image_path=f"/static/uploads/drawings/{ambiguous_filename}",
                    thumbnail_path=f"/static/uploads/drawings/{ambiguous_filename}",
                    uploaded_by=user_b.id,
                ),
            ]
        )
        db.commit()
        ids = {
            "user_a": user_a.id,
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "product_a": product_a.id,
            "product_b": product_b.id,
        }

    (legacy_drawing_root / legacy_filename).write_bytes(TINY_WEBP)
    (private_drawing_root / private_filename).write_bytes(TINY_WEBP)
    (private_drawing_root / ambiguous_filename).write_bytes(TINY_WEBP)

    application = create_app()

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_get_db
    try:
        yield application, ids, factory, private_drawing_root, private_draft_root
    finally:
        application.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_create_app_static_mount_rewrites_formal_drawings_to_customer_auth(
    formal_private_drawing_app,
) -> None:
    app, _, _, _, _ = formal_private_drawing_app
    legacy_url = "/static/uploads/drawings/legacy-order-a.webp"
    private_url = "/static/uploads/drawings/private-product-b.webp"
    ambiguous_url = "/static/uploads/drawings/ambiguous.webp"
    windows_equivalent_urls = (
        "/static/uploads/drawings%5Clegacy-order-a.webp",
        "/static/uploads/x/%2e%2e/drawings/legacy-order-a.webp",
        "/static/x/%2e%2e/uploads/drawings/legacy-order-a.webp",
        "/static/UPLOADS/DRAWINGS/legacy-order-a.webp",
        "/static/uploads./drawings/legacy-order-a.webp",
        "/static/uploads%20/drawings/legacy-order-a.webp",
        "/static/uploads/drawings./legacy-order-a.webp",
        "/static/%2e%2e/static/uploads/drawings/legacy-order-a.webp",
    )
    with TestClient(app) as client:
        assert client.get("/static/index.html").status_code == 200
        assert client.get(legacy_url).status_code == 401
        assert client.head(legacy_url).status_code == 401
        for equivalent_url in windows_equivalent_urls:
            assert client.get(equivalent_url).status_code in {401, 404}, equivalent_url
            assert client.head(equivalent_url).status_code in {401, 404}, equivalent_url

        _login(client, "n032-user-a", "UserAPass123!")
        allowed = client.get(legacy_url)
        allowed_head = client.head(legacy_url)
        assert allowed.status_code == 200
        assert allowed.content == TINY_WEBP
        assert allowed.headers["cache-control"] == "private, no-store"
        assert allowed.headers["x-content-type-options"] == "nosniff"
        assert allowed_head.status_code == 200
        assert allowed_head.content == b""
        assert allowed_head.headers["content-length"] == str(len(TINY_WEBP))
        for equivalent_url in windows_equivalent_urls:
            equivalent = client.get(equivalent_url)
            assert equivalent.status_code in {200, 404}, equivalent_url
            if equivalent.status_code == 200:
                assert equivalent.content == TINY_WEBP
                assert equivalent.headers["cache-control"] == "private, no-store"
        assert client.get(private_url).status_code == 403
        assert client.get(ambiguous_url).status_code == 404
        assert client.get("/static/uploads/drawings/unknown.webp").status_code == 404

        client.cookies.clear()
        _login(client, "n032-user-b", "UserBPass123!")
        assert client.get(legacy_url).status_code == 403
        assert client.head(legacy_url).status_code == 403
        assert client.get(private_url).status_code == 200

        client.cookies.clear()
        _login(client, "n032-no-view", "NoViewPass123!")
        assert client.get(legacy_url).status_code == 403
        assert client.head(legacy_url).status_code == 403


def test_public_static_files_physically_excludes_uploads_for_windows_aliases(
    tmp_path: Path,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from main import PublicStaticFiles

    static_root = tmp_path / "static"
    secret = static_root / "uploads" / "drawings" / "secret.webp"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(TINY_WEBP)
    (static_root / "public.css").write_text("body{}", encoding="utf-8")
    static_app = FastAPI()
    static_app.mount("/static", PublicStaticFiles(directory=static_root))
    encoded_absolute = quote(str(secret), safe="")
    attempts = (
        "/static/uploads/drawings/secret.webp",
        "/static/uploads/drawings%5Csecret.webp",
        "/static/x/%2e%2e/uploads/drawings/secret.webp",
        "/static/%2e%2e/static/uploads/drawings/secret.webp",
        "/static/UPLOADS/DRAWINGS/secret.webp",
        "/static/uploads./drawings/secret.webp",
        "/static/uploads%20/drawings/secret.webp",
        f"/static/{encoded_absolute}",
    )
    with TestClient(static_app) as client:
        assert client.get("/static/public.css").status_code == 200
        for attempt in attempts:
            assert client.get(attempt).status_code == 404, attempt
            assert client.head(attempt).status_code == 404, attempt


def test_draft_reference_is_owner_bound_tamper_evident_and_consumed(
    formal_private_drawing_app,
) -> None:
    from app.models.order import Order
    from app.services.product_drawings import ORDER_DRAFT_URL_PREFIX

    app, ids, factory, private_drawing_root, private_draft_root = formal_private_drawing_app
    with factory() as db:
        baseline_orders = db.scalar(select(func.count()).select_from(Order))
    baseline_final_files = {path.name for path in private_drawing_root.iterdir()}

    with TestClient(app) as client:
        _login(client, "n032-user-a", "UserAPass123!")
        upload = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("draft.png", TINY_PNG, "image/png")},
        )
        assert upload.status_code == 200, upload.text
        reference = upload.json()["temp_path"]
        assert reference.startswith(f"{ORDER_DRAFT_URL_PREFIX}/")
        assert client.get(reference).status_code == 200
        assert client.head(reference).status_code == 200
        assert len(list(private_draft_root.glob("*"))) == 1

        client.cookies.clear()
        _login(client, "n032-user-b", "UserBPass123!")
        assert client.get(reference).status_code == 404
        rejected = client.post(
            "/api/orders",
            json=_order_payload(ids["customer_b"], ids["product_b"], reference),
        )
        assert rejected.status_code == 400

        client.cookies.clear()
        _login(client, "n032-user-a", "UserAPass123!")
        remainder = reference[len(ORDER_DRAFT_URL_PREFIX) + 1 :]
        token, filename = remainder.split("/", 1)
        header, body, signature = token.split(".")
        replacement = "A" if signature[0] != "A" else "B"
        tampered = (
            f"{ORDER_DRAFT_URL_PREFIX}/{header}.{body}.{replacement}{signature[1:]}/{filename}"
        )
        assert client.get(tampered).status_code == 404
        assert client.post(
            "/api/orders",
            json=_order_payload(ids["customer_a"], ids["product_a"], tampered),
        ).status_code == 400

        payload = jwt.decode(token, options={"verify_signature": False})
        payload["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
        expired_token = jwt.encode(payload, os.environ["ERP_SECRET_KEY"], algorithm="HS256")
        expired = f"{ORDER_DRAFT_URL_PREFIX}/{expired_token}/{filename}"
        assert client.get(expired).status_code == 404
        assert client.post(
            "/api/orders",
            json=_order_payload(ids["customer_a"], ids["product_a"], expired),
        ).status_code == 400

        success = client.post(
            "/api/orders",
            json=_order_payload(ids["customer_a"], ids["product_a"], reference),
        )
        assert success.status_code == 201, success.text

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == baseline_orders + 1
    assert list(private_draft_root.glob("*")) == []
    assert len({path.name for path in private_drawing_root.iterdir()} - baseline_final_files) == 1


def test_draft_auth_version_change_invalidates_old_signed_reference(
    formal_private_drawing_app,
) -> None:
    from app.models.user import User

    app, ids, factory, _, _ = formal_private_drawing_app
    with TestClient(app) as client:
        _login(client, "n032-user-a", "UserAPass123!")
        upload = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("draft.png", TINY_PNG, "image/png")},
        )
        reference = upload.json()["temp_path"]
        with factory() as db:
            user = db.get(User, ids["user_a"])
            user.auth_version += 1
            db.commit()
        assert client.get(reference).status_code == 401
        client.cookies.clear()
        _login(client, "n032-user-a", "UserAPass123!")
        assert client.get(reference).status_code == 404


def test_private_root_rejects_reparse_ancestor_without_creating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.product_drawings import (
        DrawingValidationError,
        save_order_draft_drawing_files,
    )

    reparse_parent = tmp_path / "simulated-reparse-parent"
    reparse_parent.mkdir()
    original_lstat = Path.lstat

    def lstat_with_reparse_flag(path: Path):
        result = original_lstat(path)
        if path == reparse_parent:
            return types.SimpleNamespace(
                st_file_attributes=getattr(result, "st_file_attributes", 0) | 0x400,
                st_mode=result.st_mode,
                st_nlink=result.st_nlink,
            )
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_flag)
    monkeypatch.setenv(
        "ERP_ORDER_DRAFT_DRAWING_DIR",
        str(reparse_parent / "drafts"),
    )
    with pytest.raises(DrawingValidationError, match="符号链接|联接点"):
        save_order_draft_drawing_files(
            content=TINY_PNG,
            content_type="image/png",
            owner_user_id=1,
            owner_auth_version=1,
        )
    assert list(reparse_parent.rglob("*")) == []


@pytest.mark.parametrize("environment", ["development", "production"])
def test_non_test_acl_gate_rejects_broad_write_and_accepts_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    import app.services.product_drawings as drawing_service

    class RuntimeSettings:
        pass

    runtime_settings = RuntimeSettings()
    runtime_settings.environment = environment

    monkeypatch.setattr(drawing_service.os, "name", "nt")
    monkeypatch.setattr(drawing_service, "load_settings", lambda: runtime_settings)
    for broad_sid in ("AU", "S-1-5-11"):
        monkeypatch.setattr(
            drawing_service,
            "_windows_directory_sddl",
            lambda _path, sid=broad_sid: f"D:P(A;;GW;;;{sid})(A;;FA;;;SY)",
        )
        with pytest.raises(
            drawing_service.DrawingValidationError,
            match="普通登录用户写入",
        ):
            drawing_service.verify_private_storage_acl(tmp_path)

    for broad_sid in ("AU", "S-1-5-11"):
        monkeypatch.setattr(
            drawing_service,
            "_windows_directory_sddl",
            lambda _path, sid=broad_sid: f"D:P(A;OICIIO;GA;;;{sid})(A;;FA;;;SY)",
        )
        with pytest.raises(
            drawing_service.DrawingValidationError,
            match="普通登录用户写入",
        ):
            drawing_service.verify_private_storage_acl(tmp_path)

    monkeypatch.setattr(
        drawing_service,
        "_windows_directory_sddl",
        lambda _path: "D:P(A;;GRGX;;;BU)(A;;FA;;;SY)",
    )
    drawing_service.verify_private_storage_acl(tmp_path)

    monkeypatch.setattr(
        drawing_service,
        "_windows_directory_sddl",
        lambda _path: "D:(A;;DC;;;S-1-5-32-545)(A;;FA;;;SY)",
    )
    with pytest.raises(
        drawing_service.DrawingValidationError,
        match="祖先目录可被普通用户替换",
    ):
        drawing_service.verify_storage_ancestor_acls(tmp_path / "trust-root")

    monkeypatch.setattr(
        drawing_service,
        "_windows_directory_sddl",
        lambda _path: "D:(A;;GWGR;;;AU)(A;;FA;;;SY)",
    )
    drawing_service.verify_storage_ancestor_acls(tmp_path / "trust-root")


def test_development_runtime_requires_explicit_private_storage_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.product_drawings as drawing_service

    class DevelopmentSettings:
        environment = "development"

    for name in (
        "ERP_DRAWING_DIR",
        "ERP_ORDER_DRAFT_DRAWING_DIR",
        "ERP_PRIVATE_STORAGE_TRUST_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        drawing_service,
        "load_settings",
        lambda: DevelopmentSettings(),
    )
    with pytest.raises(
        drawing_service.DrawingValidationError,
        match="必须显式配置",
    ):
        drawing_service.verify_private_drawing_storage()
