from __future__ import annotations

from collections.abc import Generator
import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P1-51B frontend regression"
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


def test_dynamic_printing_plate_rows_expose_formal_read_only_fields_and_controls() -> None:
    assert 'class="product-printing-plate-row"' in INDEX
    assert "productPrintingPlateCount(productForm)" in INDEX
    assert "addProductPrintingPlateRow" in INDEX
    assert "removeProductPrintingPlateRow(index)" in INDEX
    assert "添加挂板" in INDEX
    assert "输入挂板编号、内容或颜色" in INDEX
    assert "?.color_name" in INDEX
    assert "?.plate_name" in INDEX
    assert "?.plate_code" in INDEX
    assert "readonly" in INDEX


def test_plate_row_methods_runtime_cover_entry_hydration_add_remove_and_mapping(
    tmp_path: Path,
) -> None:
    signatures = [
        ("productPrintingPlateCount(form) {", "productPrintingPlateContent("),
        ("productPrintingPlateContent(count) {", "hydrateProductPrintingPlateRows("),
        ("hydrateProductPrintingPlateRows(form) {", "productPrintingPlateRow("),
        ("addProductPrintingPlateRow() {", "removeProductPrintingPlateRow("),
        ("removeProductPrintingPlateRow(index) {", "onProductPrintingSituationChanged("),
        ("onProductPrintingSituationChanged() {", "onProductPrintingChanged("),
    ]
    bodies = {signature.split("(", 1)[0]: _method_body(signature, following) for signature, following in signatures}
    script = f"""
const FunctionCtor=Function;
const vm={{productForm:{{}}}};
const methods={json.dumps(bodies, ensure_ascii=False)};
vm.productPrintingPlateCount=new FunctionCtor("form",methods.productPrintingPlateCount).bind(vm);
vm.productPrintingPlateContent=new FunctionCtor("count",methods.productPrintingPlateContent).bind(vm);
vm.hydrateProductPrintingPlateRows=new FunctionCtor("form",methods.hydrateProductPrintingPlateRows).bind(vm);
vm.addProductPrintingPlateRow=new FunctionCtor(methods.addProductPrintingPlateRow).bind(vm);
vm.removeProductPrintingPlateRow=new FunctionCtor("index",methods.removeProductPrintingPlateRow).bind(vm);
vm.productHasPrinting=form=>!!form&&!['','无印刷'].includes(String(form.print_content||''));
vm.productDirectPrintingColorCount=()=>0;
vm.onProductPrintingSituationChanged=new FunctionCtor(methods.onProductPrintingSituationChanged).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};

vm.productForm={{
  _printing_situation:'挂板印刷',printing_plate_mode:'no_plate',print_content:'三色印刷',
  printing_colors:'黑色＋红色＋蓝色',_printing_colors:['黑色','红色','蓝色'],
  printing_plate_1_id:31,printing_plate_2_id:32,printing_plate_3_id:33,
  plate_alignment_value_mm:1.2,plate_mount_value_mm:2.3
}};
vm.onProductPrintingSituationChanged();
expect(vm.productForm.printing_plate_mode==='plate','entering plate mode failed');
expect(vm.productForm._printing_plate_count===1,'non-plate entry inherited direct-print row count');
expect(vm.productForm.print_content==='单色印刷','one row was not mapped to single color');
expect(vm.productForm.printing_colors===null&&vm.productForm._printing_colors.length===0,'old direct colors were not cleared');
expect([1,2,3].every(i=>vm.productForm[`printing_plate_${{i}}_id`]===null),'stale bindings survived entry');

vm.addProductPrintingPlateRow();vm.addProductPrintingPlateRow();vm.addProductPrintingPlateRow();
expect(vm.productForm._printing_plate_count===3,'rows exceeded or failed to reach three');
expect(vm.productForm.print_content==='三色印刷','three rows were not mapped to three colors');
vm.productForm.printing_plate_1_id=101;vm.productForm.printing_plate_2_id=202;vm.productForm.printing_plate_3_id=303;
vm.removeProductPrintingPlateRow(2);
expect(vm.productForm._printing_plate_count===2,'row removal did not decrement count');
expect(vm.productForm.printing_plate_1_id===101&&vm.productForm.printing_plate_2_id===303&&vm.productForm.printing_plate_3_id===null,'row removal did not preserve ordered bindings');
expect(vm.productForm.print_content==='双色印刷','two rows were not mapped to two colors');
vm.removeProductPrintingPlateRow(1);vm.removeProductPrintingPlateRow(1);
expect(vm.productForm._printing_plate_count===1,'last plate row was removed');

const existing={{printing_plate_mode:'plate',print_content:'三色印刷',printing_plate_1_id:7,printing_plate_2_id:8,printing_plate_3_id:null}};
vm.hydrateProductPrintingPlateRows(existing);
expect(existing._printing_plate_count===2,'existing bindings did not hydrate exact row count');
"""
    _run_node(script, tmp_path, "p1-51b-plate-row-methods.js")


