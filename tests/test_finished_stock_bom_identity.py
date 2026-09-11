import json
from types import SimpleNamespace

import pytest

from app.models.product import Product
from app.services.finished_stock_identity import product_basis, order_product_basis, matching_component_basis
from app.services.multilevel_bom_orders import read_compiled_order_bom
from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph


@pytest.mark.parametrize("field,value", [
    ("product_id", 999), ("unit", "张"), ("spec", "1×2mm"),
    ("material", "OTHER"), ("flute", "X"), ("production_process", "新增印刷"),
    ("crease_middle_mm", "999"), ("pieces_per_box", "7"),
])
def test_each_physical_mismatch_is_not_eligible(composite_requisition_app, _p181_published_map_identity, field, value):
    _, factory = composite_requisition_app
    seed_graph(factory, separate=True)
    with factory() as db:
        snapshot = next(s for s in read_compiled_order_bom(db, 1).snapshots if s.component_product_id == 2)
        expected = order_product_basis(db, 1, 2)
        assert product_basis(db.get(Product, 2)) == expected
        detail = SimpleNamespace(physical_basis_json=expected)
        lot = SimpleNamespace(finished_detail=detail)
        assert matching_component_basis(db, snapshot, lot)
        changed = json.loads(expected)
        changed[field] = value
        detail.physical_basis_json = json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert not matching_component_basis(db, snapshot, lot)
        detail.physical_basis_json = None
        assert not matching_component_basis(db, snapshot, lot)


def test_master_rename_and_process_edit_do_not_reinterpret_old_stock(composite_requisition_app, _p181_published_map_identity):
    _, factory = composite_requisition_app
    seed_graph(factory, separate=True)
    with factory() as db:
        product = db.get(Product, 2)
        basis = product_basis(product)
        product.product_name = "新名称"
        product.product_code = "新编码"
        assert product_basis(product) == basis
        product.production_process = "新增印刷"
        assert product_basis(product) != basis
        assert order_product_basis(db, 1, 2) == basis


def test_admin_confirmation_is_explicit_atomic_and_replayable(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import select, func
    from app.core.time_contract import beijing_today
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement
    from app.models.user import User
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from tests.test_n039_composite_bom_requisition import _login
    _, factory = composite_requisition_app
    app, _ = composite_requisition_app
    seed_graph(factory, separate=True)
    with factory() as db:
        target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=2)
        lot = manual_finished_in(db, customer_id=1, product_id=2, location_id=target.location.id,
            quantity=20, stock_date=beijing_today(), source_type="manual", remarks="隔离旧批次夹具",
            operator_id=1, idempotency_key="identity-old-lot", expected_layout_version=target.layout_version)
        lot.finished_detail.physical_basis_json = None
        db.commit()
        lid, version = lot.id, lot.version
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        preview = client.get(f"/api/warehouse/lots/{lid}/physical-identity/preview")
        assert preview.status_code == 200, preview.text
        payload = dict(preview_hash=preview.json()["preview_hash"], operation_key="identity-confirm",
                       physical_match_confirmed=True)
        path = f"/api/warehouse/lots/{lid}/physical-identity/confirm"
        refused = client.post(path, json={**payload, "physical_match_confirmed": False})
        assert refused.status_code == 422
        with monkeypatch.context() as patch:
            from app.services import audit_log
            def failed_audit(*args, **kwargs):
                raise RuntimeError("模拟审计失败")
            patch.setattr(audit_log, "append_audit_event", failed_audit)
            failed = client.post(path, json=payload)
            assert failed.status_code == 500, failed.text
        with factory() as db:
            lot = db.get(InventoryLot, lid)
            assert lot.version == version and lot.finished_detail.physical_basis_json is None
            assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 1
        saved = client.post(path, json=payload)
        assert saved.status_code == 200, saved.text
        replay = client.post(path, json=payload)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["movement_id"] == saved.json()["movement_id"]
        changed = client.post(path, json={**payload, "preview_hash": "0" * 64})
        assert changed.status_code == 409
        with factory() as db:
            lot = db.get(InventoryLot, lid)
            assert lot.quantity_available == 20 and lot.quantity_reserved == lot.quantity_consumed == 0
            assert lot.finished_detail.physical_basis_json == order_product_basis(db, 1, 2)
            db.get(User, 1).role = "sales"
            db.commit()
        assert client.post(path, json=payload).status_code == 403


def test_assembled_recipe_is_physical_identity_but_accompany_is_not(composite_requisition_app, _p181_published_map_identity):
    from app.models.user import User
    from tests.test_multilevel_bom_master import save
    _, factory = composite_requisition_app
    seed_graph(factory)
    with factory() as db:
        root = db.get(Product, 1)
        before = product_basis(root)
        assert before == order_product_basis(db, 1, 1)
        save(db, db.get(User, 1), 1, "assembled", [(2, 4, "assembly"), (3, 4, "assembly")])
        assert product_basis(root) != before
        assert order_product_basis(db, 1, 1) == before
