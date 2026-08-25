from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
)
from scripts.admin import repair_p1_102_formal_projection_residues as repair


def _runtime_map(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "floors": {
                    "1F": {"revision": repair.EXPECTED_RUNTIME_REVISIONS["1F"]},
                    "3F": {"revision": repair.EXPECTED_RUNTIME_REVISIONS["3F"]},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _seed_database(path: Path) -> None:
    engine = create_sqlite_engine(path)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
            {"revision": repair.EXPECTED_ALEMBIC_REVISION},
        )
    with factory() as db:
        actor = User(
            id=1,
            username="repair-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="匿名管理员",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
        )
        customers = [
            Customer(id=5, name="匿名客户甲", customer_code="ANON-5"),
            Customer(id=12, name="匿名客户乙", customer_code="ANON-12"),
        ]
        floor1 = WarehouseFloor(
            id=1,
            floor_code="1F",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
        )
        floor3 = WarehouseFloor(
            id=3,
            floor_code="3F",
            floor_name="三楼",
            floor_number=3,
            construction_status="enabled",
        )
        db.add_all([actor, *customers, floor1, floor3])
        db.flush()
        products = [
            Product(
                id=60,
                customer_id=5,
                product_code="P60",
                customer_material_code="P60",
                product_name="匿名60",
            ),
            Product(
                id=236,
                customer_id=5,
                product_code="P236",
                customer_material_code="P236",
                product_name="匿名236",
            ),
            Product(
                id=2694,
                customer_id=5,
                product_code="P2694",
                customer_material_code="P2694",
                product_name="匿名2694",
            ),
            Product(
                id=679,
                customer_id=12,
                product_code="P679",
                customer_material_code="P679",
                product_name="匿名679",
            ),
        ]
        areas = [
            WarehouseArea(
                id=7,
                floor_id=3,
                area_code="C1",
                area_name="匿名C1",
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=1,
                capacity_reviewed_by="匿名复核",
                capacity_reviewed_at=date(2026, 8, 25),
            ),
            WarehouseArea(
                id=1,
                floor_id=3,
                area_code="A1",
                area_name="匿名A1",
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=1,
                capacity_reviewed_by="匿名复核",
                capacity_reviewed_at=date(2026, 8, 25),
            ),
            WarehouseArea(
                id=31,
                floor_id=1,
                area_code="FIN-001",
                area_name="匿名FIN",
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=2,
                capacity_reviewed_by="匿名复核",
                capacity_reviewed_at=date(2026, 8, 25),
            ),
        ]
        db.add_all([*products, *areas])
        db.flush()
        policy = WarehouseAreaStoragePolicy(
            id=6,
            area_id=31,
            map_feature_id="FIN-ZONE",
            allowed_inventory_types_json='["finished"]',
            storage_layout="pallet_ground",
            status="published",
            published_map_revision=repair.EXPECTED_RUNTIME_REVISIONS["1F"],
            version=19,
            updated_by=actor.id,
        )
        db.add(policy)
        locations = [
            WarehouseLocation(
                id=400,
                location_code="C1-R13",
                location_name="匿名C1位置",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="C1",
                storage_type="ground",
                placement_status="placed",
                source_version="V11",
            ),
            WarehouseLocation(
                id=429,
                location_code="F1-FIN-001-L007",
                location_name="匿名FIN7",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=1,
                area_code="FIN-001",
                storage_type="ground",
                placement_status="placed",
                source_version="TWIN_V1",
            ),
            WarehouseLocation(
                id=431,
                location_code="F1-FIN-001-L009",
                location_name="匿名FIN9",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=1,
                area_code="FIN-001",
                storage_type="ground",
                placement_status="placed",
                source_version="TWIN_V1",
            ),
            WarehouseLocation(
                id=6,
                location_code="A1-L05",
                location_name="匿名A1位置",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                placement_status="placed",
                source_version="V11",
            ),
        ]
        db.add_all(locations)
        db.flush()
        db.add_all(
            [
                Floor3LocationLayout(
                    id=398,
                    location_id=400,
                    left_pct=Decimal("10"),
                    top_pct=Decimal("10"),
                    width_pct=Decimal("5"),
                    height_pct=Decimal("5"),
                    version=1,
                    source_type="seeded",
                    layout_kind="unknown",
                ),
                Floor3LocationLayout(
                    id=426,
                    location_id=429,
                    left_pct=Decimal("20"),
                    top_pct=Decimal("10"),
                    width_pct=Decimal("5"),
                    height_pct=Decimal("5"),
                    version=1,
                    source_type="manual",
                    layout_kind="physical_pallet",
                ),
                Floor3LocationLayout(
                    id=428,
                    location_id=431,
                    left_pct=Decimal("30"),
                    top_pct=Decimal("10"),
                    width_pct=Decimal("5"),
                    height_pct=Decimal("5"),
                    version=1,
                    source_type="manual",
                    layout_kind="physical_pallet",
                ),
                Floor3LocationLayout(
                    id=5,
                    location_id=6,
                    left_pct=Decimal("40"),
                    top_pct=Decimal("10"),
                    width_pct=Decimal("5"),
                    height_pct=Decimal("5"),
                    version=1,
                    source_type="seeded",
                    layout_kind="unknown",
                ),
            ]
        )
        plan = WarehouseGroundLayoutPlan(
            id=1,
            area_id=31,
            status="published",
            target_slot_count=2,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision=repair.EXPECTED_RUNTIME_REVISIONS["1F"],
            published_map_revision=repair.EXPECTED_RUNTIME_REVISIONS["1F"],
            preview_fingerprint="a" * 64,
            version=2,
            publish_idempotency_key="anonymous-fin-publish",
            publish_request_hash="b" * 64,
            updated_by=actor.id,
            published_by=actor.id,
            published_at=datetime.now(),
        )
        db.add(plan)
        db.flush()
        db.add_all(
            [
                WarehouseGroundLayoutSlot(
                    id=7,
                    plan_id=1,
                    location_id=429,
                    route_sequence=1,
                    row_no=1,
                    slot_no=7,
                    x_mm=Decimal("100"),
                    y_mm=Decimal("100"),
                    width_mm=1200,
                    depth_mm=1000,
                ),
                WarehouseGroundLayoutSlot(
                    id=9,
                    plan_id=1,
                    location_id=431,
                    route_sequence=2,
                    row_no=1,
                    slot_no=9,
                    x_mm=Decimal("1400"),
                    y_mm=Decimal("100"),
                    width_mm=1200,
                    depth_mm=1000,
                ),
            ]
        )
        lots = [
            InventoryLot(
                id=42,
                lot_number="ANON-C1-42",
                inventory_type="finished",
                warehouse_location_id=400,
                quantity_available=20,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 8, 25),
                last_movement_at=datetime.now(),
                version=1,
            ),
            InventoryLot(
                id=217,
                lot_number="ANON-FIN-217",
                inventory_type="finished",
                warehouse_location_id=429,
                quantity_available=0,
                quantity_reserved=12,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="production_completion",
                stock_date=date(2026, 8, 25),
                last_movement_at=datetime.now(),
                version=3,
            ),
            InventoryLot(
                id=202,
                lot_number="ANON-FIN-202",
                inventory_type="finished",
                warehouse_location_id=431,
                quantity_available=0,
                quantity_reserved=200,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="production_completion",
                stock_date=date(2026, 8, 25),
                last_movement_at=datetime.now(),
                version=3,
            ),
            InventoryLot(
                id=186,
                lot_number="ANON-A1-186",
                inventory_type="finished",
                warehouse_location_id=6,
                quantity_available=0,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="delivery_return",
                stock_date=date(2026, 8, 25),
                last_movement_at=datetime.now(),
                version=14,
            ),
        ]
        db.add_all(lots)
        db.flush()
        for lot_id, customer_id, product_id in (
            (42, 5, 60),
            (217, 5, 236),
            (202, 5, 2694),
            (186, 12, 679),
        ):
            db.add(
                FinishedGoodsInventoryDetail(
                    inventory_lot_id=lot_id,
                    owner_customer_id=customer_id,
                    owner_customer_name_snapshot=f"匿名{customer_id}",
                    is_general=False,
                    product_id=product_id,
                    inventory_code_snapshot=f"ANON-{product_id}",
                    product_name_snapshot=f"匿名产品{product_id}",
                )
            )
        pallets = [
            InventoryPallet(
                id=159,
                pallet_code="ANON-FIN-159",
                location_id=429,
                location_occupancy_key="PRODUCTION_COMPLETION:87",
                status="active",
                is_current=True,
                needs_relocation=False,
                version=3,
                created_by=1,
                updated_by=1,
            ),
            InventoryPallet(
                id=155,
                pallet_code="ANON-FIN-155",
                location_id=431,
                location_occupancy_key="PRODUCTION_COMPLETION:83",
                status="active",
                is_current=True,
                needs_relocation=False,
                version=3,
                created_by=1,
                updated_by=1,
            ),
            InventoryPallet(
                id=148,
                pallet_code="ANON-A1-148",
                location_id=6,
                location_occupancy_key="PRIMARY",
                status="active",
                is_current=True,
                needs_relocation=False,
                version=1,
                created_by=1,
                updated_by=1,
            ),
        ]
        db.add_all(pallets)
        db.flush()
        db.add_all(
            [
                InventoryPalletItem(
                    id=247,
                    pallet_id=159,
                    inventory_lot_id=217,
                    customer_id=5,
                    product_id=236,
                    inventory_code="ANON-236",
                    product_name="匿名236",
                    item_type="finished",
                    quantity=12,
                    unit="boxes",
                    match_status="matched",
                    created_by=1,
                ),
                InventoryPalletItem(
                    id=245,
                    pallet_id=155,
                    inventory_lot_id=202,
                    customer_id=5,
                    product_id=2694,
                    inventory_code="ANON-2694",
                    product_name="匿名2694",
                    item_type="finished",
                    quantity=200,
                    unit="boxes",
                    match_status="matched",
                    created_by=1,
                ),
                InventoryPalletItem(
                    id=215,
                    pallet_id=148,
                    inventory_lot_id=186,
                    customer_id=12,
                    product_id=679,
                    inventory_code="ANON-679",
                    product_name="匿名679",
                    item_type="finished",
                    quantity=6,
                    unit="boxes",
                    match_status="matched",
                    created_by=1,
                ),
            ]
        )
        db.commit()
    engine.dispose()


