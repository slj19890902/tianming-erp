from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _range_text(start: int, end: int) -> str:
    return f"箱编号为#{start}到#{end}"


def _layout(
    *,
    index: int,
    po: str,
    customer_name: str,
    style: str,
    color: str,
    product_size: str,
    units_per_carton: int,
    carton_qty: int,
    carton_no_start: int,
    carton_no_end: int,
    common_box_note: str = "",
) -> dict:
    return {
        "layout_index": index,
        "source_sheet_name": "Sheet1",
        "external_po_no": po,
        "to_customer_name": customer_name,
        "vendor_style_no": style,
        "color": color,
        "product_size": product_size,
        "units_per_carton": units_per_carton,
        "layout_total_units": units_per_carton * carton_qty,
        "layout_carton_qty": carton_qty,
        "carton_no_start": carton_no_start,
        "carton_no_end": carton_no_end,
        "carton_no_range_text": _range_text(carton_no_start, carton_no_end),
        "summary_text": f"{units_per_carton * carton_qty}条={carton_qty}箱",
        "common_box_note": common_box_note,
        "validations": {
            "qty_validation": "ok",
            "units_validation": "ok",
        },
    }


def _workbook(po: str, total_cartons: int, layouts: list[dict]) -> list[dict]:
    return [
        {
            "file_name": f"{po}.xls",
            "sheets": [
                {
                    "sheet_name": "Sheet1",
                    "order_total_carton_qty": total_cartons,
                    "layout_count": len(layouts),
                    "layouts": layouts,
                }
            ],
        }
    ]


def _sample_30400873() -> list[dict]:
    customer_name = "Winners Merchant Int'l LP"
    return _workbook(
        "30 400873",
        200,
        [
            _layout(
                index=1,
                po="30 400873",
                customer_name=customer_name,
                style="24300664FQ",
                color="Gray C2",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=60,
                carton_no_start=1,
                carton_no_end=60,
            ),
            _layout(
                index=2,
                po="30 400873",
                customer_name=customer_name,
                style="24300781FQ",
                color="Gray C4",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=40,
                carton_no_start=61,
                carton_no_end=100,
            ),
            _layout(
                index=3,
                po="30 400873",
                customer_name=customer_name,
                style="24300693FQ",
                color="Gray Multi C2",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=40,
                carton_no_start=101,
                carton_no_end=140,
            ),
            _layout(
                index=4,
                po="30 400873",
                customer_name=customer_name,
                style="24300766FQ",
                color="Taupe Multi C1",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=60,
                carton_no_start=141,
                carton_no_end=200,
            ),
        ],
    )


def _sample_30400851() -> list[dict]:
    customer_name = "Winners Merchant Int'l LP"
    return _workbook(
        "30 400851",
        550,
        [
            _layout(
                index=1,
                po="30 400851",
                customer_name=customer_name,
                style="24300664FQ",
                color="Blue Multi C2",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=150,
                carton_no_start=1,
                carton_no_end=150,
            ),
            _layout(
                index=2,
                po="30 400851",
                customer_name=customer_name,
                style="24300694FQ",
                color="Blue Multi C2",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=50,
                carton_no_start=151,
                carton_no_end=200,
                common_box_note="45*34*32cm",
            ),
            _layout(
                index=3,
                po="30 400851",
                customer_name=customer_name,
                style="24300693FQ",
                color="Blue Multi C2",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=100,
                carton_no_start=201,
                carton_no_end=300,
            ),
            _layout(
                index=4,
                po="30 400851",
                customer_name=customer_name,
                style="24300781FQ",
                color="Blue Multi C2",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=100,
                carton_no_start=301,
                carton_no_end=400,
            ),
            _layout(
                index=5,
                po="30 400851",
                customer_name=customer_name,
                style="24300766FQ",
                color="Blue Multi C2",
                product_size='90"x90"',
                units_per_carton=2,
                carton_qty=150,
                carton_no_start=401,
                carton_no_end=550,
            ),
        ],
    )


def _sample_35043393() -> list[dict]:
    customer_name = "Winners Merchant Int'l LP"
    return _workbook(
        "35 043393",
        200,
        [
            _layout(
                index=1,
                po="35 043393",
                customer_name=customer_name,
                style="24300754TW",
                color="Gray C2",
                product_size='68"x90"',
                units_per_carton=4,
                carton_qty=75,
                carton_no_start=1,
                carton_no_end=75,
                common_box_note="66*45*25cm",
            ),
            _layout(
                index=2,
                po="35 043393",
                customer_name=customer_name,
                style="24300754FQ",
                color="Gray C2",
                product_size='90"x90"',
                units_per_carton=4,
                carton_qty=125,
                carton_no_start=76,
                carton_no_end=200,
                common_box_note="66*45*30cm",
            ),
        ],
    )


