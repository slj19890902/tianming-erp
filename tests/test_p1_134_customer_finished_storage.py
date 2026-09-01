from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker


@pytest.fixture()
def customer_finished_storage_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    from app.api.customers import router as customers_router
    from app.api.deps import get_current_user, get_db
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.user import User
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryPallet,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseLocation,
    )
    from app.services import location_candidates

    monkeypatch.setenv("ERP_SECRET_KEY", "p1-134-customer-storage-tests")
    engine = create_sqlite_engine(tmp_path / "p1-134-customer-storage.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    published_revision = "p1-134-test-revision"
    map_zones: dict[str, str] = {}
    with factory() as db:
        admin = User(
            username="p1-134-admin",
            password_hash="not-used",
            role="admin",
            real_name="P1-134 Admin",
            display_name="P1-134 Admin",
            must_change_password=False,
            customer_access_mode="all",
        )
        scoped = User(
            username="p1-134-scoped",
            password_hash="not-used",
            role="sales",
            real_name="P1-134 Scoped",
            display_name="P1-134 Scoped",
            must_change_password=False,
            customer_access_mode="selected",
        )
        viewer = User(
            username="p1-134-viewer",
            password_hash="not-used",
            role="finance",
            real_name="P1-134 Viewer",
            display_name="P1-134 Viewer",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(
            customer_number=134,
            customer_code="P1134",
            name="P1-134 测试客户",
            version=1,
        )
        floor = WarehouseFloor(
            floor_code="4F",
            floor_name="四楼",
            floor_number=4,
            construction_status="enabled",
        )
        db.add_all([admin, scoped, viewer, customer, floor])
        db.flush()
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="customers.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="customers.edit",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
            ]
        )

        def add_area(
            code: str,
            *,
            policy_status: str = "published",
            allowed_types: tuple[str, ...] = ("finished",),
            area_status: str = "enabled",
            location_count: int = 1,
            occupied_indexes: tuple[int, ...] = (),
        ) -> WarehouseArea:
            area = WarehouseArea(
                floor_id=floor.id,
                area_code=code,
                area_name=f"{code} 成品区",
                construction_status=area_status,
                planned_location_count=location_count,
            )
            db.add(area)
            db.flush()
            feature_id = f"ZONE-4F-{code}"
            map_zones[feature_id] = code
            db.add(
                WarehouseAreaStoragePolicy(
                    area_id=area.id,
                    map_feature_id=feature_id,
                    allowed_inventory_types_json=json.dumps(list(allowed_types)),
                    storage_layout="rack",
                    status=policy_status,
                    published_map_revision=(
                        published_revision if policy_status == "published" else None
                    ),
                    version=1,
                )
            )
            for index in range(1, location_count + 1):
                location = WarehouseLocation(
                    location_code=f"4F-{code}-L{index:03d}",
                    location_name=f"{code} 货位 {index}",
                    warehouse_type="finished",
                    is_active=True,
                    warehouse_floor=4,
                    area_code=code,
                    storage_type="rack",
                    sort_order=index,
                    is_temporary=False,
                    source_version="CURRENT_MAP",
                    address_kind="legacy",
                    address_area_id=area.id,
                    placement_status="placed",
                )
                db.add(location)
                db.flush()
                db.add(
                    Floor3LocationLayout(
                        location_id=location.id,
                        left_pct=Decimal(str(5 + index * 5)),
                        top_pct=Decimal("10"),
                        width_pct=Decimal("4"),
                        height_pct=Decimal("5"),
                        source_type="manual",
                        layout_kind="physical_rack",
                    )
                )
                if index in occupied_indexes:
                    db.add(
                        InventoryPallet(
                            pallet_code=f"PALLET-{code}-{index}",
                            location_id=location.id,
                            status="active",
                            is_current=True,
                        )
                    )
            return area

        area_a1 = add_area("A1", location_count=2, occupied_indexes=(1,))
        area_a2 = add_area("A2", location_count=1, occupied_indexes=(1,))
        area_a3 = add_area("A3", policy_status="draft")
        area_a4 = add_area("A4", allowed_types=("semi_finished",))
        area_a5 = add_area("A5", area_status="layout_complete")
        db.commit()
        app_ids = {
            "admin": admin.id,
            "scoped": scoped.id,
            "viewer": viewer.id,
            "customer": customer.id,
            "a1": area_a1.id,
            "a2": area_a2.id,
            "a3": area_a3.id,
            "a4": area_a4.id,
            "a5": area_a5.id,
        }

    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda floor_number: (
            {
                "revision": published_revision,
                "zones_by_id": dict(map_zones),
                "zone_ids_by_area": {
                    area_code: [feature_id]
                    for feature_id, area_code in map_zones.items()
                },
            }
            if int(floor_number) == 4
            else None
        ),
    )

    app = FastAPI()
    app.include_router(customers_router, prefix="/api/master/customers")
    app.state.current_user_id = app_ids["admin"]
    app.state.session_factory = factory
    app.state.ids = app_ids

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    def override_current_user() -> User:
        with factory() as db:
            return db.scalar(
                select(User)
                .options(selectinload(User.permission_overrides))
                .where(User.id == app.state.current_user_id)
            )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    return app