def test_plate_selection_projection_validation_and_raw_history_guard(tmp_path: Path) -> None:
    bodies = {
        "count": _method_body("productPrintingPlateCount(form) {", "productPrintingPlateContent("),
        "row": _method_body("productPrintingPlateRow(form,index) {", "addProductPrintingPlateRow("),
        "original": _method_body("productPrintingOriginalSnapshot(form) {", "productPrintingConfigurationChanged("),
        "changed": _method_body("productPrintingConfigurationChanged(form) {", "productPrintingWriteFields("),
        "write": _method_body("productPrintingWriteFields(form) {", "productDirectPrintingColorCount("),
    }
    error_body = INDEX.split("productPrintingPlateError() {", 1)[1].split("productPrintingColorError() {", 1)[0].rsplit("}", 1)[0]
    script = f"""
const FunctionCtor=Function;const bodies={json.dumps(bodies, ensure_ascii=False)};
const vm={{
  modal:{{type:'product'}},printingPlates:[
    {{id:11,plate_code:'GB-011',color_name:'专红',plate_name:'正唛'}},
    {{id:22,plate_code:'GB-022',color_name:'专蓝',plate_name:'侧唛'}}
  ]
}};
vm.productHasPrinting=form=>!!form&&form.print_content!=='无印刷';
vm.productPrintingPlateCount=new FunctionCtor('form',bodies.count).bind(vm);
vm.productPrintingPlateRow=new FunctionCtor('form','index',bodies.row).bind(vm);
vm.productPrintingOriginalSnapshot=new FunctionCtor('form',bodies.original).bind(vm);
vm.productPrintingConfigurationChanged=new FunctionCtor('form',bodies.changed).bind(vm);
vm.productPrintingColorSummary=()=>'';
vm.productPrintingWriteFields=new FunctionCtor('form',bodies.write).bind(vm);
Object.defineProperty(vm,'productPrintingPlateError',{{get:new FunctionCtor({json.dumps(error_body, ensure_ascii=False)}).bind(vm)}});
const expect=(value,message)=>{{if(!value)throw new Error(message)}};

vm.productForm={{printing_plate_mode:'plate',print_content:'双色印刷',_printing_plate_count:2,printing_plate_1_id:11,printing_plate_2_id:11,printing_plate_3_id:null}};
expect(vm.productPrintingPlateError.includes('不能重复'),'duplicate formal plate was accepted');
vm.productForm.printing_plate_2_id=22;
expect(vm.productPrintingPlateError==='','valid ordered formal plates were rejected');
const selected=vm.productPrintingPlateRow(vm.productForm,2);
expect(selected.color_name==='专蓝'&&selected.plate_name==='侧唛'&&selected.plate_code==='GB-022','formal plate fields were not projected read-only');
vm.productForm._printing_plate_count=0;
expect(vm.productPrintingPlateError.includes('至少保留一行'),'zero rows did not block save');

const raw='黑色,红色／蓝色|绿色';
const legacy={{
  id:9,_printing_situation:'三色印刷',_printing_colors:['黑色','红色','蓝色','绿色'],
  printing_plate_mode:'no_plate',print_content:'多色印刷',printing_colors:raw,
  printing_plate_1_id:null,printing_plate_2_id:null,printing_plate_3_id:null,
  _printing_original:{{_printing_situation:'三色印刷',_printing_colors:['黑色','红色','蓝色','绿色'],printing_plate_mode:'no_plate',print_content:'多色印刷',printing_colors:raw,printing_plate_1_id:null,printing_plate_2_id:null,printing_plate_3_id:null}}
}};
const untouched=vm.productPrintingWriteFields(legacy);
expect(untouched.printing_colors===raw&&untouched.print_content==='多色印刷','P1-51A raw sibling-edit protection regressed');
"""
    _run_node(script, tmp_path, "p1-51b-plate-validation-history.js")


