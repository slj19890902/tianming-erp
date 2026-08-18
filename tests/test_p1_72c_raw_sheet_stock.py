from __future__ import annotations

import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_stock_replenishment_flow import _login, stock_replenishment_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    match = re.search(
        rf"(?m)^\s{{10}}(?:async\s+)?{re.escape(name)}\([^\n]*\)\s*\{{",
        INDEX,
    )
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"(?m)^\s{10}(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$]*\([^\n]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    source = INDEX[match.end() : match.end() + next_method.start()]
    return source.rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-72C frontend regressions"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed_raw_products(session_factory) -> dict[str, int]:
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product

    with session_factory() as db:
        customer = db.scalar(select(Customer).where(Customer.customer_code == "TH"))
        material = db.scalar(select(Material).where(Material.code == "A416D"))
        assert customer is not None and material is not None
        other = Customer(
            customer_number=2,
            customer_code="OTHER",
            name="其他客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        db.add(other)
        db.flush()

        def product(code: str, customer_id: int, pieces_per_box: int) -> Product:
            return Product(
                customer_id=customer_id,
                product_code=code,
                customer_material_code=code,
                product_name=f"{code}通用片料产品",
                material_id=material.id,
                legacy_material_text="A416D/AB",
                length_mm=Decimal("500"),
                width_mm=Decimal("300"),
                height_mm=Decimal("200"),
                box_category="normal",
                flute_type="AB",
                layer_count=5,
                report_length_mm=900,
                report_width_mm=600,
                crease_type="毛片",
                pieces_per_box=pieces_per_box,
                default_cutting_mode="一开一",
            )

        dedicated_a = product("RAW-A", customer.id, 1)
        dedicated_b = product("RAW-B", customer.id, 2)
        other_product = product("RAW-C", other.id, 2)
        rotated = product("RAW-ROTATE", other.id, 1)
        rotated.report_length_mm = 600
        rotated.report_width_mm = 900
        mismatched = product("RAW-MISMATCH", other.id, 1)
        mismatched.report_width_mm = 601
        db.add_all([dedicated_a, dedicated_b, other_product, rotated, mismatched])
        db.commit()
        return {
            "customer_id": customer.id,
            "other_customer_id": other.id,
            "material_id": material.id,
            "dedicated_a": dedicated_a.id,
            "dedicated_b": dedicated_b.id,
            "other_product": other_product.id,
            "rotated": rotated.id,
            "mismatched": mismatched.id,
        }


def _raw_sheet_payload(
    ids: dict[str, int],
    *,
    scope: str,
    idempotency_key: str,
    sheet_type: str = "raw_board",
) -> dict:
    customer_id = ids["customer_id"] if scope == "customer" else None
    return {
        "source_type": "customer_request",
        "idempotency_key": idempotency_key,
        "supplier_name": "苏州佳丰",
        "customer_id": customer_id,
        "stock_now": False,
        "items": [
            {
                "target_inventory_type": "semi_finished",
                "customer_id": customer_id,
                "product_id": None,
                "product_name": "客户专用片料" if customer_id else "通用片料",
                "raw_sheet_scope": scope,
                "material_id": ids["material_id"],
                "material_code": "A416D",
                "layer_count": 5,
                "flute_type": "AB",
                "report_length_mm": 900,
                "report_width_mm": 600,
                "crease_type": None,
                "sheet_type": sheet_type,
                "component_type": "whole",
                "pieces_per_box": 1,
                "stock_yield_per_sheet": 1,
                "quantity": 20,
                "location_id": None,
            }
        ],
    }


def _receive(client: TestClient, item_id: int, key: str) -> int:
    response = client.put(
        f"/api/incoming/receive/sr{item_id}",
        json={"received_quantity": 20, "idempotency_key": key},
    )
    assert response.status_code == 200, response.text
    lot_id = response.json()["received_inventory_lot_id"]
    assert isinstance(lot_id, int)
    return lot_id


def test_customer_raw_sheet_receipt_binds_all_compatible_products_only(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    ids = _seed_raw_products(session_factory)
    with TestClient(app) as client:
        _login(client)
        before = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_raw_sheet_payload(
                ids, scope="customer", idempotency_key="p1-72c-dedicated"
            ),
        )
        assert before.status_code == 201, before.text
        order = before.json()
        assert order["items"][0]["raw_sheet_scope"] == "customer"
        lot_id = _receive(client, order["items"][0]["id"], "p1-72c-dedicated-in")

    from app.models.warehouse_inventory import (
        InventoryLot,
        SemiFinishedLotAllowedProduct,
    )

    with session_factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None and lot.semi_finished_detail is not None
        assert lot.semi_finished_detail.owner_customer_id == ids["customer_id"]
        assert lot.semi_finished_detail.sheet_type == "raw_board"
        assert lot.semi_finished_detail.cutting_note.startswith(
            "stock_replenishment:raw_sheet_scope:customer"
        )
        allowed = set(
            db.scalars(
                select(SemiFinishedLotAllowedProduct.product_id).where(
                    SemiFinishedLotAllowedProduct.inventory_lot_id == lot_id
                )
            ).all()
        )
        assert allowed == {ids["dedicated_a"], ids["dedicated_b"]}
        from app.services.semi_finished_inventory import (
            semi_finished_candidates_for_product,
        )

        sibling_candidates = semi_finished_candidates_for_product(
            db,
            product_id=ids["dedicated_b"],
            customer_id=ids["customer_id"],
            board_length_mm=900,
            board_width_mm=600,
            material_code="A416D",
            flute_type="AB",
            component_type="whole",
            pieces_per_box=2,
            stock_yield_per_sheet=1,
        )
        assert [(row.lot.id, row.source) for row in sibling_candidates] == [
            (lot_id, "signature")
        ]
        foreign_candidates = semi_finished_candidates_for_product(
            db,
            product_id=ids["other_product"],
            customer_id=ids["other_customer_id"],
            board_length_mm=900,
            board_width_mm=600,
            material_code="A416D",
            flute_type="AB",
            component_type="whole",
            pieces_per_box=2,
            stock_yield_per_sheet=1,
        )
        assert foreign_candidates == []


def test_general_raw_sheet_cross_customer_is_exact_directional_and_confirmed(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    ids = _seed_raw_products(session_factory)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_raw_sheet_payload(
                ids, scope="general", idempotency_key="p1-72c-general"
            ),
        )
        assert created.status_code == 201, created.text
        assert created.json()["items"][0]["raw_sheet_scope"] == "general"
        replay = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_raw_sheet_payload(
                ids, scope="general", idempotency_key="p1-72c-general"
            ),
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == created.json()["id"]
        lot_id = _receive(
            client, created.json()["items"][0]["id"], "p1-72c-general-in"
        )

    from app.models.warehouse_inventory import InventoryLot
    from app.services.semi_finished_inventory import (
        GENERAL_SEMI_FINISHED_STOCK,
        semi_finished_candidates_for_product,
    )

    with session_factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None and lot.semi_finished_detail is not None
        assert lot.semi_finished_detail.owner_customer_id is None
        assert lot.semi_finished_detail.cutting_note.startswith(
            "stock_replenishment:raw_sheet_scope:general"
        )

        candidates = semi_finished_candidates_for_product(
            db,
            product_id=ids["other_product"],
            customer_id=ids["other_customer_id"],
            board_length_mm=900,
            board_width_mm=600,
            material_code="A416D",
            flute_type="AB",
            component_type="whole",
            pieces_per_box=2,
            stock_yield_per_sheet=1,
        )
        assert [(row.lot.id, row.source) for row in candidates] == [
            (lot_id, "general_signature")
        ]
        assert GENERAL_SEMI_FINISHED_STOCK in candidates[0].warning_codes

        rotated = semi_finished_candidates_for_product(
            db,
            product_id=ids["rotated"],
            customer_id=ids["other_customer_id"],
            board_length_mm=600,
            board_width_mm=900,
            material_code="A416D",
            flute_type="AB",
            component_type="whole",
            pieces_per_box=1,
            stock_yield_per_sheet=1,
        )
        mismatched = semi_finished_candidates_for_product(
            db,
            product_id=ids["mismatched"],
            customer_id=ids["other_customer_id"],
            board_length_mm=900,
            board_width_mm=601,
            material_code="A416D",
            flute_type="AB",
            component_type="whole",
            pieces_per_box=1,
            stock_yield_per_sheet=1,
        )
        assert rotated == []
        assert mismatched == []

def test_general_stock_order_rejects_processed_sheet_type(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    ids = _seed_raw_products(session_factory)
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_raw_sheet_payload(
                ids,
                scope="general",
                idempotency_key="p1-72c-general-net",
                sheet_type="net_sheet",
            ),
        )
    assert response.status_code == 409, response.text
    assert "通用片料" in response.json()["detail"]
    from app.models.stock_replenishment import StockReplenishmentOrder

    with session_factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 0


def test_legacy_processed_general_lot_is_not_offered_as_cross_customer_stock(
    stock_replenishment_app,
) -> None:
    """Old or manually corrupted processed sheets must never gain broad scope."""

    app, session_factory = stock_replenishment_app
    ids = _seed_raw_products(session_factory)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_raw_sheet_payload(
                ids, scope="general", idempotency_key="p1-72c-legacy-processed"
            ),
        )
        assert created.status_code == 201, created.text
        lot_id = _receive(
            client,
            created.json()["items"][0]["id"],
            "p1-72c-legacy-processed-in",
        )

    from app.models.warehouse_inventory import InventoryLot
    from app.services.semi_finished_inventory import (
        semi_finished_candidates_for_product,
    )

    with session_factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None and lot.semi_finished_detail is not None
        lot.semi_finished_detail.sheet_type = "net_sheet"
        db.commit()

    with session_factory() as db:
        assert semi_finished_candidates_for_product(
            db,
            product_id=ids["other_product"],
            customer_id=ids["other_customer_id"],
            board_length_mm=900,
            board_width_mm=600,
            material_code="A416D",
            flute_type="AB",
            component_type="whole",
            pieces_per_box=2,
            stock_yield_per_sheet=1,
        ) == []