def test_customer_finished_storage_preferences_are_ordered_versioned_and_idempotent(
    customer_finished_storage_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog
    from app.models.customer import Customer
    from app.models.customer_finished_storage_preference import (
        CustomerFinishedStoragePreference,
    )
    from app.services.customer_finished_storage import (
        preferred_area_summaries_by_customer_ids,
    )

    app = customer_finished_storage_app
    ids = app.state.ids
    path = f"/api/master/customers/{ids['customer']}/finished-storage-preferences"
    with TestClient(app) as client:
        initial = client.get(path)
        saved = client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a2"], ids["a1"]]},
        )
        repeated = client.put(
            path,
            json={"expected_version": 2, "area_ids": [ids["a2"], ids["a1"]]},
        )

    assert initial.status_code == 200, initial.text
    floor = initial.json()["floors"][0]
    assert floor["floor_code"] == "4F"
    assert [item["area_id"] for item in floor["areas"]] == [ids["a1"], ids["a2"]]
    assert floor["areas"][0]["formal_location_count"] == 2
    assert floor["areas"][0]["available_location_count"] == 1
    assert floor["areas"][1]["formal_location_count"] == 1
    assert floor["areas"][1]["available_location_count"] == 0
    assert saved.status_code == 200, saved.text
    assert saved.json()["selected_area_ids"] == [ids["a2"], ids["a1"]]
    assert saved.json()["customer_version"] == 2
    assert [item["priority"] for item in saved.json()["preferences"]] == [1, 2]
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["customer_version"] == 2

    with app.state.session_factory() as db:
        assert db.get(Customer, ids["customer"]).version == 2
        rows = db.scalars(
            select(CustomerFinishedStoragePreference)
            .where(CustomerFinishedStoragePreference.customer_id == ids["customer"])
            .order_by(CustomerFinishedStoragePreference.priority)
        ).all()
        assert [row.warehouse_area_id for row in rows] == [ids["a2"], ids["a1"]]
        batch = preferred_area_summaries_by_customer_ids(
            db, [ids["customer"], 999999, ids["customer"]]
        )
        assert [item["area_id"] for item in batch[ids["customer"]]] == [
            ids["a2"],
            ids["a1"],
        ]
        assert batch[999999] == []
        assert {
            "area_id",
            "floor_number",
            "floor_name",
            "area_code",
            "area_name",
            "is_valid",
            "available_location_count",
        }.issubset(batch[ids["customer"]][0])
        audits = db.scalars(
            select(OperationLog).where(
                OperationLog.resource == "CustomerFinishedStoragePreference"
            )
        ).all()
        assert len(audits) == 1


