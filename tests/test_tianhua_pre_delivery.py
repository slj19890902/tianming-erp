from pathlib import Path
from datetime import date
from decimal import Decimal
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func,select
from sqlalchemy.orm import sessionmaker


def test_supplied_screenshot_recognizes_30_rows():
    from app.services.tianhua_pre_delivery import recognize_tianhua_image
    p=Path(r"C:\Users\Administrator\Desktop\Catch (2).jpg")
    if not p.exists(): pytest.skip("sample unavailable")
    rows=recognize_tianhua_image(p.read_bytes())
    assert len(rows)==30
    assert (rows[0].stock_code,rows[0].image_qty)==("21301877",200)
    assert (rows[-1].stock_code,rows[-1].image_qty)==("21301466",20)


def test_api_creates_isolated_draft_and_blocks_duplicate(tmp_path,monkeypatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.tianhua_pre_delivery import router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.order import Order,OrderItem
    from app.models.product import Product
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryDraft, TianhuaPreDeliveryImportItem
    from app.models.user import User
    from app.services import tianhua_pre_delivery as svc
    engine=create_sqlite_engine(tmp_path/"db.sqlite3");Base.metadata.create_all(engine);factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        u=User(username="sales",password_hash=hash_password("RolePass123!"),role="sales",real_name="销售",display_name="销售",must_change_password=False)
        c=Customer(customer_number=5,customer_code="天华",name="苏州天华超净科技股份有限公司",credit_limit=Decimal("0"))
        db.add_all([u,c]);db.flush()
        p=Product(customer_id=c.id,product_code="21301877",customer_material_code="21301877",product_name="测试",box_category="normal")
        db.add(p);db.flush()
        o=Order(order_number="TH-1",customer_id=c.id,order_date=date.today(),status="pending_delivery",payment_status="unpaid",total_amount=Decimal("0"))
        db.add(o);db.flush()
        oi=OrderItem(order_id=o.id,product_id=p.id,quantity=200,delivered_quantity=0,unit_price=Decimal("0"),subtotal=Decimal("0"),material_status="received",snapshot_product_name="测试",snapshot_product_code="21301877")
        db.add(oi);db.commit()
    monkeypatch.setattr(svc,"recognize_tianhua_image",lambda _: [svc.RecognizedRow(1,"21301877 200","21301877",200)])
    app=FastAPI();app.include_router(auth_router,prefix="/api/auth");app.include_router(router,prefix="/api/deliveries")
    def override():
        with factory() as db: yield db
    app.dependency_overrides[get_db]=override
    with TestClient(app) as client:
        assert client.post("/api/auth/login",json={"username":"sales","password":"RolePass123!"}).status_code==200
        up=client.post("/api/deliveries/tianhua-preimport/upload",files={"file":("x.png",b"x","image/png")})
        assert up.status_code==201 and up.json()["items"][0]["status"]=="ok"
        item_id=up.json()["items"][0]["item_id"]
        other=client.post("/api/deliveries/tianhua-preimport/upload",files={"file":("y.png",b"y","image/png")})
        wrong_batch=client.post(
            f"/api/deliveries/tianhua-preimport/{other.json()['batch_id']}/create-draft",
            json={"items":[{"item_id":item_id,"row_no":1,"selected":True,"final_delivery_qty":200}]},
        )
        duplicate_line=client.post(
            f"/api/deliveries/tianhua-preimport/{up.json()['batch_id']}/create-draft",
            json={"items":[
                {"item_id":item_id,"row_no":1,"selected":True,"final_delivery_qty":200},
                {"item_id":item_id,"row_no":1,"selected":True,"final_delivery_qty":200},
            ]},
        )
        blocked_responses=[]
        for blocked_status in ("not_matched","ocr_failed"):
            blocked=client.post("/api/deliveries/tianhua-preimport/upload",files={"file":(f"{blocked_status}.png",b"x","image/png")})
            blocked_item=blocked.json()["items"][0]
            with factory() as db:
                db.get(TianhuaPreDeliveryImportItem,blocked_item["item_id"]).status=blocked_status
                db.commit()
            blocked_responses.append(client.post(
                f"/api/deliveries/tianhua-preimport/{blocked.json()['batch_id']}/create-draft",
                json={"items":[{"item_id":blocked_item["item_id"],"row_no":1,"selected":True,"final_delivery_qty":200}]},
            ))
        payload={"items":[{"item_id":item_id,"row_no":1,"selected":True,"final_delivery_qty":200}]}
        first=client.post(f"/api/deliveries/tianhua-preimport/{up.json()['batch_id']}/create-draft",json=payload)
        second=client.post(f"/api/deliveries/tianhua-preimport/{up.json()['batch_id']}/create-draft",json=payload)
    assert wrong_batch.status_code==400
    assert duplicate_line.status_code==400
    assert [response.status_code for response in blocked_responses]==[400,400]
    assert first.status_code==201 and second.status_code==409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(TianhuaPreDeliveryDraft))==1
        assert db.scalar(select(func.count()).select_from(Delivery))==0
        assert db.get(OrderItem,1).delivered_quantity==0