@pytest.fixture()
def repair_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "repair.sqlite3"
    runtime = _runtime_map(tmp_path / "runtime.json")
    _seed_database(database)
    targets = deepcopy(repair.TARGETS)
    targets["area_counts"] = {
        "C1": {
            "capacity": 1,
            "positive_locations": 1,
            "current_before": 0,
            "current_after": 1,
        },
        "A1": {
            "capacity": 1,
            "positive_locations": 0,
            "current_before": 1,
            "current_after": 0,
        },
        "FIN-001": {"current_before": 2, "current_after": 2},
    }
    monkeypatch.setattr(repair, "TARGETS", targets)
    monkeypatch.setattr(repair, "FORMAL_RUNTIME_SHA256", repair.sha256_file(runtime))
    return database, runtime


def _snapshot_evidence(
    *,
    database: Path,
    runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, dict, Path]:
    plan = repair.inspect_plan(database, runtime)
    monkeypatch.setattr(repair, "FORMAL_DATABASE", database)
    snapshot = repair.create_snapshot(
        database=database,
        runtime_map=runtime,
        backup_dir=tmp_path / "snapshot-backups",
        expected_plan_sha256=plan["plan_sha256"],
    )
    evidence = tmp_path / "snapshot-evidence.json"
    evidence.write_text(json.dumps(snapshot), encoding="utf-8")
    return plan, snapshot, evidence


