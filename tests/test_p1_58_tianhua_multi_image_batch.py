from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"
PNG = b"\x89PNG\r\n\x1a\n"


def _client(tmp_path, monkeypatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.tianhua_pre_delivery import router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User
    from app.services import tianhua_pre_delivery as service

    engine = create_sqlite_engine(tmp_path / "p1_58.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            display_name="管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=58,
            customer_code="TH",
            name="苏州天华超净科技股份有限公司",
            credit_limit=Decimal("0"),
        )
        db.add_all([user, customer])
        db.flush()
        for index, code in enumerate(("21300001", "21300002", "21300003"), 1):
            product = Product(
                customer_id=customer.id,
                product_code=code,
                customer_material_code=code,
                product_name=f"天华测试产品{index}",
                box_category="normal",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"TH-P158-{index}",
                customer_id=customer.id,
                order_date=date.today(),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=index * 10,
                    delivered_quantity=0,
                    unit_price=Decimal("0"),
                    subtotal=Decimal("0"),
                    material_status="received",
                    snapshot_product_name=product.product_name,
                    snapshot_product_code=code,
                )
            )
        db.commit()

    def recognize(content: bytes):
        if content.endswith(b"bad-ocr"):
            raise ValueError("未识别到天华表格行")
        if content.endswith(b"first"):
            return [
                service.RecognizedRow(1, "21300001 10", "21300001", 10),
                service.RecognizedRow(2, "21300002 20", "21300002", 20),
            ]
        return [service.RecognizedRow(1, "21300003 30", "21300003", 30)]

    monkeypatch.setattr(service, "recognize_tianhua_image", recognize)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(router, prefix="/api/deliveries")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    assert client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "RolePass123!"},
    ).status_code == 200
    return client, factory


def _batch_count(factory) -> tuple[int, int]:
    from app.models.tianhua_pre_delivery import (
        TianhuaPreDeliveryImportBatch,
        TianhuaPreDeliveryImportItem,
    )

    with factory() as db:
        return (
            db.scalar(select(func.count()).select_from(TianhuaPreDeliveryImportBatch)),
            db.scalar(select(func.count()).select_from(TianhuaPreDeliveryImportItem)),
        )


def test_multiple_images_form_one_ordered_batch_and_legacy_single_file_still_works(
    tmp_path, monkeypatch
):
    client, factory = _client(tmp_path, monkeypatch)
    with client:
        response = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                ("files", ("01-first.png", PNG + b"first", "image/png")),
                ("files", ("02-second.png", PNG + b"second", "image/png")),
            ],
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["total_rows"] == 3
        assert [row["row_no"] for row in payload["items"]] == [1, 2, 3]
        assert [row["stock_code"] for row in payload["items"]] == [
            "21300001",
            "21300002",
            "21300003",
        ]
        assert _batch_count(factory) == (1, 3)

        legacy = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files={"file": ("legacy.png", PNG + b"second", "image/png")},
        )
        assert legacy.status_code == 201, legacy.text
        assert legacy.json()["total_rows"] == 1
        assert _batch_count(factory) == (2, 4)


def test_duplicate_corrupt_and_ocr_failure_leave_no_partial_batch(tmp_path, monkeypatch):
    client, factory = _client(tmp_path, monkeypatch)
    with client:
        duplicate = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                ("files", ("a.png", PNG + b"same", "image/png")),
                ("files", ("b.png", PNG + b"same", "image/png")),
            ],
        )
        assert duplicate.status_code == 400
        assert "重复" in duplicate.text
        assert _batch_count(factory) == (0, 0)

        corrupt = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                ("files", ("first.png", PNG + b"first", "image/png")),
                ("files", ("broken.png", b"not-an-image", "image/png")),
                ("files", ("last.png", PNG + b"second", "image/png")),
            ],
        )
        assert corrupt.status_code == 400
        assert _batch_count(factory) == (0, 0)

        ocr_failure = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                ("files", ("first.png", PNG + b"first", "image/png")),
                ("files", ("bad.png", PNG + b"bad-ocr", "image/png")),
            ],
        )
        assert ocr_failure.status_code == 400
        assert "未识别" in ocr_failure.text
        assert _batch_count(factory) == (0, 0)


def test_file_count_and_total_byte_limits_are_fail_closed(tmp_path, monkeypatch):
    from app.api import tianhua_pre_delivery as api

    client, factory = _client(tmp_path, monkeypatch)
    with client:
        too_many = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                ("files", (f"{index}.png", PNG + str(index).encode(), "image/png"))
                for index in range(api.TIANHUA_MAX_IMAGE_COUNT + 1)
            ],
        )
        assert too_many.status_code == 400
        assert "张" in too_many.text
        assert _batch_count(factory) == (0, 0)

        monkeypatch.setattr(api, "TIANHUA_MAX_TOTAL_BYTES", len(PNG) * 2 + 1)
        too_large = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                ("files", ("a.png", PNG + b"a", "image/png")),
                ("files", ("b.png", PNG + b"b", "image/png")),
            ],
        )
        assert too_large.status_code == 400
        assert "总大小" in too_large.text
        assert _batch_count(factory) == (0, 0)


