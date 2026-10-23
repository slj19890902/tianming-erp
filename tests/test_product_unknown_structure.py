"""HTTP regression for edits of incomplete imported masters on isolated SQLite."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_master_data_versioning_p4_writers import writer_app
from app.models.customer import Customer
from app.models.material import Material
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.master_data_object_version import MasterDataObjectVersion

URL = "/api/master/products"
FIELDS = ("box_style", "splice_mode", "pieces_per_box", "flap_mm", "production_process")


def seed(app, known=False):
    with app.state.session_factory() as db:
        customer = Customer(customer_number=991, customer_code="LEGACY", name="合成旧资料客户")
        material = Material(code="L729Y", supplier_name="P4供应商A1", layer_count=5,
                            quote_price=2, price_unit="元/㎡", is_active=True)
        db.add_all([customer, material]); db.flush()
        product = Product(customer_id=customer.id, product_code="LEGACY-8173", customer_material_code="8173",
            product_name="合成旧纸箱", box_category="normal", box_style="WC 五层钉箱",
            length_mm=320, width_mm=240, height_mm=270, layer_count=5, flute_type="AB", unit="只",
            material_id=None, splice_mode="single" if known else None,
            pieces_per_box=1 if known else None, flap_mm=45 if known else None,
            production_process="打钉" if known else None,
            print_content=None, printing_colors=None, printing_plate_mode="no_plate",
            sale_unit_price=3, version=2)
        db.add(product); db.flush()
        order = Order(order_number="FROZEN-8173", customer_id=customer.id, order_date=date(2026,9,1), total_amount=30)
        db.add(order); db.flush()
        item = OrderItem(order_id=order.id, product_id=product.id, quantity=10, unit_price=3, subtotal=30,
            snapshot_product_name="原冻结名称", snapshot_flap_mm=55, snapshot_report_length_mm=1190,
            snapshot_report_width_mm=555, snapshot_material="原冻结材质")
        db.add(item); db.commit()
        return product.id, material.id, item.id


def body(client, pid, **changes):
    response = client.get(f"{URL}/{pid}")
    assert response.status_code == 200, response.text
    result = response.json()
    result["expected_version"] = result["version"]
    result["structure_edit_fields"] = []
    result.update(changes)
    return result


def save(client, pid, payload):
    preview = client.post(f"{URL}/{pid}/update-preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_update"]
    result = client.put(f"{URL}/{pid}", json={**payload, "confirmation_token":preview.json()["confirmation_token"]})
    assert result.status_code == 200, result.text
    return result.json(), preview.json()


def counts(app):
    with app.state.session_factory() as db:
        return db.scalar(select(func.count()).select_from(MasterDataObjectVersion))


@pytest.mark.parametrize("known", [False, True])
def test_procurement_and_printing_edit_keeps_actual_structure_and_frozen_order(writer_app, known):
    pid, material_id, item_id = seed(writer_app, known)
    with TestClient(writer_app) as client:
        original = body(client, pid)
        updated, preview = save(client, pid, body(client, pid, material_id=material_id,
            report_length_mm=1160, report_width_mm=555, crease_type="压线",
            crease_left_mm=142, crease_middle_mm=271, crease_right_mm=142,
            print_content="单色印刷", printing_colors="黑色"))
        for field in FIELDS:
            assert updated[field] == original[field], (field, updated[field], original[field])
        assert updated["version"] == 3 and updated["printing_colors"] == "黑色"
        reread = body(client, pid)
        assert all(reread[field] == original[field] for field in FIELDS)
    with writer_app.state.session_factory() as db:
        item = db.get(OrderItem, item_id)
        assert (item.snapshot_flap_mm, item.snapshot_report_length_mm, item.snapshot_report_width_mm,
                item.snapshot_material) == (55,1190,555,"原冻结材质")
        versions = db.scalars(select(MasterDataObjectVersion).where(MasterDataObjectVersion.object_id==pid,
            MasterDataObjectVersion.object_type=="product")).all()
        assert versions


def test_legacy_client_default_values_are_not_new_historical_facts(writer_app):
    pid, _, _ = seed(writer_app)
    with TestClient(writer_app) as client:
        payload=body(client,pid,box_style="A1/0201 普通开槽箱",splice_mode="single",pieces_per_box=1,
            flap_mm=30,production_notes="仅备注改动")
        payload.pop("structure_edit_fields")
        updated,_=save(client,pid,payload)
        assert updated["box_style"]=="A1/0201 普通开槽箱"
        assert all(updated[field] is None for field in FIELDS[1:])


def test_explicit_structure_edit_then_normal_ui_correction_to_unknown(writer_app):
    pid, _, _ = seed(writer_app)
    with TestClient(writer_app) as client:
        changed,_=save(client,pid,body(client,pid,structure_edit_fields=list(FIELDS),
            box_style="A1/0201 普通开槽箱",splice_mode="double",pieces_per_box=2,flap_mm=45,
            production_process="打钉",print_content="单色印刷",printing_colors="黑色",
            report_length_mm=1160,report_width_mm=555,crease_type="压线",
            crease_left_mm=142,crease_middle_mm=271,crease_right_mm=142))
        assert (changed["splice_mode"],changed["pieces_per_box"],changed["flap_mm"]) == ("double",2,45)
        corrected,preview=save(client,pid,body(client,pid,structure_edit_fields=list(FIELDS),
            box_style="WC 五层钉箱",splice_mode=None,pieces_per_box=None,flap_mm=None,
            production_process=None,change_reason="撤回无依据默认结构；保留已确认采购与印刷"))
        assert corrected["box_style"]=="A1/0201 普通开槽箱"
        assert all(corrected[field] is None for field in FIELDS[1:])
        assert corrected["printing_colors"]=="黑色" and corrected["print_content"]=="单色印刷"
        assert float(corrected["report_length_mm"])==1160
        assert corrected["unit_needs_review"] and corrected["version"]==4


@pytest.mark.parametrize("changes", [
    {"structure_edit_fields":["splice_mode"],"splice_mode":"double"},
    {"structure_edit_fields":["splice_mode","pieces_per_box"],"splice_mode":None,"pieces_per_box":1},
    {"structure_edit_fields":["splice_mode","pieces_per_box"],"splice_mode":"double","pieces_per_box":1},
    {"structure_edit_fields":["box_style"],"box_style":"模切内盒"},
    {"structure_edit_fields":["production_process"],"production_process":"模切","mold_tool_id":None},
    {"print_content":"单色印刷","printing_colors":None},
])
def test_invalid_structure_or_existing_print_mold_guard_has_no_write(writer_app, changes):
    pid, _, _=seed(writer_app)
    before=counts(writer_app)
    with TestClient(writer_app) as client:
        result=client.put(f"{URL}/{pid}",json=body(client,pid,**changes))
        assert result.status_code in (400,422), result.text
        assert body(client,pid)["version"]==2
    assert counts(writer_app)==before


def test_preview_cas_and_transaction_failure_leave_master_and_history_unchanged(writer_app, monkeypatch):
    from app.api import products
    pid, _, _=seed(writer_app)
    with TestClient(writer_app, raise_server_exceptions=False) as client:
        stale=body(client,pid,production_notes="旧预览")
        preview=client.post(f"{URL}/{pid}/update-preview",json=stale).json()
        save(client,pid,body(client,pid,production_notes="先保存"))
        result=client.put(f"{URL}/{pid}",json={**stale,"confirmation_token":preview["confirmation_token"]})
        assert result.status_code==409
        before=counts(writer_app)
        payload=body(client,pid,production_notes="晚失败不应保存")
        preview=client.post(f"{URL}/{pid}/update-preview",json=payload).json()
        def late_failure(db):
            db.flush()
            raise RuntimeError("synthetic late failure")
        monkeypatch.setattr(products,"commit_business_change",late_failure)
        result=client.put(f"{URL}/{pid}",json={**payload,"confirmation_token":preview["confirmation_token"]})
        assert result.status_code==500
        reread=body(client,pid)
        assert reread["version"]==3 and reread["production_notes"]=="先保存"
        assert reread["flap_mm"] is None and counts(writer_app)==before


def test_unselected_structure_payload_cannot_sneak_changes(writer_app):
    pid, _, _=seed(writer_app,True)
    with TestClient(writer_app) as client:
        updated,_=save(client,pid,body(client,pid,flap_mm=60,production_process="无需结合",
            production_notes="只保存备注"))
        assert updated["flap_mm"]==45 and updated["production_process"]=="打钉"


def test_applicability_is_checked_again_after_restoring_untouched_process(writer_app):
    pid, _, _=seed(writer_app)
    with writer_app.state.session_factory() as db:
        product=db.get(Product,pid)
        product.box_style="模切内盒"; product.box_category="die_cut"
        product.production_process="粘贴,模切,二次粘合"
        db.commit()
    with TestClient(writer_app) as client:
        payload=body(client,pid,production_process=None,box_category="normal")
        result=client.put(f"{URL}/{pid}",json=payload)
        assert result.status_code==400 and "二次粘合" in result.text
        assert body(client,pid)["version"]==2


def test_numeric_string_piece_count_does_not_become_server_error(writer_app):
    pid, _, _=seed(writer_app)
    with TestClient(writer_app) as client:
        updated,_=save(client,pid,body(client,pid,
            structure_edit_fields=["splice_mode","pieces_per_box"],splice_mode="double",pieces_per_box="2.0"))
        assert updated["splice_mode"]=="double" and updated["pieces_per_box"]==2


def test_explicit_new_box_type_keeps_existing_applicability_rules(writer_app):
    pid, _, _=seed(writer_app,True)
    with TestClient(writer_app) as client:
        updated,_=save(client,pid,body(client,pid,structure_edit_fields=list(FIELDS),box_style="衬板",
            splice_mode="single",pieces_per_box=1,flap_mm=None,production_process="无需结合"))
        assert updated["box_style"]=="衬板" and updated["flap_mm"] is None
        assert updated["production_process"]=="无需结合"


def test_clear_box_type_confirmation_does_not_clear_known_measurements(writer_app):
    pid, _, _=seed(writer_app,True)
    with TestClient(writer_app) as client:
        updated,_=save(client,pid,body(client,pid,structure_edit_fields=["box_style"],box_style=None))
        assert updated["box_style"] is None
        assert (updated["splice_mode"],updated["pieces_per_box"],updated["flap_mm"],updated["production_process"]) == ("single",1,45,"打钉")


@pytest.mark.parametrize("role,scope", [("workshop","all"),("sales","selected")])
def test_normal_permissions_and_customer_scope_still_guard_corrections(writer_app, role, scope):
    from app.api.deps import get_current_user
    from app.models.user import User
    pid, _, _=seed(writer_app,True)
    with TestClient(writer_app) as client:
        payload=body(client,pid,structure_edit_fields=["flap_mm"],flap_mm=None)
        before=counts(writer_app)
        with writer_app.state.session_factory() as db:
            actor=db.get(User,writer_app.state.admin_id)
            actor.role=role; actor.customer_access_mode=scope; db.commit()
        def scoped_actor():
            with writer_app.state.session_factory() as db:
                actor=db.get(User,writer_app.state.admin_id)
                list(actor.permission_overrides)
                return actor
        writer_app.dependency_overrides[get_current_user]=scoped_actor
        for method,path in ((client.post,f"{URL}/{pid}/update-preview"),(client.put,f"{URL}/{pid}")):
            response=method(path,json=payload)
            assert response.status_code==403,response.text
        with writer_app.state.session_factory() as db:
            product=db.get(Product,pid)
            assert product.version==2 and product.flap_mm==45
        assert counts(writer_app)==before