def test_dedicated_raw_sheet_without_compatible_product_is_rejected_without_write(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    ids = _seed_raw_products(session_factory)
    payload = _raw_sheet_payload(
        ids, scope="customer", idempotency_key="p1-72c-no-compatible"
    )
    payload["items"][0]["report_width_mm"] = 777
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
    assert response.status_code == 409, response.text
    assert "没有任何" in response.json()["detail"]
    from app.models.stock_replenishment import StockReplenishmentOrder

    with session_factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 0


def test_selected_customer_user_cannot_create_general_raw_sheet(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    ids = _seed_raw_products(session_factory)
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.user import User

    with session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        restricted = User(
            username="p1-72c-selected",
            password_hash=hash_password("SelectedPass123!"),
            role="sales",
            real_name="selected",
            display_name="selected",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add(restricted)
        db.flush()
        db.add(UserCustomerScope(user_id=restricted.id, customer_id=ids["customer_id"]))
        for code in ("requisition.view", "requisition.execute"):
            db.add(
                UserPermissionOverride(
                    user_id=restricted.id,
                    permission_code=code,
                    is_allowed=True,
                    granted_by=admin.id,
                )
            )
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "p1-72c-selected", "password": "SelectedPass123!"},
        )
        assert login.status_code == 200, login.text
        denied = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_raw_sheet_payload(
                ids, scope="general", idempotency_key="p1-72c-selected-general"
            ),
        )
        assert denied.status_code == 403, denied.text
        dedicated = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_raw_sheet_payload(
                ids, scope="customer", idempotency_key="p1-72c-selected-dedicated"
            ),
        )
        assert dedicated.status_code == 201, dedicated.text

    with session_factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 1