def _seed_master_data(db, customer_id: int) -> None:
    from app.models.product import Product
    from app.models.xinzhen_carton_marking import (
        XinzhenCommonBoxRule,
        XinzhenRubberTypeBlock,
    )

    products = [
        Product(
            customer_id=customer_id,
            product_code="BOX-9090-2",
            customer_material_code="BOX-9090-2",
            product_name='90"x90" 2up',
            box_category="normal",
            pieces_per_box=2,
            is_active=True,
        ),
        Product(
            customer_id=customer_id,
            product_code="BOX-9090-4",
            customer_material_code="BOX-9090-4",
            product_name='90"x90" 4up',
            box_category="normal",
            pieces_per_box=4,
            is_active=True,
        ),
        Product(
            customer_id=customer_id,
            product_code="BOX-6890-4",
            customer_material_code="BOX-6890-4",
            product_name='68"x90" 4up',
            box_category="normal",
            pieces_per_box=4,
            is_active=True,
        ),
    ]
    db.add_all(products)
    db.flush()
    db.add_all(
        [
            XinzhenCommonBoxRule(
                customer_id=customer_id,
                product_id=products[0].id,
                product_size='90"x90"',
                units_per_carton=2,
                is_active=True,
            ),
            XinzhenCommonBoxRule(
                customer_id=customer_id,
                product_id=products[0].id,
                dimension_hint_text="45*34*32cm",
                product_size='90"x90"',
                units_per_carton=2,
                vendor_style_no="24300694FQ",
                color="Blue Multi C2",
                is_active=True,
            ),
            XinzhenCommonBoxRule(
                customer_id=customer_id,
                product_id=products[1].id,
                product_size='90"x90"',
                units_per_carton=4,
                dimension_hint_text="66*45*30cm",
                is_active=True,
            ),
            XinzhenCommonBoxRule(
                customer_id=customer_id,
                product_id=products[2].id,
                product_size='68"x90"',
                units_per_carton=4,
                dimension_hint_text="66*45*25cm",
                is_active=True,
            ),
        ]
    )

    block_counter = 1

    def add_block(block_type: str, block_text: str) -> None:
        nonlocal block_counter
        db.add(
            XinzhenRubberTypeBlock(
                customer_id=customer_id,
                block_code=f"RB{block_counter:04d}",
                block_text=block_text,
                block_type=block_type,
                storage_location_text=f"LOC-{block_counter:03d}",
                is_active=True,
            )
        )
        block_counter += 1

    for block_type, values in (
        ("CUSTOMER_NAME", {"Winners Merchant Int'l LP"}),
        (
            "STYLE",
            {
                "24300664FQ",
                "24300781FQ",
                "24300693FQ",
                "24300766FQ",
                "24300694FQ",
                "24300754TW",
                "24300754FQ",
            },
        ),
        (
            "COLOR",
            {
                "Gray C2",
                "Gray C4",
                "Gray Multi C2",
                "Taupe Multi C1",
                "Blue Multi C2",
            },
        ),
        ("SIZE", {'90"x90"', '68"x90"'}),
        ("UNITS", {"2", "4"}),
        (
            "CARTON_NO",
            {
                _range_text(1, 60),
                _range_text(61, 100),
                _range_text(101, 140),
                _range_text(141, 200),
                _range_text(1, 150),
                _range_text(151, 200),
                _range_text(201, 300),
                _range_text(301, 400),
                _range_text(401, 550),
                _range_text(1, 75),
                _range_text(76, 200),
            },
        ),
        ("CARTON_QTY", {"40", "50", "60", "75", "100", "125", "150"}),
        ("PO", {"30 400873", "30 400851", "35 043393"}),
    ):
        for value in sorted(values):
            add_block(block_type, value)


@pytest.fixture
def xinzhen_api_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.xinzhen_carton_marking import router as xinzhen_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "xinzhen.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="sales",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="Sales",
            display_name="Sales",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="XINZHEN",
            name="新振",
            credit_limit=Decimal("0"),
        )
        db.add_all([user, customer])
        db.flush()
        _seed_master_data(db, customer.id)
        db.commit()
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(xinzhen_router, prefix="/api/xinzhen-carton-marking")

    def override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    return app, factory, 1


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "sales", "password": "RolePass123!"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("builder", "expected_layouts", "expected_header", "expected_total"),
    [
        (_sample_30400873, 4, 200, 200),
        (_sample_30400851, 5, 550, 550),
        (_sample_35043393, 2, 200, 200),
    ],
)
def test_import_apply_builds_backend_chain_for_sample_orders(
    xinzhen_api_app,
    builder,
    expected_layouts,
    expected_header,
    expected_total,
) -> None:
    app, _factory, customer_id = xinzhen_api_app
    payload = {
        "customer_id": customer_id,
        "source_file_name": "sample.xls",
        "source_file_hash": "hash-sample",
        "parsed_json": builder(),
        "mode": "apply",
    }
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/xinzhen-carton-marking/import-json", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["parsed_layout_count"] == expected_layouts
        assert data["header_total_carton_qty"] == expected_header
        assert data["layout_total_carton_qty"] == expected_total
        assert data["import_status"] == "ready"
        assert data["order"]["status"] == "pending_receipt"
        assert len(data["print_layouts"]) == expected_layouts
        assert data["generated_changeover_step_count"] == max(expected_layouts - 1, 0)
        assert data["missing_common_box_count"] == 0
        assert data["missing_rubber_block_count"] == 0
        detail = client.get(
            f"/api/xinzhen-carton-marking/import-batches/{data['import_batch_id']}"
        )
        assert detail.status_code == 200
        assert detail.json()["order"]["external_po_no"] == data["order"]["external_po_no"]
        mobile = client.get(
            f"/api/xinzhen-carton-marking/import-batches/{data['import_batch_id']}/mobile-view"
        )
        assert mobile.status_code == 200
        assert mobile.json()["order_overview"]["layout_count"] == expected_layouts
        assert len(mobile.json()["changeover_views"]) == max(expected_layouts - 1, 0)