@pytest.fixture(scope="module")
def plate_rows_app(tmp_path_factory: pytest.TempPathFactory) -> FastAPI:
    from app.api.deps import get_current_user, get_db
    from app.api.master_data_versions import router as version_router
    from app.api.products import router as products_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.printing_plate import PrintingPlate
    from app.models.user import User

    os.environ["ERP_SECRET_KEY"] = "p1-51b-printing-plate-rows-tests-only"
    engine = create_sqlite_engine(
        tmp_path_factory.mktemp("p1-51b") / "p1-51b-printing-plate-rows.sqlite3"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        customers = [
            Customer(customer_number=5151, customer_code="P151B-A", name="P1-51B 客户甲"),
            Customer(customer_number=5152, customer_code="P151B-B", name="P1-51B 客户乙"),
        ]
        admin = User(
            username="p151b-admin", password_hash="unused", role="admin",
            real_name="P1-51B Admin", is_active=True, must_change_password=False,
            customer_access_mode="all",
        )
        scoped = User(
            username="p151b-scoped", password_hash="unused", role="sales",
            real_name="P1-51B Scoped", is_active=True, must_change_password=False,
            customer_access_mode="selected",
        )
        workshop = User(
            username="p151b-workshop", password_hash="unused", role="workshop",
            real_name="P1-51B Workshop", is_active=True, must_change_password=False,
            customer_access_mode="all",
        )
        db.add_all([*customers, admin, scoped, workshop])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=scoped.id, customer_id=customers[0].id, assigned_by=admin.id),
                UserPermissionOverride(
                    user_id=scoped.id, permission_code="warehouse.view", is_allowed=True,
                    granted_by=admin.id,
                ),
            ]
        )
        plates = [
            PrintingPlate(
                plate_code="P151B-RED-001", customer_id=customers[0].id,
                plate_name="正唛警示内容", color_name="专红", rack_location="1F-PL-R01-L1-P41",
                status="active", created_by=admin.id,
            ),
            PrintingPlate(
                plate_code="P151B-BLUE-002", customer_id=customers[0].id,
                plate_name="侧唛型号内容", color_name="专蓝", rack_location="1F-PL-R01-L1-P42",
                status="active", created_by=admin.id,
            ),
            PrintingPlate(
                plate_code="P151B-GREEN-003", customer_id=customers[0].id,
                plate_name="底唛环保内容", color_name="专绿", rack_location="1F-PL-R01-L1-P43",
                status="active", created_by=admin.id,
            ),
            PrintingPlate(
                plate_code="P151B-INACTIVE", customer_id=customers[0].id,
                plate_name="停用内容", color_name="灰色", rack_location="1F-PL-R01-L1-P44",
                status="inactive", created_by=admin.id,
            ),
            PrintingPlate(
                plate_code="P151B-FOREIGN", customer_id=customers[1].id,
                plate_name="乙客户内容", color_name="黑色", rack_location="1F-PL-R01-L1-P45",
                status="active", created_by=admin.id,
            ),
        ]
        db.add_all(plates)
        db.commit()
        app_state = {
            "admin_id": int(admin.id), "scoped_id": int(scoped.id),
            "workshop_id": int(workshop.id),
            "customer_a": int(customers[0].id), "customer_b": int(customers[1].id),
            "plate_ids": [int(row.id) for row in plates],
        }

    app = FastAPI()
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(version_router, prefix="/api/master-data")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    def override_current_user():
        with factory() as db:
            user = db.get(User, app.state.current_user_id)
            assert user is not None
            list(user.permission_overrides)
            return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.state.session_factory = factory
    for key, value in app_state.items():
        setattr(app.state, key, value)
    app.state.current_user_id = app.state.admin_id
    yield app
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_plate_rows_user(plate_rows_app: FastAPI) -> Generator[None, None, None]:
    plate_rows_app.state.current_user_id = plate_rows_app.state.admin_id
    yield
    plate_rows_app.state.current_user_id = plate_rows_app.state.admin_id