def test_stock_order_ui_exposes_raw_sheet_scope_without_new_sales_order_route() -> None:
    assert '@click="addRawStockReplenishmentLine"' in INDEX
    assert "指定客户片料" in INDEX
    assert "通用片料" in INDEX
    assert "每张可抵片数" in INDEX
    assert ':disabled="!user?.unrestricted_customer_access"' in INDEX
    save = _method_body("saveStockReplenishmentDraft")
    assert 'axios.post("/api/requisition/stock-replenishment/orders"' in save
    assert "item.raw_sheet_scope=item._raw_sheet_scope" in save
    assert 'axios.post("/api/orders"' not in save


def test_raw_sheet_frontend_scope_and_validation_are_fail_closed(tmp_path: Path) -> None:
    add = _method_body("addRawStockReplenishmentLine")
    validate = _method_body("validateStockReplenishmentForm")
    script = f"""
const FunctionCtor=Function;
global.createIdempotencyKey=()=>"raw-key";
const vm={{user:{{unrestricted_customer_access:true}},stockReplenishmentForm:{{source_type:"customer_request",items:[]}},stockReplenishmentMaterialSuppliers(){{return ["苏州佳丰"];}},syncStockReplenishmentCustomer(){{}}}};
vm.addRawStockReplenishmentLine=new FunctionCtor({json.dumps(add, ensure_ascii=False)}).bind(vm);
vm.validateStockReplenishmentForm=new FunctionCtor({json.dumps(validate, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
vm.addRawStockReplenishmentLine();
const line=vm.stockReplenishmentForm.items[0];
expect(line._entry_mode==="raw_sheet","raw sheet mode missing");
expect(line._raw_sheet_scope==="customer","dedicated scope should be safe default");
Object.assign(line,{{material_id:1,material_code:"A416D",material_supplier_name:"苏州佳丰",layer_count:5,flute_type:"AB",report_length_mm:900,report_width_mm:600,quantity:20,stock_yield_per_sheet:1}});
expect(vm.validateStockReplenishmentForm().includes("客户"),"dedicated raw sheet allowed without customer");
line.customer_id=7;
expect(vm.validateStockReplenishmentForm()==="","valid dedicated raw sheet was rejected");
line._raw_sheet_scope="general";line.customer_id=null;line.product_id=null;
expect(vm.validateStockReplenishmentForm()==="","valid general raw sheet was rejected");
line.sheet_type="net_sheet";
expect(vm.validateStockReplenishmentForm().includes("通用片料"),"processed general sheet was accepted");
"""
    _run_node(script, tmp_path, "p1-72c-raw-sheet-ui.js")
