from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from io import BytesIO
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


def _tiny_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, "PNG")
    return output.getvalue()


TINY_PNG = _tiny_png()


def _image_bytes(
    image_format: str,
    *,
    size: tuple[int, int] = (2, 2),
    mode: str = "RGB",
) -> bytes:
    output = BytesIO()
    Image.new(mode, size, "white").save(output, image_format)
    return output.getvalue()


def _pdf_bytes(active_kind: str | None = None) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    if active_kind == "javascript":
        action = DictionaryObject(
            {
                NameObject("/S"): NameObject("/JavaScript"),
                NameObject("/JS"): TextStringObject("app.alert('unsafe')"),
            }
        )
        writer.root_object[NameObject("/TMAction")] = writer._add_object(action)
    elif active_kind == "indirect_javascript":
        action = DictionaryObject(
            {
                NameObject("/S"): writer._add_object(NameObject("/JavaScript")),
            }
        )
        writer.root_object[NameObject("/TMAction")] = writer._add_object(action)
    elif active_kind == "launch":
        action = DictionaryObject(
            {
                NameObject("/S"): NameObject("/Launch"),
                NameObject("/F"): TextStringObject("unsafe.exe"),
            }
        )
        writer.root_object[NameObject("/TMAction")] = writer._add_object(action)
    elif active_kind == "open_action":
        writer.root_object[NameObject("/OpenAction")] = ArrayObject(
            [page.indirect_reference, NameObject("/Fit")]
        )
    elif active_kind == "xfa_initialize":
        xfa = DecodedStreamObject()
        xfa.set_data(
            b"""<?xml version="1.0" encoding="UTF-8"?>
<xdp:xdp xmlns:xdp="http://ns.adobe.com/xdp/">
  <template xmlns="http://www.xfa.org/schema/xfa-template/3.3/">
    <subform name="form1">
      <event activity="initialize">
        <script contentType="application/x-javascript">app.alert('unsafe XFA');</script>
      </event>
    </subform>
  </template>
</xdp:xdp>"""
        )
        acro_form = DictionaryObject(
            {
                NameObject("/Fields"): ArrayObject(),
                NameObject("/XFA"): writer._add_object(xfa),
            }
        )
        writer.root_object[NameObject("/AcroForm")] = writer._add_object(acro_form)
    elif active_kind == "uri":
        action = DictionaryObject(
            {
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject("https://attacker.invalid/"),
            }
        )
        annotation = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(100), FloatObject(100)]
                ),
                NameObject("/A"): writer._add_object(action),
            }
        )
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    elif active_kind == "external_stream":
        external_stream = DecodedStreamObject()
        external_stream.set_data(b"")
        external_stream[NameObject("/F")] = TextStringObject("external.bin")
        writer.root_object[NameObject("/TMExternalStream")] = writer._add_object(
            external_stream
        )
    elif active_kind == "filespec":
        filespec = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Filespec"),
                NameObject("/F"): TextStringObject("external.pdf"),
            }
        )
        writer.root_object[NameObject("/TMFileSpec")] = writer._add_object(filespec)
    elif active_kind == "internal_goto":
        action = DictionaryObject(
            {
                NameObject("/S"): NameObject("/GoTo"),
                NameObject("/D"): ArrayObject(
                    [page.indirect_reference, NameObject("/Fit")]
                ),
            }
        )
        writer.root_object[NameObject("/TMAction")] = writer._add_object(action)
    elif active_kind == "ordinary_annotation_flags":
        annotation = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Text"),
                NameObject("/Rect"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(10), FloatObject(10)]
                ),
                NameObject("/F"): NumberObject(4),
            }
        )
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    elif active_kind == "embedded_file":
        writer.add_attachment("unsafe.txt", b"unsafe")
    elif active_kind == "rich_media":
        writer.root_object[NameObject("/RichMedia")] = DictionaryObject()
    elif active_kind == "rich_media_subtype":
        writer.root_object[NameObject("/TMAnnotation")] = writer._add_object(
            DictionaryObject(
                {NameObject("/Subtype"): NameObject("/RichMedia")}
            )
        )
    elif active_kind in {"submit_form", "import_data"}:
        action_name = "/SubmitForm" if active_kind == "submit_form" else "/ImportData"
        action = DictionaryObject({NameObject("/S"): NameObject(action_name)})
        writer.root_object[NameObject("/TMAction")] = writer._add_object(action)
    elif active_kind in {
        "goto_remote",
        "goto_embedded",
        "rendition",
        "movie",
        "sound",
        "transition",
        "named",
        "set_ocg_state",
        "thread",
    }:
        action_name = {
            "goto_remote": "/GoToR",
            "goto_embedded": "/GoToE",
            "rendition": "/Rendition",
            "movie": "/Movie",
            "sound": "/Sound",
            "transition": "/Trans",
            "named": "/Named",
            "set_ocg_state": "/SetOCGState",
            "thread": "/Thread",
        }[active_kind]
        action = DictionaryObject({NameObject("/S"): NameObject(action_name)})
        writer.root_object[NameObject("/TMAction")] = writer._add_object(action)
    elif active_kind in {
        "screen_subtype",
        "movie_subtype",
        "sound_subtype",
        "three_d_subtype",
        "widget_subtype",
        "projection_subtype",
    }:
        subtype = {
            "screen_subtype": "/Screen",
            "movie_subtype": "/Movie",
            "sound_subtype": "/Sound",
            "three_d_subtype": "/3D",
            "widget_subtype": "/Widget",
            "projection_subtype": "/Projection",
        }[active_kind]
        writer.root_object[NameObject("/TMAnnotation")] = writer._add_object(
            DictionaryObject({NameObject("/Subtype"): NameObject(subtype)})
        )
    writer.write(output)
    return output.getvalue()