def _plate_product_payload(app: FastAPI, suffix: str, count: int, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "customer_id": app.state.customer_a,
        "product_code": f"P151B-P-{suffix}",
        "customer_material_code": f"P151B-C-{suffix}",
        "product_name": f"P1-51B 挂板常用箱 {suffix}",
        "box_category": "normal",
        "box_style": "A1",
        "print_content": {1: "单色印刷", 2: "双色印刷", 3: "三色印刷"}[count],
        "printing_plate_mode": "plate",
        "printing_colors": "客户端伪造颜色必须忽略",
    }
    for index, plate_id in enumerate(app.state.plate_ids[:count], start=1):
        payload[f"printing_plate_{index}_id"] = plate_id
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("count", [1, 2, 3])
def test_api_maps_one_to_three_ordered_rows_to_canonical_print_content(
    plate_rows_app: FastAPI, count: int,
) -> None:
    with TestClient(plate_rows_app) as client:
        response = client.post(
            "/api/master/products",
            json=_plate_product_payload(plate_rows_app, f"CANON-{count}", count),
        )
        assert response.status_code == 201, response.text
        saved = response.json()
        reopened = client.get(f"/api/master/products/{saved['id']}")

    expected_content = {1: "单色印刷", 2: "双色印刷", 3: "三色印刷"}[count]
    assert saved["print_content"] == expected_content
    assert saved["printing_plate_mode"] == "plate"
    assert saved["printing_colors"] is None
    assert [row["id"] for row in saved["printing_plates"]] == plate_rows_app.state.plate_ids[:count]
    assert [row["id"] for row in reopened.json()["printing_plates"]] == plate_rows_app.state.plate_ids[:count]


def test_generic_plate_projection_is_never_a_backend_payload(plate_rows_app: FastAPI) -> None:
    from app.models.product import Product

    payload = _plate_product_payload(plate_rows_app, "GENERIC", 1)
    payload["print_content"] = "挂板印刷"
    with plate_rows_app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(Product))
    with TestClient(plate_rows_app) as client:
        response = client.post("/api/master/products", json=payload)
    assert response.status_code == 422, response.text
    with plate_rows_app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Product)) == before


@pytest.mark.parametrize(
    ("suffix", "fields", "message"),
    [
        ("ZERO", {"printing_plate_1_id": None}, "选择 1 块挂板"),
        ("GAP", {"printing_plate_1_id": None, "printing_plate_2_id": 2}, "按颜色顺序"),
        ("DUP", {"printing_plate_2_id": 1}, "不能在一个常用箱中重复"),
        ("INACTIVE", {"printing_plate_1_id": 4}, "不是启用状态"),
        ("FOREIGN", {"printing_plate_1_id": 5}, "不属于当前客户"),
        ("MISSING", {"printing_plate_1_id": 999999}, "不存在"),
    ],
)
def test_invalid_plate_rows_fail_atomically(
    plate_rows_app: FastAPI, suffix: str, fields: dict[str, object], message: str,
) -> None:
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.product import Product

    count = 2 if suffix in {"GAP", "DUP"} else 1
    payload = _plate_product_payload(plate_rows_app, suffix, count, **fields)
    with plate_rows_app.state.session_factory() as db:
        before_products = db.scalar(select(func.count()).select_from(Product))
        before_versions = db.scalar(
            select(func.count()).select_from(MasterDataObjectVersion)
        )
    with TestClient(plate_rows_app) as client:
        response = client.post("/api/master/products", json=payload)
    assert response.status_code in {400, 422}, response.text
    assert message in response.text
    with plate_rows_app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Product)) == before_products
        assert (
            db.scalar(select(func.count()).select_from(MasterDataObjectVersion))
            == before_versions
        )


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ("P151B-RED-001", "P151B-RED-001"),
        ("侧唛型号", "P151B-BLUE-002"),
        ("专绿", "P151B-GREEN-003"),
    ],
)
def test_plate_search_covers_code_content_and_color_with_customer_scope(
    plate_rows_app: FastAPI, query: str, expected_code: str,
) -> None:
    with TestClient(plate_rows_app) as client:
        response = client.get(
            "/api/warehouse/printing-plates",
            params={"q": query, "customer_id": plate_rows_app.state.customer_a},
        )
        assert response.status_code == 200, response.text
        assert [row["plate_code"] for row in response.json()["items"]] == [expected_code]

        plate_rows_app.state.current_user_id = plate_rows_app.state.scoped_id
        scoped = client.get("/api/warehouse/printing-plates", params={"q": "P151B-"})
        forbidden = client.get(
            "/api/warehouse/printing-plates",
            params={"customer_id": plate_rows_app.state.customer_b},
        )
    assert scoped.status_code == 200, scoped.text
    assert all(row["customer_id"] == plate_rows_app.state.customer_a for row in scoped.json()["items"])
    assert forbidden.status_code == 403, forbidden.text