def test_customer_finished_storage_rejects_invalid_duplicate_and_stale_writes_atomically(
    customer_finished_storage_app: FastAPI,
) -> None:
    from app.models.customer import Customer
    from app.services.customer_finished_storage import ordered_preferred_area_ids

    app = customer_finished_storage_app
    ids = app.state.ids
    path = f"/api/master/customers/{ids['customer']}/finished-storage-preferences"
    with TestClient(app) as client:
        assert client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a1"]]},
        ).status_code == 200
        invalid = client.put(
            path,
            json={"expected_version": 2, "area_ids": [ids["a3"]]},
        )
        duplicate = client.put(
            path,
            json={"expected_version": 2, "area_ids": [ids["a1"], ids["a1"]]},
        )
        stale = client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a2"]]},
        )

    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "CUSTOMER_FINISHED_STORAGE_AREA_UNAVAILABLE"
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == "CUSTOMER_FINISHED_STORAGE_AREA_DUPLICATE"
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "MASTER_VERSION_CONFLICT"
    with app.state.session_factory() as db:
        assert db.get(Customer, ids["customer"]).version == 2
        assert ordered_preferred_area_ids(db, ids["customer"]) == [ids["a1"]]


def test_customer_finished_storage_uses_shared_customer_version_cas(
    customer_finished_storage_app: FastAPI,
) -> None:
    from app.models.customer import Customer
    from app.models.user import User
    from app.services.customer_finished_storage import ordered_preferred_area_ids
    from app.services.master_data_versioning import apply_versioned_update

    app = customer_finished_storage_app
    ids = app.state.ids
    with app.state.session_factory() as db:
        customer = db.get(Customer, ids["customer"])
        admin = db.get(User, ids["admin"])
        assert customer is not None and admin is not None
        apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates={"remark": "并发更新客户基础资料"},
            expected_version=1,
            user=admin,
            reason="P1-134 聚合版本并发测试",
            source="tests.p1_134",
        )
        db.commit()

    path = f"/api/master/customers/{ids['customer']}/finished-storage-preferences"
    with TestClient(app) as client:
        stale = client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a1"]]},
        )

    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "MASTER_VERSION_CONFLICT"
    with app.state.session_factory() as db:
        assert db.get(Customer, ids["customer"]).version == 2
        assert ordered_preferred_area_ids(db, ids["customer"]) == []


def test_customer_finished_storage_keeps_selected_area_identity_and_reports_later_blocker(
    customer_finished_storage_app: FastAPI,
) -> None:
    from app.models.warehouse_inventory import WarehouseArea, WarehouseAreaStoragePolicy

    app = customer_finished_storage_app
    ids = app.state.ids
    path = f"/api/master/customers/{ids['customer']}/finished-storage-preferences"
    with TestClient(app) as client:
        saved = client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a2"], ids["a1"]]},
        )
        assert saved.status_code == 200, saved.text
        with app.state.session_factory() as db:
            area_a1 = db.get(WarehouseArea, ids["a1"])
            area_a1.area_name = "A1 已改名成品区"
            policy_a2 = db.scalar(
                select(WarehouseAreaStoragePolicy).where(
                    WarehouseAreaStoragePolicy.area_id == ids["a2"]
                )
            )
            policy_a2.status = "draft"
            db.commit()
        refreshed = client.get(path)

    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["selected_area_ids"] == [ids["a2"], ids["a1"]]
    assert body["preferences"][0]["area_id"] == ids["a2"]
    assert body["preferences"][0]["is_valid"] is False
    assert "尚未发布" in "；".join(body["preferences"][0]["blockers"])
    assert body["preferences"][1]["area_id"] == ids["a1"]
    assert "已改名" in body["preferences"][1]["area_master_name"]
    option_ids = [
        area["area_id"]
        for floor in body["floors"]
        for area in floor["areas"]
    ]
    assert option_ids == [ids["a1"]]


def test_customer_finished_storage_honors_customer_scope(
    customer_finished_storage_app: FastAPI,
) -> None:
    app = customer_finished_storage_app
    ids = app.state.ids
    app.state.current_user_id = ids["scoped"]
    path = f"/api/master/customers/{ids['customer']}/finished-storage-preferences"
    with TestClient(app) as client:
        read = client.get(path)
        write = client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a1"]]},
        )
    assert read.status_code == 403
    assert write.status_code == 403


def test_customer_finished_storage_requires_edit_permission_for_put(
    customer_finished_storage_app: FastAPI,
) -> None:
    app = customer_finished_storage_app
    ids = app.state.ids
    app.state.current_user_id = ids["viewer"]
    path = f"/api/master/customers/{ids['customer']}/finished-storage-preferences"
    with TestClient(app) as client:
        read = client.get(path)
        write = client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a1"]]},
        )

    assert read.status_code == 200, read.text
    assert write.status_code == 403