TINY_PDF = _pdf_bytes()


@pytest.fixture()
def n028_order_drawing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "ERP_ORDER_DRAFT_DRAWING_DIR",
        str(tmp_path / "uploads" / "order_drafts"),
    )
    monkeypatch.setenv(
        "ERP_DRAWING_DIR",
        str(tmp_path / "uploads" / "drawings"),
    )
    engine = create_sqlite_engine(tmp_path / "n028-order-drawings.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n028-drawing-admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="Drawing Admin",
            must_change_password=False,
        )
        denied_sales = User(
            username="n028-drawing-sales",
            password_hash=hash_password("SalesPass123!"),
            role="sales",
            real_name="Drawing Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(
            customer_number=2801,
            customer_code="N028-DRAWING",
            name="N028 Drawing Customer",
        )
        product = Product(
            customer=customer,
            product_code="N028-DRAWING-PRODUCT",
            customer_material_code="N028-DRAWING-PRODUCT",
            product_name="N028 drawing product",
            box_category="normal",
            sale_unit_price=Decimal("9.00"),
        )
        db.add_all([admin, denied_sales, customer, product])
        db.flush()
        order = Order(
            order_number="N028-DRAWING-ORDER",
            customer_id=customer.id,
            order_date=date(2026, 7, 13),
            total_amount=Decimal("9.00"),
            created_by=admin.id,
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=1,
            unit_price=Decimal("9.00"),
            subtotal=Decimal("9.00"),
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
        )
        db.add_all(
            [
                UserCustomerScope(
                    user_id=denied_sales.id,
                    customer_id=customer.id,
                ),
                UserPermissionOverride(
                    user_id=denied_sales.id,
                    permission_code="products.edit",
                    is_allowed=False,
                    granted_by=admin.id,
                ),
                item,
            ]
        )
        db.commit()
        ids = {
            "customer": customer.id,
            "product": product.id,
            "item": item.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def _save_to_product_order_payload(
    customer_id: int,
    product_id: int,
    temp_drawing_file: str,
) -> dict:
    return {
        "customer_id": customer_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "unit_price": "9.00",
                "temp_drawing_file": temp_drawing_file,
                "drawing_save_option": "save_to_product",
            }
        ],
    }