def test_maximum_image_count_is_one_batch_and_cross_image_order_duplicates_do_not_create_draft(
    tmp_path, monkeypatch
):
    from app.api import tianhua_pre_delivery as api
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryDraft

    client, factory = _client(tmp_path, monkeypatch)
    with client:
        maximum = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                (
                    "files",
                    (f"{index:02}.png", PNG + f"maximum-{index}".encode(), "image/png"),
                )
                for index in range(api.TIANHUA_MAX_IMAGE_COUNT)
            ],
        )
        assert maximum.status_code == 201, maximum.text
        assert maximum.json()["total_rows"] == api.TIANHUA_MAX_IMAGE_COUNT
        assert [row["row_no"] for row in maximum.json()["items"]] == list(
            range(1, api.TIANHUA_MAX_IMAGE_COUNT + 1)
        )

        duplicate_order = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files=[
                ("files", ("same-order-a.png", PNG + b"same-order-a", "image/png")),
                ("files", ("same-order-b.png", PNG + b"same-order-b", "image/png")),
            ],
        )
        assert duplicate_order.status_code == 201, duplicate_order.text
        rows = duplicate_order.json()["items"]
        assert len(rows) == 2
        assert rows[0]["order_item_id"] == rows[1]["order_item_id"]
        draft = client.post(
            f"/api/deliveries/tianhua-preimport/{duplicate_order.json()['batch_id']}/create-draft",
            json={
                "items": [
                    {
                        "item_id": row["item_id"],
                        "row_no": row["row_no"],
                        "selected": True,
                        "final_delivery_qty": 30,
                    }
                    for row in rows
                ]
            },
        )
        assert draft.status_code == 400
        assert "重复绑定同一订单明细" in draft.text

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(TianhuaPreDeliveryDraft)) == 0
        assert db.scalar(select(func.count()).select_from(Delivery)) == 0
        assert db.scalar(select(func.count()).select_from(DeliveryItem)) == 0


def test_upload_transaction_failure_rolls_back_batch_and_rows(tmp_path, monkeypatch):
    from app.api import tianhua_pre_delivery as api

    client, factory = _client(tmp_path, monkeypatch)
    original = api.create_batch

    def fail_after_flush(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(api, "create_batch", fail_after_flush)
    with client, pytest.raises(RuntimeError, match="injected transaction failure"):
        client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files={"file": ("failure.png", PNG + b"first", "image/png")},
        )
    assert _batch_count(factory) == (0, 0)


def test_frontend_keeps_a_stable_removable_file_list_and_submits_once():
    source = INDEX.read_text(encoding="utf-8")
    modal_start = source.index('<div v-else-if="modal.type === \'tianhuaPreimport\'">')
    modal_end = source.index('<div v-else-if="modal.type === \'statement\'">', modal_start)
    modal = source[modal_start:modal_end]
    method_start = source.index("onTianhuaImageSelected(event)")
    method_end = source.index("useTianhuaImageQty(row)", method_start)
    methods = source[method_start:method_end]

    assert "multiple" in modal
    assert "已选 ${tianhuaPreimport.files.length} 张" in modal
    assert 'v-for="(file,index) in tianhuaPreimport.files"' in modal
    assert "removeTianhuaImage(index)" in modal
    assert "files:[]" in source
    assert 'form.append("files", file)' in methods
    assert "if (this.loading) return false" in methods
    assert 'form.append("file",' not in methods


def test_frontend_double_click_starts_only_one_upload_request():
    source = INDEX.read_text(encoding="utf-8")
    marker = "async startTianhuaRecognition() {"
    start = source.index(marker) + len(marker)
    end = source.index("\n          },\n          useTianhuaImageQty", start)
    body = source[start:end]
    script = f"""
const assert = require('assert');
class FakeFormData {{
  constructor() {{ this.values=[]; }}
  append(name,value) {{ this.values.push([name,value]); }}
}}
globalThis.FormData=FakeFormData;
let resolvePost;
let postCount=0;
globalThis.axios={{post:()=>{{postCount+=1;return new Promise(resolve=>{{resolvePost=resolve;}});}}}};
const vm={{
  loading:false,
  tianhuaPreimport:{{files:[{{name:'a.png'}},{{name:'b.png'}}],preDeliveryDate:'2026-08-15',batchId:null,batchNumber:'',items:[],draft:null}},
  showToast:()=>{{}}, errorMessage:error=>String(error),
}};
const upload=new Function('return async function() {{'+{json.dumps(body, ensure_ascii=False)}+'}}')();
(async()=>{{
  const first=upload.call(vm);
  const second=upload.call(vm);
  assert.strictEqual(await second,false);
  assert.strictEqual(postCount,1);
  resolvePost({{data:{{batch_id:1,batch_number:'TH-1',pre_delivery_date:'2026-08-15',items:[],draft:null,total_rows:0}}}});
  assert.strictEqual(await first,true);
  assert.strictEqual(postCount,1);
  assert.strictEqual(vm.loading,false);
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=INDEX.parent.parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