def test_customer_finished_storage_rejects_non_operational_area_variants(
    customer_finished_storage_app: FastAPI,
) -> None:
    from app.models.warehouse_inventory import WarehouseLocation

    app = customer_finished_storage_app
    ids = app.state.ids
    path = f"/api/master/customers/{ids['customer']}/finished-storage-preferences"
    with TestClient(app) as client:
        initial = client.get(path)
        candidate_ids = {
            area["area_id"]
            for floor in initial.json()["floors"]
            for area in floor["areas"]
        }
        assert candidate_ids == {ids["a1"], ids["a2"]}

        for area_key in ("a3", "a4", "a5"):
            rejected = client.put(
                path,
                json={"expected_version": 1, "area_ids": [ids[area_key]]},
            )
            assert rejected.status_code == 409, rejected.text
            assert (
                rejected.json()["detail"]["code"]
                == "CUSTOMER_FINISHED_STORAGE_AREA_UNAVAILABLE"
            )

        with app.state.session_factory() as db:
            locations = db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.address_area_id == ids["a1"]
                )
            ).all()
            assert locations
            for location in locations:
                location.is_active = False
            db.commit()

        no_formal_location = client.put(
            path,
            json={"expected_version": 1, "area_ids": [ids["a1"]]},
        )

    assert no_formal_location.status_code == 409, no_formal_location.text
    assert (
        no_formal_location.json()["detail"]["code"]
        == "CUSTOMER_FINISHED_STORAGE_AREA_UNAVAILABLE"
    )