def test_explicit_products_edit_deny_blocks_both_product_drawing_paths_before_persistence(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-sales", "SalesPass123!")
        standalone = client.post(
            f"/api/orders/items/{ids['item']}/drawing",
            params={"save_to_product": "true"},
            files={"file": ("drawing.png", b"drawing", "image/png")},
        )
        assert standalone.status_code == 403

        draft = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("drawing.png", TINY_PNG, "image/png")},
        )
        assert draft.status_code == 200, draft.text

        create = client.post(
            "/api/orders",
            json=_save_to_product_order_payload(
                ids["customer"],
                ids["product"],
                draft.json()["temp_path"],
            ),
        )
        assert create.status_code == 403

    with factory() as db:
        item = db.get(OrderItem, ids["item"])
        assert item is not None
        assert item.drawing_file is None
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1


def test_products_edit_allows_order_and_standalone_product_drawing_saves(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("drawing.png", TINY_PNG, "image/png")},
        )
        assert draft.status_code == 200, draft.text
        assert draft.json()["temp_path"].startswith(
            "/static/uploads/order_drafts/"
        )
        assert "/draft_" in draft.json()["temp_path"]
        assert draft.json()["temp_path"].endswith(".webp")
        created = client.post(
            "/api/orders",
            json=_save_to_product_order_payload(
                ids["customer"],
                ids["product"],
                draft.json()["temp_path"],
            ),
        )
        assert created.status_code == 201
        created_item_id = created.json()["items"][0]["id"]

        standalone = client.post(
            f"/api/orders/items/{created_item_id}/drawing",
            params={"save_to_product": "true"},
            files={"file": ("replacement.png", TINY_PNG, "image/png")},
        )
        assert standalone.status_code == 200
        assert standalone.json()["saved_to_product"] is True

    with factory() as db:
        drawings = db.scalars(
            select(ProductDrawing)
            .where(ProductDrawing.product_id == ids["product"])
            .order_by(ProductDrawing.id)
        ).all()
        assert len(drawings) == 2
        assert db.get(OrderItem, created_item_id).drawing_file is not None
        assert all(row.image_path != row.thumbnail_path for row in drawings)
        drawing_root = Path(os.environ["ERP_DRAWING_DIR"])
        for row in drawings:
            assert (drawing_root / Path(row.image_path).name).is_file()
            assert (drawing_root / Path(row.thumbnail_path).name).is_file()
    assert len(list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*"))) == 4
    draft_files = list(
        Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"]).glob("*")
    )
    assert draft_files == []


def test_order_only_png_keeps_high_image_without_orphan_thumbnail(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("drawing.png", TINY_PNG, "image/png")},
        )
        assert draft.status_code == 200, draft.text
        payload = _save_to_product_order_payload(
            ids["customer"],
            ids["product"],
            draft.json()["temp_path"],
        )
        payload["items"][0]["drawing_save_option"] = "order_only"
        created = client.post("/api/orders", json=payload)

    assert created.status_code == 201, created.text
    created_item_id = created.json()["items"][0]["id"]
    final_files = list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*"))
    assert len(final_files) == 1
    assert "_thumb" not in final_files[0].name
    with factory() as db:
        item = db.get(OrderItem, created_item_id)
        assert item is not None
        assert Path(item.drawing_file).name == final_files[0].name
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0


