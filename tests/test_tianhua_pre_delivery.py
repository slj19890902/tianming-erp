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


def test_api_creates_linked_pending_delivery_and_blocks_duplicate(tmp_path,monkeypatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.deliveries import router as deliveries_router
    from app.api.tianhua_pre_delivery import mobile_router, router
    from app.core.database import create_sqlite_engine
    from app.core.security import create_tianhua_pick_token, hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order,OrderItem
    from app.models.product import Product
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryDraft, TianhuaPreDeliveryDraftItem, TianhuaPreDeliveryImportItem
    from app.models.user import User
    from app.services import tianhua_pre_delivery as svc
    monkeypatch.setenv("ERP_SECRET_KEY","customer-remark-mobile-test-secret")
    engine=create_sqlite_engine(tmp_path/"db.sqlite3");Base.metadata.create_all(engine);factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        u=User(username="admin",password_hash=hash_password("RolePass123!"),role="admin",real_name="管理员",display_name="管理员",must_change_password=False)
        c=Customer(customer_number=5,customer_code="TH",name="苏州天华超净科技股份有限公司",credit_limit=Decimal("0"))
        db.add_all([u,c]);db.flush()
        p=Product(customer_id=c.id,product_code="21301877",customer_material_code="21301877",product_name="测试",box_category="normal")
        db.add(p);db.flush()
        o=Order(order_number="TH-1",customer_id=c.id,order_date=date.today(),status="pending_delivery",payment_status="unpaid",total_amount=Decimal("0"))
        db.add(o);db.flush()
        oi=OrderItem(order_id=o.id,product_id=p.id,quantity=200,delivered_quantity=0,unit_price=Decimal("0"),subtotal=Decimal("0"),material_status="received",snapshot_product_name="测试",snapshot_product_code="21301877")
        db.add(oi);db.commit()
    monkeypatch.setattr(svc,"recognize_tianhua_image",lambda _: [svc.RecognizedRow(1,"21301877 200","21301877",200)])
    app=FastAPI();app.include_router(auth_router,prefix="/api/auth");app.include_router(deliveries_router,prefix="/api/deliveries");app.include_router(router,prefix="/api/deliveries");app.include_router(mobile_router,prefix="/api/mobile")
    def override():
        with factory() as db: yield db
    app.dependency_overrides[get_db]=override
    with TestClient(app) as client:
        assert client.post("/api/auth/login",json={"username":"admin","password":"RolePass123!"}).status_code==200
        png=b"\x89PNG\r\n\x1a\nstub"
        up=client.post("/api/deliveries/tianhua-preimport/upload",files={"file":("x.png",png,"image/png")})
        assert up.status_code==201 and up.json()["items"][0]["status"]=="ok"
        item_id=up.json()["items"][0]["item_id"]
        other=client.post("/api/deliveries/tianhua-preimport/upload",files={"file":("y.png",png,"image/png")})
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
            blocked=client.post("/api/deliveries/tianhua-preimport/upload",files={"file":(f"{blocked_status}.png",png,"image/png")})
            blocked_item=blocked.json()["items"][0]
            with factory() as db:
                db.get(TianhuaPreDeliveryImportItem,blocked_item["item_id"]).status=blocked_status
                db.commit()
            blocked_responses.append(client.post(
                f"/api/deliveries/tianhua-preimport/{blocked.json()['batch_id']}/create-draft",
                json={"items":[{"item_id":blocked_item["item_id"],"row_no":1,"selected":True,"final_delivery_qty":200}]},
            ))
        payload={"remark":"来源：天华预送货图片导入","items":[{"item_id":item_id,"row_no":1,"selected":True,"final_delivery_qty":200}]}
        first=client.post(f"/api/deliveries/tianhua-preimport/{up.json()['batch_id']}/create-draft",json=payload)
        second=client.post(f"/api/deliveries/tianhua-preimport/{up.json()['batch_id']}/create-draft",json=payload)
        with factory() as db:
            draft=db.scalars(select(TianhuaPreDeliveryDraft)).one()
            delivery_item=db.scalars(select(DeliveryItem)).one()
            assert delivery_item.remarks is None
            assert "来源：天华预送货图片导入" in draft.remark
            legacy_internal_remark=f"来源：天华预送货草稿 {draft.draft_number}"
            delivery_item.remarks=legacy_internal_remark
            delivery_item_id=delivery_item.id
            delivery_id=delivery_item.delivery_id
            db.commit()
        detail=client.get(f"/api/deliveries/{delivery_id}")
        print_data=client.get(f"/api/deliveries/{delivery_id}/print")
        with factory() as db:
            delivery_item=db.get(DeliveryItem,delivery_item_id)
            assert delivery_item.remarks==legacy_internal_remark
            delivery_item.remarks="请核对数量后签字"
            draft_item_id=db.scalars(select(TianhuaPreDeliveryDraftItem)).one().id
            db.commit()
        mobile_token,_expires=create_tianhua_pick_token(
            up.json()["batch_id"],
            draft.id,
        )
        updated=client.put(
            f"/api/mobile/tianhua-pick/items/{draft_item_id}",
            json={
                "token":mobile_token,
                "mobile_pick_status":"picked",
                "mobile_picked_qty":200,
                "mobile_pick_note":"现场确认 200",
            },
        )
        detail_after_update=client.get(f"/api/deliveries/{delivery_id}")
        print_after_update=client.get(f"/api/deliveries/{delivery_id}/print")
        with factory() as db:
            db.get(Delivery,delivery_id).status="voided"
            db.commit()
        voided_update=client.put(
            f"/api/deliveries/tianhua-preimport/{up.json()['batch_id']}/update-draft",
            json=payload,
        )
        with factory() as db:
            db.get(Delivery,delivery_id).status="pending"
            db.commit()
    assert wrong_batch.status_code==400
    assert duplicate_line.status_code==400
    assert [response.status_code for response in blocked_responses]==[400,400]
    assert first.status_code==201 and second.status_code==409
    assert first.json()["delivery_number"].startswith("TH-")
    assert first.json()["items"][0]["order_id"] == 1
    assert first.json()["items"][0]["order_no"] == "TH-1"
    assert detail.status_code==200 and detail.json()["items"][0]["remarks"] is None
    assert print_data.status_code==200 and print_data.json()["items"][0]["remarks"] is None
    assert updated.status_code==200
    assert detail_after_update.json()["items"][0]["remarks"]=="请核对数量后签字"
    assert print_after_update.json()["items"][0]["remarks"]=="请核对数量后签字"
    assert "现场确认 200" not in print_after_update.text
    assert voided_update.status_code==400
    assert "关联送货单已作废" in voided_update.json()["detail"]
    assert "重新上传预送货截图" in voided_update.json()["detail"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(TianhuaPreDeliveryDraft))==1
        assert db.scalar(select(func.count()).select_from(Delivery))==1
        delivery=db.scalars(select(Delivery)).one()
        assert delivery.status=="pending"
        assert delivery.total_quantity==200
        assert db.get(OrderItem,1).delivered_quantity==0
        assert db.get(DeliveryItem,delivery_item_id).remarks=="请核对数量后签字"
        assert db.get(TianhuaPreDeliveryDraftItem,draft_item_id).mobile_pick_note=="现场确认 200"