def test_preference_batch_scans_warehouse_catalog_once_for_many_customers(
    customer_finished_storage_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.customer import Customer
    from app.models.customer_finished_storage_preference import (
        CustomerFinishedStoragePreference,
    )
    from app.services import customer_finished_storage

    app = customer_finished_storage_app
    ids = app.state.ids
    customer_ids = [ids["customer"]]
    with app.state.session_factory() as db:
        db.add(
            CustomerFinishedStoragePreference(
                customer_id=ids["customer"],
                warehouse_area_id=ids["a1"],
                priority=1,
            )
        )
        for offset in range(1, 41):
            customer = Customer(
                customer_number=10000 + offset,
                customer_code=f"P1134-BATCH-{offset}",
                name=f"P1-134 批量客户 {offset}",
                version=1,
            )
            db.add(customer)
            db.flush()
            customer_ids.append(int(customer.id))
            db.add(
                CustomerFinishedStoragePreference(
                    customer_id=int(customer.id),
                    warehouse_area_id=ids["a1"],
                    priority=1,
                )
            )
        db.commit()

    original_catalog = customer_finished_storage._finished_area_catalog
    catalog_calls = 0

    def counted_catalog(db: Session):
        nonlocal catalog_calls
        catalog_calls += 1
        return original_catalog(db)

    monkeypatch.setattr(
        customer_finished_storage,
        "_finished_area_catalog",
        counted_catalog,
    )
    with app.state.session_factory() as db:
        result = customer_finished_storage.preferred_area_summaries_by_customer_ids(
            db,
            customer_ids,
        )

    assert catalog_calls == 1
    assert set(result) == set(customer_ids)
    assert all(
        [summary["area_id"] for summary in rows] == [ids["a1"]]
        for rows in result.values()
    )


def test_preference_batch_short_circuits_when_no_customer_has_preferences(
    customer_finished_storage_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import customer_finished_storage

    app = customer_finished_storage_app
    ids = app.state.ids

    def fail_if_catalog_is_scanned(_db: Session):
        raise AssertionError("empty preference batches must not scan warehouse locations")

    monkeypatch.setattr(
        customer_finished_storage,
        "_finished_area_catalog",
        fail_if_catalog_is_scanned,
    )
    with app.state.session_factory() as db:
        result = customer_finished_storage.preferred_area_summaries_by_customer_ids(
            db,
            [ids["customer"], 999999, ids["customer"]],
        )
    assert result == {ids["customer"]: [], 999999: []}


def test_preferred_finished_area_policy_edit_keeps_finished_use(
    customer_finished_storage_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException, Request

    from app.api import warehouse as warehouse_api
    from app.models.customer_finished_storage_preference import (
        CustomerFinishedStoragePreference,
    )
    from app.models.user import User

    app = customer_finished_storage_app
    ids = app.state.ids
    feature = {
        "id": "ZONE-4F-A1",
        "feature_kind": "zone",
        "erp_area_code": "A1",
        "allowed_inventory_types": ["finished"],
        "storage_layout": "rack",
        "version": 1,
    }
    monkeypatch.setattr(
        warehouse_api,
        "load_effective_warehouse_twin_floor_for_edit",
        lambda _floor_code: {
            "floor_code": "4F",
            "layout_id": "p1-134-policy-test",
            "revision": "p1-134-policy-revision",
            "features": [dict(feature)],
            "pallets": [],
        },
    )
    monkeypatch.setattr(
        warehouse_api,
        "update_warehouse_twin_zone_policy",
        lambda *_args, **_kwargs: SimpleNamespace(
            value={**feature, "area_name": "A1 成品区"},
            floor_revision="p1-134-policy-revision-next",
            applied=False,
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/warehouse/twin-layout/floors/4F/zones/ZONE-4F-A1/storage-policy",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )

    with app.state.session_factory() as db:
        db.add(
            CustomerFinishedStoragePreference(
                customer_id=ids["customer"],
                warehouse_area_id=ids["a1"],
                priority=1,
                created_by=ids["admin"],
            )
        )
        db.commit()
        admin = db.get(User, ids["admin"])
        assert admin is not None

        with pytest.raises(HTTPException) as rejected:
            warehouse_api._update_twin_zone_storage_policy_locked(
                floor_code="4F",
                feature_id="ZONE-4F-A1",
                payload=warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision="p1-134-policy-revision",
                    expected_version=1,
                    operation_key="p1-134-remove-finished",
                    allowed_inventory_types=["semi_finished"],
                    storage_layout="rack",
                    erp_area_code="A1",
                ),
                request=request,
                db=db,
                user=admin,
            )
        assert rejected.value.status_code == 409
        assert "客户默认成品区域" in str(rejected.value.detail)

        allowed = warehouse_api._update_twin_zone_storage_policy_locked(
            floor_code="4F",
            feature_id="ZONE-4F-A1",
            payload=warehouse_api.TwinZoneStoragePolicyPayload(
                expected_revision="p1-134-policy-revision",
                expected_version=1,
                operation_key="p1-134-keep-finished",
                allowed_inventory_types=["finished"],
                storage_layout="rack",
                erp_area_code="A1",
                area_name="A1 成品区",
            ),
            request=request,
            db=db,
            user=admin,
        )

    assert allowed["applied"] is False


def test_publish_blocks_stale_draft_that_removes_finished_from_preferred_area(
    customer_finished_storage_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import warehouse as warehouse_api
    from app.models.customer_finished_storage_preference import (
        CustomerFinishedStoragePreference,
    )

    app = customer_finished_storage_app
    ids = app.state.ids
    draft_feature = {
        "id": "ZONE-4F-A1",
        "feature_kind": "zone",
        "erp_area_code": "A1",
        "allowed_inventory_types": ["semi_finished"],
        "storage_layout": "rack",
    }
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_layout_draft",
        lambda _floor_code: {
            "floor_code": "4F",
            "layout_id": "p1-134-publish-test",
            "features": [dict(draft_feature)],
            "pallets": [],
        },
    )
    with app.state.session_factory() as db:
        db.add(
            CustomerFinishedStoragePreference(
                customer_id=ids["customer"],
                warehouse_area_id=ids["a1"],
                priority=1,
                created_by=ids["admin"],
            )
        )
        db.commit()

        blocked = warehouse_api._formal_area_publish_blockers(db, "4F")
        assert any("客户默认成品区域" in item for item in blocked)

        draft_feature["allowed_inventory_types"] = ["finished"]
        allowed = warehouse_api._formal_area_publish_blockers(db, "4F")

    assert not any("客户默认成品区域" in item for item in allowed)