def test_order_item_upload_without_product_save_creates_no_thumbnail(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        response = client.post(
            f"/api/orders/items/{ids['item']}/drawing",
            files={"file": ("order-only.png", TINY_PNG, "image/png")},
        )

    assert response.status_code == 200, response.text
    final_files = list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*"))
    assert len(final_files) == 1
    assert "_thumb" not in final_files[0].name
    with factory() as db:
        item = db.get(OrderItem, ids["item"])
        assert item is not None
        assert Path(item.drawing_file).name == final_files[0].name
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0


def test_order_item_upload_commit_failure_removes_new_files_and_rolls_back(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app

    def fail_commit(_session) -> None:
        raise RuntimeError("injected order item commit failure")

    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        event.listen(factory.class_, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="injected order item commit failure"):
                client.post(
                    f"/api/orders/items/{ids['item']}/drawing",
                    params={"save_to_product": "true"},
                    files={"file": ("fault.png", TINY_PNG, "image/png")},
                )
        finally:
            event.remove(factory.class_, "before_commit", fail_commit)

    with factory() as db:
        assert db.get(OrderItem, ids["item"]).drawing_file is None
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
    assert list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*")) == []


@pytest.mark.parametrize("fault_kind", ["workflow", "commit"])
def test_create_order_fault_removes_promoted_drawings_and_rolls_back(
    n028_order_drawing_app,
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
) -> None:
    import app.api.orders as orders_api
    from app.models.order import Order, OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app

    def fail_workflow(*_args, **_kwargs) -> None:
        raise RuntimeError("injected create order workflow failure")

    def fail_commit(_session) -> None:
        raise RuntimeError("injected create order commit failure")

    listener_installed = False
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("drawing.png", TINY_PNG, "image/png")},
        )
        assert draft.status_code == 200, draft.text
        if fault_kind == "workflow":
            monkeypatch.setattr(
                orders_api,
                "_apply_order_reservation_plans",
                fail_workflow,
            )
            expected_message = "injected create order workflow failure"
        else:
            event.listen(factory.class_, "before_commit", fail_commit)
            listener_installed = True
            expected_message = "injected create order commit failure"
        try:
            with pytest.raises(RuntimeError, match=expected_message):
                client.post(
                    "/api/orders",
                    json=_save_to_product_order_payload(
                        ids["customer"],
                        ids["product"],
                        draft.json()["temp_path"],
                    ),
                )
        finally:
            if listener_installed:
                event.remove(factory.class_, "before_commit", fail_commit)

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
    assert list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*")) == []
    draft_files = list(
        Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"]).glob("*")
    )
    assert len(draft_files) == 1
    assert "_thumb" not in draft_files[0].name


