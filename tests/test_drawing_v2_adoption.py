"""Explicit task adoption uses disposable SQLite and storage only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import importlib.util

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import drawing_adoption
from app.api.deps import get_current_user, get_db
from app.api.drawing_adoption import DrawingAdoptionWrite, adopt_drawing, get_drawing_adoptions
from app.api.drawing_design import DesignWrite, PublishWrite, publish_design, save_design
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.drawing_design import (DrawingRelease, ProductionTaskDrawing,
                                      ProductionTaskDrawingAdoption)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.user import User
from app.services.drawing_binding import bind_new_task_drawing, bound_task_release
from app.services.secure_uploads import resolve_stored_reference


@pytest.fixture
def adoption_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "files"))
    engine = create_engine(f"sqlite:///{tmp_path / 'adoption.sqlite3'}", connect_args={"timeout": 15})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="采用隔离客户")
        other = Customer(name="其他隔离客户")
        user = User(username="drawing-adoption-admin", password_hash="x", role="admin", real_name="UAT")
        viewer = User(username="drawing-adoption-viewer", password_hash="x", role="sales", real_name="只读")
        db.add_all([customer, other, user, viewer])
        db.flush()
        products = [Product(customer_id=c.id, product_code=f"ADOPT-{i}", customer_material_code=f"ADOPT-{i}",
                           product_name="隔离衬板", length_mm=Decimal("100"), width_mm=Decimal("50"),
                           height_mm=Decimal("3"), flute_type="B", version=1)
                    for i, c in enumerate([customer, other])]
        db.add_all(products)
        db.commit()
        releases = []
        for i, product in enumerate([products[0], products[0], products[1]]):
            version = 1 if i == 1 else None
            save_design(product.id, DesignWrite(expected_product_version=1, expected_design_version=version,
                        template_key="liner_v1", parameters={}), db, user)
            releases.append(publish_design(product.id, PublishWrite(expected_product_version=1,
                            expected_design_version=2 if i == 1 else 1,
                            idempotency_key=f"release-adoption-{i}"), db, user)["id"])
        order = Order(order_number="ADOPTION-UAT", customer_id=customer.id, order_date=date(2026, 9, 22))
        db.add(order)
        db.flush()
        item = OrderItem(order_id=order.id, product_id=products[0].id, quantity=10, unit_price=0,
                         subtotal=0, snapshot_product_name="隔离衬板", snapshot_spec="100×50", snapshot_material="B")
        db.add(item)
        db.flush()
        task = ProductionTask(order_item_id=item.id, version=1, status="waiting_material", planned_quantity=0)
        db.add(task)
        db.flush()
        # Start at the earlier release to model a task created before the update.
        db.add(ProductionTaskDrawing(task_id=task.id, release_id=releases[0]))
        db.commit()
        ids = dict(task=task.id, item=item.id, order=order.id, user=user.id, viewer=viewer.id,
                   product=products[0].id, old=releases[0], new=releases[1], foreign=releases[2])
    yield engine, ids
    engine.dispose()


def request(ids, **changes):
    return DrawingAdoptionWrite(**(dict(release_id=ids["new"], expected_task_version=1,
                                idempotency_key="adoption-request-001", confirmed_not_issued=True) | changes))


def test_adoption_retains_original_and_audit_and_exact_retry(adoption_env):
    engine, ids = adoption_env
    with Session(engine) as db:
        user = db.get(User, ids["user"])
        before = get_drawing_adoptions(ids["task"], db, user)
        assert before["current_release"]["id"] == ids["old"]
        assert {entry["id"] for entry in before["candidates"]} == {ids["old"], ids["new"]}
        assert before["can_adopt"] and before["task_version"] == 1
        first = adopt_drawing(ids["task"], request(ids), db, user)
        assert not first["replayed"] and first["task_version"] == 2
        assert bound_task_release(db, ids["task"]).id == ids["new"]
        assert db.get(ProductionTaskDrawing, ids["task"]).release_id == ids["old"]
        task = db.get(ProductionTask, ids["task"])
        assert task.status == "waiting_material" and task.planned_quantity == 0
        assert db.query(OperationLog).filter(OperationLog.action_code == "production.drawing.adopted").count() == 1
        retry = adopt_drawing(ids["task"], request(ids), db, user)
        assert retry["replayed"] and retry["adoption"]["id"] == first["adoption"]["id"]
        assert db.query(ProductionTaskDrawingAdoption).count() == 1
        with pytest.raises(HTTPException) as changed:
            adopt_drawing(ids["task"], request(ids, release_id=ids["old"]), db, user)
        assert changed.value.status_code == 409


@pytest.mark.parametrize("fault", ["stale", "foreign", "completed", "cancelled", "force_closed"])
def test_rejects_stale_scope_and_invalid_business_state(adoption_env, fault):
    engine, ids = adoption_env
    with Session(engine) as db:
        payload = request(ids)
        if fault == "stale":
            payload.expected_task_version = 2
        elif fault == "foreign":
            payload.release_id = ids["foreign"]
        elif fault == "completed":
            task = db.get(ProductionTask, ids["task"])
            task.status, task.planned_quantity = "completed", 10
        elif fault == "cancelled":
            db.get(Order, ids["order"]).status = "cancelled"
        else:
            db.get(OrderItem, ids["item"]).is_force_closed = True
        db.commit()
        with pytest.raises(HTTPException) as failure:
            adopt_drawing(ids["task"], payload, db, db.get(User, ids["user"]))
        assert failure.value.status_code == (404 if fault == "foreign" else 409)
        assert db.query(ProductionTaskDrawingAdoption).count() == 0
        assert db.get(ProductionTask, ids["task"]).version == 1


@pytest.mark.parametrize("file_kind", ["pdf", "svg"])
def test_corrupt_files_do_not_adopt_or_increment_task(adoption_env, file_kind):
    import json
    engine, ids = adoption_env
    with Session(engine) as db:
        release = db.get(DrawingRelease, ids["new"])
        reference = release.pdf_reference if file_kind == "pdf" else json.loads(release.manifest_json)["svg_snapshots"]["print"]["reference"]
        resolve_stored_reference(reference).write_bytes(b"broken isolated artifact")
        with pytest.raises(HTTPException) as failure:
            adopt_drawing(ids["task"], request(ids), db, db.get(User, ids["user"]))
        assert failure.value.status_code == 503
        assert bound_task_release(db, ids["task"]).id == ids["old"]
        assert db.get(ProductionTask, ids["task"]).version == 1
        assert db.query(ProductionTaskDrawingAdoption).count() == 0


def test_http_permission_and_confirmation(adoption_env):
    engine, ids = adoption_env
    app = FastAPI()
    app.include_router(drawing_adoption.router, prefix="/api/production")
    actor = {"id": ids["viewer"]}
    def session():
        with Session(engine) as db:
            yield db
    def current_user():
        with Session(engine) as db:
            user = db.get(User, actor["id"])
            list(user.permission_overrides)
            db.expunge(user)
            return user
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = current_user
    url = f"/api/production/tasks/{ids['task']}/drawing-adoptions"
    with TestClient(app) as client:
        listing = client.get(url)
        assert listing.status_code == 200 and not listing.json()["can_adopt"]
        assert client.post(url, json=request(ids).model_dump()).status_code == 403
        actor["id"] = ids["user"]
        body = request(ids).model_dump()
        body["confirmed_not_issued"] = False
        assert client.post(url, json=body).status_code == 422
        body.pop("confirmed_not_issued")
        assert client.post(url, json=body).status_code == 422
        assert client.post(url, json=request(ids).model_dump()).status_code == 200


def test_concurrent_same_request_creates_one_event(adoption_env):
    engine, ids = adoption_env
    def run(_):
        with Session(engine) as db:
            try:
                return adopt_drawing(ids["task"], request(ids), db, db.get(User, ids["user"]))
            except HTTPException as error:
                if error.status_code != 409:
                    raise
                return adopt_drawing(ids["task"], request(ids), db, db.get(User, ids["user"]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0]["adoption"]["id"] == results[1]["adoption"]["id"]
    with Session(engine) as db:
        assert db.query(ProductionTaskDrawingAdoption).count() == 1
        assert db.get(ProductionTask, ids["task"]).version == 2


def test_explicit_adoption_can_replace_legacy_for_one_pending_task(adoption_env):
    engine, ids = adoption_env
    with Session(engine) as db:
        product = db.get(Product, ids["product"])
        item = OrderItem(order_id=ids["order"], product_id=product.id, quantity=10, unit_price=0,
                         subtotal=0, snapshot_product_name="隔离例外", snapshot_spec="100×50", snapshot_material="B",
                         drawing_file="private:order_drawings/legacy-exception.pdf")
        db.add(item)
        db.flush()
        task = ProductionTask(order_item_id=item.id, version=1, status="pending", planned_quantity=10)
        db.add(task)
        db.flush()
        bind_new_task_drawing(db, task, product, source_is_new=True)
        db.commit()
        assert bound_task_release(db, task.id) is None
        response = adopt_drawing(task.id, request(ids), db, db.get(User, ids["user"]))
        assert response["adoption"]["old_release_id"] is None
        assert response["adoption"]["legacy_reference"] == item.drawing_file
        assert bound_task_release(db, task.id).id == ids["new"]
        assert task.status == "pending" and task.planned_quantity == 10
        assert bound_task_release(db, ids["task"]).id == ids["old"]


def test_customer_scope_and_disabled_writes_preserve_history(adoption_env, monkeypatch):
    engine, ids = adoption_env
    with Session(engine) as db:
        viewer = db.get(User, ids["viewer"])
        viewer.customer_access_mode = "selected"
        db.commit()
        with pytest.raises(HTTPException) as denied:
            get_drawing_adoptions(ids["task"], db, viewer)
        assert denied.value.status_code == 403
        user = db.get(User, ids["user"])
        adopt_drawing(ids["task"], request(ids), db, user)
        monkeypatch.setenv("ERP_DRAWING_V2_ENABLED", "0")
        listing = get_drawing_adoptions(ids["task"], db, user)
        assert not listing["can_adopt"] and listing["current_release"]["id"] == ids["new"]
        with pytest.raises(HTTPException) as disabled:
            adopt_drawing(ids["task"], request(ids, release_id=ids["old"], expected_task_version=2,
                          idempotency_key="adoption-request-002"), db, user)
        assert disabled.value.status_code == 503
        assert bound_task_release(db, ids["task"]).id == ids["new"]
        assert db.query(ProductionTaskDrawingAdoption).count() == 1


def test_migration_adoption_table_is_immutable(adoption_env):
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    engine, ids = adoption_env
    # Only the new migration's DDL, exercised in this test's disposable DB.
    with engine.begin() as connection:
        for table in ["production_task_drawing_adoptions", "production_task_drawings", "drawing_releases",
                      "drawing_number_sequences", "drawing_designs"]:
            connection.exec_driver_sql(f"DROP TABLE {table}")
        path = Path(__file__).parents[1] / "alembic/versions/rt21v8x9z68_drawing_v2_managed_release.py"
        spec = importlib.util.spec_from_file_location("adoption_test_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        connection.exec_driver_sql("INSERT INTO production_task_drawing_adoptions(task_id,new_release_id,expected_task_version,resulting_task_version,idempotency_key,request_hash,confirmed_not_issued,adopted_by) VALUES (1,1,1,2,'immutable-test','hash',1,1)")
    for statement in ["UPDATE production_task_drawing_adoptions SET new_release_id=2", "DELETE FROM production_task_drawing_adoptions"]:
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(text(statement))