def test_dry_run_apply_and_replay_preserve_inventory_facts(
    repair_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime = repair_database
    before, snapshot, snapshot_evidence = _snapshot_evidence(
        database=database,
        runtime=runtime,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    source_snapshot_sha256 = snapshot["backup"]["sha256"]
    rehearsal = tmp_path / "rehearsal.sqlite3"
    shutil.copy2(snapshot["backup"]["path"], rehearsal)
    assert before["status"] == "ready"
    assert before["total_changes_before"] == before["total_changes_after"] == 0
    result = repair.apply_repair(
        database=rehearsal,
        runtime_map=runtime,
        database_role="isolated-rehearsal",
        operator_user_id=1,
        expected_plan_sha256=before["plan_sha256"],
        authorization=repair.AUTHORIZATION,
        snapshot_evidence=snapshot_evidence,
        source_snapshot_sha256=source_snapshot_sha256,
    )
    assert result["status"] == "verified"
    assert result["projection_complete"] is True
    assert {
        table: result["row_deltas"][table] for table in repair.ALLOWED_DELTAS
    } == repair.ALLOWED_DELTAS
    assert all(
        result["row_deltas"].get(table, 0) == 0
        for table in repair.PROTECTED_COUNT_TABLES
    )
    replay = repair.apply_repair(
        database=rehearsal,
        runtime_map=runtime,
        database_role="isolated-rehearsal",
        operator_user_id=1,
        expected_plan_sha256=before["plan_sha256"],
        authorization=repair.AUTHORIZATION,
        snapshot_evidence=snapshot_evidence,
        source_snapshot_sha256=source_snapshot_sha256,
    )
    assert replay["status"] == "already_applied"
    assert replay["changed"] is False


def test_target_drift_refuses_without_writes(repair_database) -> None:
    database, runtime = repair_database
    engine = create_sqlite_engine(database)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE inventory_lots SET quantity_available=21 WHERE id=42")
        )
    counts_before = repair.database_checks(database)
    with pytest.raises(repair.RepairRefused, match="protected lot contract"):
        repair.inspect_plan(database, runtime)
    assert repair.database_checks(database) == counts_before
    engine.dispose()


def test_mid_repair_failure_rolls_back_every_projection_write(
    repair_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime = repair_database
    plan, snapshot, snapshot_evidence = _snapshot_evidence(
        database=database,
        runtime=runtime,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    source_snapshot_sha256 = snapshot["backup"]["sha256"]
    rehearsal = tmp_path / "rehearsal.sqlite3"
    shutil.copy2(snapshot["backup"]["path"], rehearsal)
    original = repair._insert_fin_occupancy
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(repair, "_insert_fin_occupancy", fail_second)
    with pytest.raises(RuntimeError, match="injected failure"):
        repair.apply_repair(
            database=rehearsal,
            runtime_map=runtime,
            database_role="isolated-rehearsal",
            operator_user_id=1,
            expected_plan_sha256=plan["plan_sha256"],
            authorization=repair.AUTHORIZATION,
            snapshot_evidence=snapshot_evidence,
            source_snapshot_sha256=source_snapshot_sha256,
        )
    assert repair.inspect_plan(rehearsal, runtime)["status"] == "ready"


def test_report_path_and_formal_hardlink_cannot_bypass_safety(
    repair_database, tmp_path: Path
) -> None:
    database, runtime = repair_database
    sha_before = repair.sha256_file(database)
    with pytest.raises(repair.RepairRefused, match="report path conflicts"):
        repair.main(
            [
                "--database",
                str(database),
                "--runtime-map",
                str(runtime),
                "--report",
                str(database),
            ]
        )
    assert repair.sha256_file(database) == sha_before

    report = tmp_path / "report.json"
    report_tmp = report.with_suffix(report.suffix + ".tmp")
    report_tmp.hardlink_to(database)
    with pytest.raises(repair.RepairRefused, match="report path conflicts"):
        repair.main(
            [
                "--database",
                str(database),
                "--runtime-map",
                str(runtime),
                "--report",
                str(report),
            ]
        )
    assert repair.sha256_file(database) == sha_before

    hardlink = tmp_path / "formal-hardlink.sqlite3"
    hardlink.hardlink_to(database)
    original_formal = repair.FORMAL_DATABASE
    try:
        repair.FORMAL_DATABASE = database
        with pytest.raises(repair.RepairRefused, match="cannot target the formal"):
            repair._require_database_role(hardlink, "isolated-rehearsal")
    finally:
        repair.FORMAL_DATABASE = original_formal


def test_snapshot_uses_verified_sqlite_backup_under_formal_role(
    repair_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime = repair_database
    plan = repair.inspect_plan(database, runtime)
    monkeypatch.setattr(repair, "FORMAL_DATABASE", database)
    result = repair.create_snapshot(
        database=database,
        runtime_map=runtime,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=plan["plan_sha256"],
    )
    backup = Path(result["backup"]["path"])
    assert result["status"] == "snapshot_verified"
    assert backup.is_file()
    assert "FINAL_P1_102" in backup.name
    assert repair.database_checks(backup)["integrity_check"] == "ok"
    assert repair.inspect_plan(database, runtime)["status"] == "ready"


def test_rehearsal_evidence_must_bind_verified_snapshot_sha(
    repair_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime = repair_database
    plan, snapshot, snapshot_path = _snapshot_evidence(
        database=database,
        runtime=runtime,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    plan_sha = plan["plan_sha256"]
    rehearsal_path = tmp_path / "rehearsal.json"
    rehearsal = {
        "database_role": "isolated-rehearsal",
        "status": "verified",
        "plan_sha256": plan_sha,
        "script_sha256": repair.script_sha256(),
        "replay_verified": True,
        "projection_complete": True,
        "source_snapshot_sha256": "b" * 64,
    }
    rehearsal_path.write_text(json.dumps(rehearsal), encoding="utf-8")
    with pytest.raises(repair.RepairRefused, match="not bound"):
        repair._load_rehearsal_evidence(
            rehearsal_path,
            snapshot_path,
            plan_sha,
            runtime,
        )
    rehearsal["source_snapshot_sha256"] = snapshot["backup"]["sha256"]
    rehearsal_path.write_text(json.dumps(rehearsal), encoding="utf-8")
    assert (
        repair._load_rehearsal_evidence(
            rehearsal_path,
            snapshot_path,
            plan_sha,
            runtime,
        )["source_snapshot_sha256"]
        == snapshot["backup"]["sha256"]
    )


def test_rehearsal_must_use_independent_verified_snapshot_copy(
    repair_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime = repair_database
    plan, snapshot, snapshot_evidence = _snapshot_evidence(
        database=database,
        runtime=runtime,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    backup = Path(snapshot["backup"]["path"])
    kwargs = {
        "runtime_map": runtime,
        "database_role": "isolated-rehearsal",
        "operator_user_id": 1,
        "expected_plan_sha256": plan["plan_sha256"],
        "authorization": repair.AUTHORIZATION,
        "snapshot_evidence": snapshot_evidence,
        "source_snapshot_sha256": snapshot["backup"]["sha256"],
    }
    with pytest.raises(repair.RepairRefused, match="independent copy"):
        repair.apply_repair(database=backup, **kwargs)

    hardlink = tmp_path / "snapshot-hardlink.sqlite3"
    hardlink.hardlink_to(backup)
    with pytest.raises(repair.RepairRefused, match="independent copy"):
        repair.apply_repair(database=hardlink, **kwargs)

    rehearsal = tmp_path / "rehearsal-copy.sqlite3"
    shutil.copy2(backup, rehearsal)
    result = repair.apply_repair(database=rehearsal, **kwargs)
    assert result["status"] == "verified"
    assert result["replay_verified"] is True


def test_full_snapshot_rehearsal_and_formal_chain_is_verified_and_idempotent(
    repair_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime = repair_database
    plan, snapshot, snapshot_evidence = _snapshot_evidence(
        database=database,
        runtime=runtime,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    rehearsal_database = tmp_path / "rehearsal-chain.sqlite3"
    shutil.copy2(snapshot["backup"]["path"], rehearsal_database)
    rehearsal = repair.apply_repair(
        database=rehearsal_database,
        runtime_map=runtime,
        database_role="isolated-rehearsal",
        operator_user_id=1,
        expected_plan_sha256=plan["plan_sha256"],
        authorization=repair.AUTHORIZATION,
        snapshot_evidence=snapshot_evidence,
        source_snapshot_sha256=snapshot["backup"]["sha256"],
    )
    rehearsal_evidence = tmp_path / "rehearsal-evidence.json"
    rehearsal_evidence.write_text(json.dumps(rehearsal), encoding="utf-8")

    formal = repair.apply_repair(
        database=database,
        runtime_map=runtime,
        database_role="formal",
        operator_user_id=1,
        expected_plan_sha256=plan["plan_sha256"],
        authorization=repair.AUTHORIZATION,
        backup_dir=tmp_path / "formal-recovery-backups",
        snapshot_evidence=snapshot_evidence,
        rehearsal_evidence=rehearsal_evidence,
    )
    assert formal["status"] == "verified"
    assert formal["changed"] is True
    assert formal["row_deltas"] == {
        **{table: 0 for table in repair.PROTECTED_COUNT_TABLES},
        **repair.ALLOWED_DELTAS,
    }
    assert formal["protected_inventory_signature"] == rehearsal[
        "protected_inventory_signature"
    ]
    recovery_backup = Path(formal["backup"]["path"])
    assert recovery_backup.is_file()
    assert not repair._same_file(recovery_backup, Path(snapshot["backup"]["path"]))
    assert formal["backup"]["sha256"] == snapshot["backup"]["sha256"]
    assert repair.inspect_plan(database, runtime)["status"] == "already_applied"

    sha_before_replay = repair.sha256_file(database)
    replay = repair.apply_repair(
        database=database,
        runtime_map=runtime,
        database_role="formal",
        operator_user_id=1,
        expected_plan_sha256=plan["plan_sha256"],
        authorization=repair.AUTHORIZATION,
        backup_dir=tmp_path / "formal-recovery-backups",
        snapshot_evidence=snapshot_evidence,
        rehearsal_evidence=rehearsal_evidence,
    )
    assert replay["status"] == "already_applied"
    assert replay["changed"] is False
    assert repair.sha256_file(database) == sha_before_replay

    engine = create_sqlite_engine(database)
    with engine.connect() as connection:
        audit_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM operation_logs "
                "WHERE action_code=:action_code AND request_id=:request_id"
            ),
            {
                "action_code": repair.ACTION_CODE,
                "request_id": plan["plan_sha256"],
            },
        ).scalar_one()
    engine.dispose()
    assert audit_count == 1


def test_backup_result_must_match_reopened_file_checks() -> None:
    plan_sha = "d" * 64
    backup = SimpleNamespace(integrity_check="ok", sha256="a" * 64, size=123)
    checks = {
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "alembic_revisions": [repair.EXPECTED_ALEMBIC_REVISION],
        "sha256": "b" * 64,
        "size": 123,
    }
    with pytest.raises(repair.RepairRefused, match="does not match"):
        repair._assert_verified_backup(
            backup=backup,
            checks=checks,
            plan={"status": "ready", "plan_sha256": plan_sha},
            expected_plan_sha256=plan_sha,
        )