def _update_payload(product: dict, **overrides: object) -> dict:
    from app.api.products import ProductPayload
    payload = {
        field: product.get(field)
        for field in ProductPayload.model_fields
        if field in product
    }
    payload.update(expected_version=product["version"], change_reason="P1-51B 挂板行专项")
    payload.update(overrides)
    return payload


def _confirmed_put(client: TestClient, url: str, payload: dict) -> dict:
    first = client.put(url, json=payload)
    if first.status_code == 409 and isinstance(first.json().get("detail"), dict):
        token = first.json()["detail"].get("confirmation_token")
        if token:
            first = client.put(url, json={**payload, "confirmation_token": token})
    assert first.status_code == 200, first.text
    return first.json()


def test_update_keeps_cas_and_permission_guards(plate_rows_app: FastAPI) -> None:
    with TestClient(plate_rows_app) as client:
        created = client.post(
            "/api/master/products", json=_plate_product_payload(plate_rows_app, "CAS", 1)
        ).json()
        changed = _confirmed_put(
            client,
            f"/api/master/products/{created['id']}",
            _update_payload(
                created, print_content="双色印刷",
                printing_plate_2_id=plate_rows_app.state.plate_ids[1],
            ),
        )
        stale = client.put(
            f"/api/master/products/{created['id']}",
            json=_update_payload(created, remark="陈旧页面不得覆盖"),
        )
        plate_rows_app.state.current_user_id = plate_rows_app.state.workshop_id
        denied = client.put(
            f"/api/master/products/{created['id']}",
            json=_update_payload(changed, remark="无编辑权限不得写入"),
        )
        plate_rows_app.state.current_user_id = plate_rows_app.state.admin_id
        reopened = client.get(f"/api/master/products/{created['id']}").json()

    assert stale.status_code == 409, stale.text
    assert denied.status_code == 403, denied.text
    assert reopened["version"] == changed["version"]
    assert [row["id"] for row in reopened["printing_plates"]] == plate_rows_app.state.plate_ids[:2]


def test_switching_out_unbinds_without_deleting_plate_ledgers_or_history(
    plate_rows_app: FastAPI,
) -> None:
    from app.models.printing_plate import PrintingPlateLocationMovement, PrintingPlateResinReuse

    first_plate = plate_rows_app.state.plate_ids[0]
    with plate_rows_app.state.session_factory() as db:
        plate = db.get(__import__("app.models.printing_plate", fromlist=["PrintingPlate"]).PrintingPlate, first_plate)
        assert plate is not None
        db.add(
            PrintingPlateLocationMovement(
                printing_plate_id=plate.id, plate_code_snapshot=plate.plate_code,
                from_location="1F-PL-R01-L1-P40", to_location=plate.rack_location,
                actor_id=plate_rows_app.state.admin_id, idempotency_key="p151b-move-evidence",
                expected_version=1, resulting_version=2, source="manual_input",
            )
        )
        db.add(
            PrintingPlateResinReuse(
                printing_plate_id=plate.id, plate_code_snapshot=plate.plate_code,
                from_customer_id=plate_rows_app.state.customer_a,
                from_customer_name_snapshot="P1-51B 客户甲", from_plate_name_snapshot="旧正唛",
                from_color_name_snapshot="旧红", to_customer_id=plate_rows_app.state.customer_a,
                to_customer_name_snapshot="P1-51B 客户甲", to_plate_name_snapshot=plate.plate_name,
                to_color_name_snapshot=plate.color_name, rack_location_snapshot=plate.rack_location,
                actor_id=plate_rows_app.state.admin_id, actor_username_snapshot="p151b-admin",
                idempotency_key="p151b-reuse-evidence", expected_version=1, resulting_version=2,
                old_resin_removed=True, new_resin_mounted=True,
            )
        )
        db.commit()

    with TestClient(plate_rows_app) as client:
        created = client.post(
            "/api/master/products", json=_plate_product_payload(plate_rows_app, "UNLINK", 1)
        ).json()
        saved = _confirmed_put(
            client,
            f"/api/master/products/{created['id']}",
            _update_payload(
                created, print_content="单色印刷", printing_colors="PANTONE 186 C",
                printing_plate_mode="no_plate", printing_plate_1_id=None,
            ),
        )

    assert saved["printing_plate_mode"] == "no_plate"
    assert saved["printing_plate_1_id"] is None
    with plate_rows_app.state.session_factory() as db:
        from app.models.printing_plate import PrintingPlate
        plate = db.get(PrintingPlate, first_plate)
        assert plate is not None
        assert plate.rack_location == "1F-PL-R01-L1-P41"
        assert db.scalar(select(func.count()).select_from(PrintingPlateLocationMovement)) == 1
        assert db.scalar(select(func.count()).select_from(PrintingPlateResinReuse)) == 1