def test_import_dry_run_returns_summary_without_persisting(xinzhen_api_app) -> None:
    app, factory, customer_id = xinzhen_api_app
    payload = {
        "customer_id": customer_id,
        "source_file_name": "dry-run.xls",
        "source_file_hash": "dry-run-hash",
        "parsed_json": _sample_30400873(),
        "mode": "dry_run",
    }
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/xinzhen-carton-marking/import-json", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["import_batch_id"] is None
        assert data["order_id"] is None
        assert data["import_status"] == "ready"
    from app.models.xinzhen_carton_marking import (
        XinzhenCartonMarkingImportBatch,
        XinzhenCartonMarkingOrder,
        XinzhenPrintLayout,
    )

    with factory() as db:
        assert db.scalar(
            select(func.count()).select_from(XinzhenCartonMarkingImportBatch)
        ) == 0
        assert db.scalar(select(func.count()).select_from(XinzhenCartonMarkingOrder)) == 0
        assert db.scalar(select(func.count()).select_from(XinzhenPrintLayout)) == 0


def test_header_total_mismatch_marks_needs_review(xinzhen_api_app) -> None:
    app, _factory, customer_id = xinzhen_api_app
    workbook = _sample_30400873()
    workbook[0]["sheets"][0]["order_total_carton_qty"] = 999
    payload = {
        "customer_id": customer_id,
        "source_file_name": "mismatch.xls",
        "source_file_hash": "mismatch-hash",
        "parsed_json": workbook,
        "mode": "apply",
    }
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/xinzhen-carton-marking/import-json", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["import_status"] == "needs_review"
        assert data["mismatch_count"] == 1
        assert data["order"]["status"] == "needs_review"


def test_missing_rubber_block_marks_slot_missing_and_needs_review(
    xinzhen_api_app,
) -> None:
    app, factory, customer_id = xinzhen_api_app
    from app.models.xinzhen_carton_marking import XinzhenRubberTypeBlock

    with factory() as db:
        db.execute(
            delete(XinzhenRubberTypeBlock).where(
                XinzhenRubberTypeBlock.customer_id == customer_id,
                XinzhenRubberTypeBlock.block_type == "STYLE",
                XinzhenRubberTypeBlock.block_text == "24300664FQ",
            )
        )
        db.commit()
    payload = {
        "customer_id": customer_id,
        "source_file_name": "missing-rubber.xls",
        "source_file_hash": "missing-rubber-hash",
        "parsed_json": _sample_30400873(),
        "mode": "apply",
    }
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/xinzhen-carton-marking/import-json", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["import_status"] == "needs_review"
        assert data["missing_rubber_block_count"] > 0
        assert any(
            row["match_status"] == "missing"
            for row in data["print_layout_slot_values"]
        )


def test_missing_common_box_marks_layout_missing_and_needs_review(
    xinzhen_api_app,
) -> None:
    app, factory, customer_id = xinzhen_api_app
    from app.models.xinzhen_carton_marking import XinzhenCommonBoxRule

    with factory() as db:
        db.execute(
            delete(XinzhenCommonBoxRule).where(
                XinzhenCommonBoxRule.customer_id == customer_id,
                XinzhenCommonBoxRule.product_size == '90"x90"',
                XinzhenCommonBoxRule.units_per_carton == 2,
            )
        )
        db.commit()
    payload = {
        "customer_id": customer_id,
        "source_file_name": "missing-box.xls",
        "source_file_hash": "missing-box-hash",
        "parsed_json": _sample_30400873(),
        "mode": "apply",
    }
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/xinzhen-carton-marking/import-json", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["import_status"] == "needs_review"
        assert data["missing_common_box_count"] > 0
        assert any(
            layout["box_match_status"] == "missing" for layout in data["print_layouts"]
        )