def test_draft_upload_and_order_create_reject_untrusted_files_and_paths(
    n028_order_drawing_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product_drawing import ProductDrawing
    from app.services.product_drawings import (
        MAX_DRAWING_BYTES,
        MAX_DRAWING_IMAGE_PIXELS,
        MAX_DRAWING_IMAGE_SIDE,
    )

    app, ids, factory = n028_order_drawing_app
    Path("incoming.png").write_bytes(TINY_PNG)
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        rejected_uploads = [
            ("attack.html", b"<html>attack</html>", "text/html"),
            ("attack.svg", b"<svg><script/></svg>", "image/svg+xml"),
            ("fake.png", b"<html>not an image</html>", "image/png"),
            ("fake-gif.png", _image_bytes("GIF"), "image/png"),
            ("fake-tiff.png", _image_bytes("TIFF"), "image/png"),
            ("fake-bmp.png", _image_bytes("BMP"), "image/png"),
            (
                "oversized-dimension.png",
                _image_bytes(
                    "PNG",
                    size=(MAX_DRAWING_IMAGE_SIDE + 1, 1),
                    mode="1",
                ),
                "image/png",
            ),
            (
                "oversized-pixels.png",
                _image_bytes(
                    "PNG",
                    size=(6_000, MAX_DRAWING_IMAGE_PIXELS // 6_000 + 1),
                    mode="1",
                ),
                "image/png",
            ),
            ("header-only.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"),
            ("truncated.pdf", TINY_PDF[:-16], "application/pdf"),
            (
                "javascript.pdf",
                _pdf_bytes("javascript"),
                "application/pdf",
            ),
            (
                "indirect-javascript.pdf",
                _pdf_bytes("indirect_javascript"),
                "application/pdf",
            ),
            ("launch.pdf", _pdf_bytes("launch"), "application/pdf"),
            (
                "open-action.pdf",
                _pdf_bytes("open_action"),
                "application/pdf",
            ),
            (
                "embedded-file.pdf",
                _pdf_bytes("embedded_file"),
                "application/pdf",
            ),
            ("rich-media.pdf", _pdf_bytes("rich_media"), "application/pdf"),
            (
                "rich-media-subtype.pdf",
                _pdf_bytes("rich_media_subtype"),
                "application/pdf",
            ),
            ("submit-form.pdf", _pdf_bytes("submit_form"), "application/pdf"),
            ("import-data.pdf", _pdf_bytes("import_data"), "application/pdf"),
            ("goto-remote.pdf", _pdf_bytes("goto_remote"), "application/pdf"),
            ("goto-embedded.pdf", _pdf_bytes("goto_embedded"), "application/pdf"),
            ("rendition.pdf", _pdf_bytes("rendition"), "application/pdf"),
            ("movie-action.pdf", _pdf_bytes("movie"), "application/pdf"),
            ("sound-action.pdf", _pdf_bytes("sound"), "application/pdf"),
            ("transition.pdf", _pdf_bytes("transition"), "application/pdf"),
            ("named-action.pdf", _pdf_bytes("named"), "application/pdf"),
            (
                "set-ocg-state.pdf",
                _pdf_bytes("set_ocg_state"),
                "application/pdf",
            ),
            ("thread-action.pdf", _pdf_bytes("thread"), "application/pdf"),
            (
                "external-stream.pdf",
                _pdf_bytes("external_stream"),
                "application/pdf",
            ),
            ("filespec.pdf", _pdf_bytes("filespec"), "application/pdf"),
            ("screen-subtype.pdf", _pdf_bytes("screen_subtype"), "application/pdf"),
            ("movie-subtype.pdf", _pdf_bytes("movie_subtype"), "application/pdf"),
            ("sound-subtype.pdf", _pdf_bytes("sound_subtype"), "application/pdf"),
            ("3d-subtype.pdf", _pdf_bytes("three_d_subtype"), "application/pdf"),
            ("widget-subtype.pdf", _pdf_bytes("widget_subtype"), "application/pdf"),
            (
                "projection-subtype.pdf",
                _pdf_bytes("projection_subtype"),
                "application/pdf",
            ),
            ("large.png", b"0" * (MAX_DRAWING_BYTES + 1), "image/png"),
        ]
        for filename, content, content_type in rejected_uploads:
            response = client.post(
                "/api/orders/draft-drawing",
                files={"file": (filename, content, content_type)},
            )
            assert response.status_code == 400, (filename, response.text)

        for filename, content, content_type in rejected_uploads:
            response = client.post(
                f"/api/orders/items/{ids['item']}/drawing",
                files={"file": (filename, content, content_type)},
            )
            assert response.status_code == 400, (filename, response.text)

        rejected_references = [
            "C:\\Windows\\win.ini",
            "D:\\纸箱厂erp软件搭建\\data\\carton_erp.sqlite3",
            "/incoming.png",
            "/static/uploads/drawings/replacement.png",
            "/static/uploads/order_drafts/../replacement.webp",
            "/static/uploads/order_drafts/draft_00000000000000000000000000000000.webp",
        ]
        for reference in rejected_references:
            response = client.post(
                "/api/orders",
                json=_save_to_product_order_payload(
                    ids["customer"],
                    ids["product"],
                    reference,
                ),
            )
            assert response.status_code == 400, (reference, response.text)

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1
        assert db.get(OrderItem, ids["item"]).drawing_file is None
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
    assert list(
        Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"]).glob("*")
    ) == []
    assert list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*")) == []


@pytest.mark.parametrize(
    ("filename", "active_kind"),
    [
        pytest.param("xfa-initialize.pdf", "xfa_initialize", id="xfa-initialize"),
        pytest.param("uri-action.pdf", "uri", id="uri-action"),
    ],
)
def test_xfa_initialize_and_uri_pdfs_are_rejected_without_side_effects(
    n028_order_drawing_app,
    filename: str,
    active_kind: str,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    content = _pdf_bytes(active_kind)
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft_response = client.post(
            "/api/orders/draft-drawing",
            files={"file": (filename, content, "application/pdf")},
        )
        item_response = client.post(
            f"/api/orders/items/{ids['item']}/drawing",
            params={"save_to_product": "true"},
            files={"file": (filename, content, "application/pdf")},
        )

    assert draft_response.status_code == 400, draft_response.text
    assert item_response.status_code == 400, item_response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1
        assert db.get(OrderItem, ids["item"]).drawing_file is None
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
    assert list(
        Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"]).glob("*")
    ) == []
    assert list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*")) == []


@pytest.mark.parametrize(
    "active_kind",
    ["internal_goto", "ordinary_annotation_flags"],
)
def test_static_pdf_internal_navigation_and_annotation_flags_remain_allowed(
    n028_order_drawing_app,
    active_kind: str,
) -> None:
    app, _, _ = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        response = client.post(
            "/api/orders/draft-drawing",
            files={
                "file": (
                    f"{active_kind}.pdf",
                    _pdf_bytes(active_kind),
                    "application/pdf",
                )
            },
        )

    assert response.status_code == 200, response.text
    draft_files = list(
        Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"]).glob("*")
    )
    assert len(draft_files) == 1
    assert draft_files[0].suffix == ".pdf"


@pytest.mark.parametrize(
    "bomb_error",
    [
        Image.DecompressionBombWarning("warning bomb"),
        Image.DecompressionBombError("error bomb"),
    ],
)
def test_draft_upload_rejects_pillow_decompression_bombs_without_files(
    n028_order_drawing_app,
    monkeypatch: pytest.MonkeyPatch,
    bomb_error: Exception,
) -> None:
    import app.services.product_drawings as drawing_service

    app, _, _ = n028_order_drawing_app

    def raise_bomb(_content):
        raise bomb_error

    monkeypatch.setattr(drawing_service.Image, "open", raise_bomb)
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        response = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("bomb.png", TINY_PNG, "image/png")},
        )

    assert response.status_code == 400
    assert list(
        Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"]).glob("*")
    ) == []


def test_tampered_draft_is_read_with_limit_and_cannot_create_order(
    n028_order_drawing_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.order import Order, OrderItem
    from app.services.product_drawings import MAX_DRAWING_BYTES

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("drawing.png", TINY_PNG, "image/png")},
        )
        assert draft.status_code == 200, draft.text
        draft_path = (
            Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"])
            / Path(draft.json()["temp_path"]).name
        )
        draft_path.write_bytes(b"0" * (MAX_DRAWING_BYTES + 1))

        def forbid_read_bytes(*_args, **_kwargs):
            raise AssertionError("load_order_draft_drawing must not call Path.read_bytes")

        monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
        response = client.post(
            "/api/orders",
            json=_save_to_product_order_payload(
                ids["customer"],
                ids["product"],
                draft.json()["temp_path"],
            ),
        )

    assert response.status_code == 400
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1
    assert list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*")) == []