def test_history_restore_reuses_exact_plate_gate_and_cas(plate_rows_app: FastAPI) -> None:
    from app.models.printing_plate import PrintingPlate

    with TestClient(plate_rows_app) as client:
        original = client.post(
            "/api/master/products", json=_plate_product_payload(plate_rows_app, "RESTORE", 1)
        ).json()
        changed = _confirmed_put(
            client,
            f"/api/master/products/{original['id']}",
            _update_payload(original, printing_plate_1_id=plate_rows_app.state.plate_ids[1]),
        )
        with plate_rows_app.state.session_factory() as db:
            old_plate = db.get(PrintingPlate, plate_rows_app.state.plate_ids[0])
            assert old_plate is not None
            old_plate.status = "inactive"
            db.commit()
        invalid = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore-preview",
            json={"expected_version": changed["version"]},
        )
        with plate_rows_app.state.session_factory() as db:
            old_plate = db.get(PrintingPlate, plate_rows_app.state.plate_ids[0])
            assert old_plate is not None
            old_plate.status = "active"
            db.commit()
        stale = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore-preview",
            json={"expected_version": original["version"]},
        )
        preview = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore-preview",
            json={"expected_version": changed["version"]},
        )
        assert preview.status_code == 200, preview.text
        bad_token = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore",
            json={
                "expected_version": changed["version"], "reason": "错误令牌零写",
                "confirmation_token": "invalid-token",
            },
        )
        after_bad = client.get(f"/api/master/products/{original['id']}").json()
        restored = client.post(
            f"/api/master-data/product/{original['id']}/versions/1/restore",
            json={
                "expected_version": changed["version"], "reason": "P1-51B 恢复挂板顺序",
                "confirmation_token": preview.json()["confirmation_token"],
            },
        )
        reopened = client.get(f"/api/master/products/{original['id']}").json()

    assert invalid.status_code == 409 and "不是启用状态" in invalid.text
    assert stale.status_code == 409, stale.text
    assert bad_token.status_code == 409, bad_token.text
    assert after_bad["version"] == changed["version"]
    assert restored.status_code == 200, restored.text
    assert reopened["version"] == changed["version"] + 1
    assert reopened["printing_plate_1_id"] == plate_rows_app.state.plate_ids[0]


def test_backend_preserves_p1_51a_legacy_direct_color_raw_on_sibling_edit(
    plate_rows_app: FastAPI,
) -> None:
    from app.models.product import Product

    raw = "黑色,红色／蓝色|绿色"
    with plate_rows_app.state.session_factory() as db:
        product = Product(
            customer_id=plate_rows_app.state.customer_a,
            product_code="P151B-RAW", customer_material_code="P151B-RAW",
            product_name="P1-51A 原始颜色保护", box_category="normal", box_style="A1",
            print_content="多色印刷", printing_colors=raw, printing_plate_mode="no_plate",
            remark="原备注", is_active=True, version=1,
        )
        db.add(product)
        db.commit()
        product_id = int(product.id)
    with TestClient(plate_rows_app) as client:
        opened = client.get(f"/api/master/products/{product_id}").json()
        updated = client.put(
            f"/api/master/products/{product_id}",
            json=_update_payload(opened, remark="只改兄弟字段"),
        )
        assert updated.status_code == 200, updated.text
        reopened = client.get(f"/api/master/products/{product_id}").json()
    assert reopened["printing_colors"] == raw
    assert reopened["print_content"] == "多色印刷"


