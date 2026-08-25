from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker


def _projection_fixture(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    engine = create_sqlite_engine(tmp_path / "p1-102-projection-lock.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        floor = WarehouseFloor(
            floor_code="1F",
            floor_name="anonymous measured floor",
            floor_number=1,
            construction_status="enabled",
        )
        db.add(floor)
        db.flush()
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="FIN-TEST",
            area_name="anonymous finished area",
            construction_status="enabled",
        )
        location = WarehouseLocation(
            location_code="F1-FIN-TEST-L001",
            location_name="anonymous slot",
            warehouse_type="finished",
            warehouse_floor=1,
            area_code="FIN-TEST",
            storage_type="ground",
            is_active=True,
            placement_status="placed",
            source_version="TWIN_V1",
        )
        location.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("10"),
            top_pct=Decimal("10"),
            width_pct=Decimal("5"),
            height_pct=Decimal("5"),
            version=2,
            source_type="manual",
            layout_kind="physical_pallet",
        )
        db.add_all([area, location])
        db.commit()
        result = int(location.id), int(area.id)
    return engine, factory, result


def test_floor_projection_mutex_serializes_map_and_inventory_writers(tmp_path) -> None:
    from app.services.location_candidates import (
        claim_active_placed_location,
        claim_warehouse_floor_projection,
    )

    engine, factory, (location_id, _area_id) = _projection_fixture(tmp_path)
    try:
        with factory() as map_writer, factory() as inventory_writer:
            assert claim_warehouse_floor_projection(
                map_writer,
                floor_number=1,
            )
            inventory_writer.execute(text("PRAGMA busy_timeout=50"))
            with pytest.raises(OperationalError):
                claim_active_placed_location(
                    inventory_writer,
                    location_id,
                    expected_layout_version=2,
                )
            inventory_writer.rollback()
            map_writer.rollback()
            assert claim_active_placed_location(
                inventory_writer,
                location_id,
                expected_layout_version=2,
            )
            inventory_writer.rollback()
    finally:
        engine.dispose()


def test_inventory_destination_claim_blocks_map_writer_in_reverse_order(tmp_path) -> None:
    from app.services.location_candidates import (
        claim_active_placed_location,
        claim_warehouse_floor_projection,
    )

    engine, factory, (location_id, _area_id) = _projection_fixture(tmp_path)
    try:
        with factory() as inventory_writer, factory() as map_writer:
            assert claim_active_placed_location(
                inventory_writer,
                location_id,
                expected_layout_version=2,
            )
            map_writer.execute(text("PRAGMA busy_timeout=50"))
            with pytest.raises(OperationalError):
                claim_warehouse_floor_projection(
                    map_writer,
                    floor_number=1,
                )
            map_writer.rollback()
            inventory_writer.rollback()
            assert claim_warehouse_floor_projection(
                map_writer,
                floor_number=1,
            )
            map_writer.rollback()
    finally:
        engine.dispose()


def test_publish_failure_restores_runtime_before_releasing_floor_writer(
    tmp_path,
    monkeypatch,
) -> None:
    from fastapi import HTTPException

    from app.api import warehouse as warehouse_api
    from app.services.location_candidates import claim_active_placed_location
    from app.services.warehouse_twin_layout_editor import WarehouseTwinLayoutEditError

    engine, factory, (location_id, _area_id) = _projection_fixture(tmp_path)
    try:
        with factory() as map_writer, factory() as inventory_writer:
            inventory_writer.execute(text("PRAGMA busy_timeout=50"))
            snapshot = object()
            restore_observations: list[tuple[bool, str | None]] = []

            monkeypatch.setattr(
                warehouse_api,
                "load_effective_warehouse_twin_floor_for_edit",
                lambda _floor_code: {},
            )
            monkeypatch.setattr(
                warehouse_api,
                "plan_mold_rack_layout_relocations",
                lambda _db, _layout: [],
            )
            monkeypatch.setattr(
                warehouse_api,
                "_formal_area_publish_blockers",
                lambda _db, _floor_code, **_kwargs: [],
            )
            monkeypatch.setattr(
                warehouse_api,
                "snapshot_warehouse_twin_publish_state",
                lambda: snapshot,
            )

            def fail_after_runtime_publish(*_args, **_kwargs):
                raise WarehouseTwinLayoutEditError("injected publish failure")

            def restore_while_writer_is_held(
                restored_snapshot,
                *,
                backup_name=None,
            ) -> None:
                assert restored_snapshot is snapshot
                assert map_writer.in_transaction()
                try:
                    with pytest.raises(OperationalError):
                        claim_active_placed_location(
                            inventory_writer,
                            location_id,
                            expected_layout_version=2,
                        )
                finally:
                    inventory_writer.rollback()
                restore_observations.append((map_writer.in_transaction(), backup_name))

            monkeypatch.setattr(
                warehouse_api,
                "publish_warehouse_twin_layout_draft",
                fail_after_runtime_publish,
            )
            monkeypatch.setattr(
                warehouse_api,
                "restore_warehouse_twin_publish_state",
                restore_while_writer_is_held,
            )

            payload = warehouse_api.TwinLayoutDraftPublishPayload(
                expected_published_revision="published-v1",
                expected_draft_revision="draft-v2",
                operation_key="p1-102-restore-order",
            )
            with pytest.raises(HTTPException) as caught:
                warehouse_api._publish_twin_layout_draft_locked(
                    floor_code="1F",
                    payload=payload,
                    request=None,
                    db=map_writer,
                    user=SimpleNamespace(id=1),
                )

            assert caught.value.status_code == 422
            assert restore_observations == [(True, None)]
            assert not map_writer.in_transaction()
            assert claim_active_placed_location(
                inventory_writer,
                location_id,
                expected_layout_version=2,
            )
            inventory_writer.rollback()
    finally:
        engine.dispose()


def test_location_claim_does_not_expire_unrelated_dirty_business_rows(tmp_path) -> None:
    from app.models.warehouse_inventory import WarehouseArea
    from app.services.location_candidates import claim_active_placed_location

    engine, factory, (location_id, area_id) = _projection_fixture(tmp_path)
    try:
        with factory(autoflush=False) as db:
            area = db.get(WarehouseArea, area_id)
            assert area is not None
            area.area_name = "pending business edit"
            assert claim_active_placed_location(
                db,
                location_id,
                expected_layout_version=2,
            )
            assert area.area_name == "pending business edit"
            assert area in db.dirty
            db.rollback()
    finally:
        engine.dispose()