def test_hard_linked_external_pdf_cannot_be_used_as_order_draft(
    n028_order_drawing_app,
    tmp_path: Path,
) -> None:
    from app.models.order import Order, OrderItem

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("drawing.pdf", TINY_PDF, "application/pdf")},
        )
        assert draft.status_code == 200, draft.text
        draft_path = (
            Path(os.environ["ERP_ORDER_DRAFT_DRAWING_DIR"])
            / Path(draft.json()["temp_path"]).name
        )
        external_pdf = tmp_path / "external-valid.pdf"
        external_pdf.write_bytes(TINY_PDF)
        draft_path.unlink()
        try:
            os.link(external_pdf, draft_path)
        except OSError as error:
            pytest.skip(f"当前文件系统不支持硬链接测试：{error}")

        response = client.post(
            "/api/orders",
            json=_save_to_product_order_payload(
                ids["customer"],
                ids["product"],
                draft.json()["temp_path"],
            ),
        )

    assert response.status_code == 400
    assert "硬链接" in response.json()["detail"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1
    assert list(Path(os.environ["ERP_DRAWING_DIR"]).glob("*")) == []


def test_valid_pdf_draft_is_promoted_to_a_safe_pdf_order_drawing(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("drawing.pdf", TINY_PDF, "application/pdf")},
        )
        assert draft.status_code == 200, draft.text
        assert draft.json()["temp_path"].endswith(".pdf")
        payload = _save_to_product_order_payload(
            ids["customer"],
            ids["product"],
            draft.json()["temp_path"],
        )
        payload["items"][0]["drawing_save_option"] = "order_only"
        created = client.post("/api/orders", json=payload)

    assert created.status_code == 201, created.text
    created_item_id = created.json()["items"][0]["id"]
    with factory() as db:
        item = db.get(OrderItem, created_item_id)
        assert item is not None
        assert item.drawing_file.startswith("/static/uploads/drawings/product_")
        assert item.drawing_file.endswith(".pdf")
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