def test_frontend_search_merges_results_and_hydrated_bindings_without_overwrite(
    tmp_path: Path,
) -> None:
    assert '@search="searchPrintingPlates"' in INDEX
    assert "this.mergePrintingPlateOptions(detail?.printing_plates || [],form.customer_id)" in INDEX
    bodies = {
        "merge": _method_body("mergePrintingPlateOptions(rows, customerId=null) {", "async searchPrintingPlates("),
        "search": _method_body("async searchPrintingPlates(value) {", "async ensureProductEditorOptions("),
    }
    script = f"""
const methods={json.dumps(bodies, ensure_ascii=False)};
global.latestRequestControllers=new Map();
let observed=null;
global.axios={{get:async(url,options)=>{{observed={{url,options}};return {{data:{{items:[{{id:99,plate_code:'SEARCH',plate_name:'搜索结果',color_name:'专绿',customer_id:7,status:'active'}}]}}}};}}}};
const vm={{
  isWorkshop:false,productForm:{{customer_id:7}},
  printingPlates:[
    {{id:11,plate_code:'OLD',plate_name:'既有选中',color_name:'专红',customer_id:7,status:'active'}},
    {{id:22,plate_code:'OTHER',plate_name:'另一行选中',color_name:'专蓝',customer_id:7,status:'active'}}
  ],
  beginLatestRequest:key=>{{const controller={{signal:{{}}}};latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest:()=>{{}},isCancelledRequest:()=>false,showToast:()=>{{}},errorMessage:String,
}};
vm.mergePrintingPlateOptions=new Function('rows','customerId',methods.merge).bind(vm);
vm.searchPrintingPlates=new Function('value',`return (async()=>{{${{methods.search}}}})()`).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
vm.mergePrintingPlateOptions([{{id:501,plate_code:'HYDRATED',plate_name:'超页历史绑定',color_name:'黑色'}}],7);
expect(vm.printingPlates.some(row=>row.id===501&&row.customer_id===7),'hydrate binding was not merged with customer');
(async()=>{{
  expect(await vm.searchPrintingPlates('专绿'),'search failed');
  expect(observed.url==='/api/warehouse/printing-plates','wrong search endpoint');
  expect(observed.options.params.q==='专绿'&&observed.options.params.customer_id===7&&observed.options.params.limit===100,'wrong scoped search params');
  expect([11,22,501,99].every(id=>vm.printingPlates.some(row=>row.id===id)),'search overwrote an existing or selected option');
}})().catch(error=>{{console.error(error);process.exitCode=1;}});
"""
    _run_node(script, tmp_path, "p1-51b-plate-search-merge.js")


@pytest.mark.parametrize(
    "changed_field",
    [
        "plate_alignment_value_mm",
        "plate_mount_value_mm",
        "machine_set_length_mm",
        "machine_set_width_mm",
        "machine_set_height_mm",
    ],
)
def test_unknown_legacy_plate_content_canonicalizes_only_after_plate_configuration_change(
    plate_rows_app: FastAPI, tmp_path: Path, changed_field: str,
) -> None:
    bodies = {
        "count": _method_body("productPrintingPlateCount(form) {", "productPrintingPlateContent("),
        "original": _method_body("productPrintingOriginalSnapshot(form) {", "productPrintingConfigurationChanged("),
        "changed": _method_body("productPrintingConfigurationChanged(form) {", "productPrintingWriteFields("),
        "write": _method_body("productPrintingWriteFields(form) {", "productDirectPrintingColorCount("),
    }
    script = f"""
const methods={json.dumps(bodies, ensure_ascii=False)};
const vm={{}};
vm.productPrintingPlateCount=new Function('form',methods.count).bind(vm);
vm.productPrintingOriginalSnapshot=new Function('form',methods.original).bind(vm);
vm.productPrintingConfigurationChanged=new Function('form',methods.changed).bind(vm);
vm.productHasPrinting=()=>true;vm.productPrintingColorSummary=()=>'';
vm.productPrintingWriteFields=new Function('form',methods.write).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
const original={{
  print_content:'红黑双色印刷',printing_colors:null,printing_plate_mode:'plate',
  printing_plate_1_id:1,printing_plate_2_id:2,printing_plate_3_id:null,
  plate_alignment_value_mm:'1.20',plate_mount_value_mm:'2.30',
  machine_set_length_mm:'500.00',machine_set_width_mm:'320.00',machine_set_height_mm:'280.00',
  _printing_plate_count:2,_printing_situation:'挂板印刷',_printing_colors:[]
}};
const form={{id:9,...original,_printing_original:{{...original}}}};
form.remark='只改兄弟字段';
expect(!vm.productPrintingConfigurationChanged(form),'sibling edit changed legacy plate configuration');
expect(vm.productPrintingWriteFields(form).print_content==='红黑双色印刷','untouched legacy raw was rewritten');
form.plate_alignment_value_mm=1.2;
expect(!vm.productPrintingConfigurationChanged(form),'numeric string equivalent was treated as change');
form.{changed_field}=Number(form.{changed_field})+1;
expect(vm.productPrintingConfigurationChanged(form),'plate machine field change was missed');
const payload=vm.productPrintingWriteFields(form);
expect(payload.print_content==='双色印刷','changed legacy plate content was not canonicalized from row count');
expect(payload.printing_plate_1_id===1&&payload.printing_plate_2_id===2,'ordered plate ids changed');
"""
    _run_node(script, tmp_path, f"p1-51b-legacy-{changed_field}.js")

    with TestClient(plate_rows_app) as client:
        created_response = client.post(
            "/api/master/products",
            json=_plate_product_payload(
                plate_rows_app, f"LEGACY-{changed_field}", 2,
                plate_alignment_value_mm="1.20", plate_mount_value_mm="2.30",
                machine_set_length_mm="500.00", machine_set_width_mm="320.00",
                machine_set_height_mm="280.00",
            ),
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        changed_value = {
            "plate_alignment_value_mm": "2.20",
            "plate_mount_value_mm": "3.30",
            "machine_set_length_mm": "501.00",
            "machine_set_width_mm": "321.00",
            "machine_set_height_mm": "281.00",
        }[changed_field]
        saved = _confirmed_put(
            client,
            f"/api/master/products/{created['id']}",
            _update_payload(created, **{changed_field: changed_value}),
        )
    assert saved["print_content"] == "双色印刷"
    assert saved[changed_field] == changed_value


def test_unknown_legacy_plate_id_change_canonicalizes_payload(tmp_path: Path) -> None:
    bodies = {
        "count": _method_body("productPrintingPlateCount(form) {", "productPrintingPlateContent("),
        "original": _method_body("productPrintingOriginalSnapshot(form) {", "productPrintingConfigurationChanged("),
        "changed": _method_body("productPrintingConfigurationChanged(form) {", "productPrintingWriteFields("),
        "write": _method_body("productPrintingWriteFields(form) {", "productDirectPrintingColorCount("),
    }
    script = f"""
const methods={json.dumps(bodies, ensure_ascii=False)};const vm={{}};
vm.productPrintingPlateCount=new Function('form',methods.count).bind(vm);
vm.productPrintingOriginalSnapshot=new Function('form',methods.original).bind(vm);
vm.productPrintingConfigurationChanged=new Function('form',methods.changed).bind(vm);
vm.productHasPrinting=()=>true;vm.productPrintingColorSummary=()=>'';
vm.productPrintingWriteFields=new Function('form',methods.write).bind(vm);
const original={{print_content:'红黑双色印刷',printing_colors:null,printing_plate_mode:'plate',printing_plate_1_id:1,printing_plate_2_id:2,printing_plate_3_id:null,_printing_plate_count:2,_printing_situation:'挂板印刷',_printing_colors:[]}};
const form={{id:9,...original,_printing_original:{{...original}},printing_plate_2_id:3}};
const payload=vm.productPrintingWriteFields(form);
if(payload.print_content!=='双色印刷'||payload.printing_plate_2_id!==3)throw new Error('plate id change did not canonicalize ordered payload');
"""
    _run_node(script, tmp_path, "p1-51b-legacy-plate-id.js")
