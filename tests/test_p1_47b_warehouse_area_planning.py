from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from starlette.requests import Request
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api import warehouse as warehouse_api
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.mold_tool import MoldLocationMovement, MoldTool
from app.models.order import Order, OrderItem
from app.models.printing_plate import PrintingPlate
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
    InventoryLot,
    InventoryPallet,
)
from app.services import warehouse_twin_layout_editor as editor
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from app.services.warehouse_location_address import location_address_payload
from app.services.location_candidates import warehouse_location_projection
from app.services.warehouse_twin_dashboard import (
    _location_payload as warehouse_twin_location_payload,
)
from app.services.warehouse_twin_production import WarehouseTwinProductionError
from app.services.warehouse_twin_layout_editor import (
    WarehouseTwinLayoutEditConflictError,
    WarehouseTwinLayoutEditError,
    _floor_revision,
    load_effective_warehouse_twin_floor_for_edit,
    update_warehouse_twin_zone_geometry,
    validate_warehouse_twin_layout_draft,
)


ROOT = Path(__file__).resolve().parents[1]
TWIN_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")


def _published_layout(path: Path, *, locked: bool = False) -> Path:
    floor = {
        "layout_id": "layout-3f-p1-47b",
        "floor_code": "3F",
        "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 10_000, "max_y": 10_000},
        "features": [
            {
                "id": "zone-f1",
                "feature_code": "ZONE-3F-ERP-F1",
                "name": "F1",
                "feature_kind": "zone",
                "subtype": "rack_storage",
                "points": [[0, 0], [10_000, 0], [10_000, 10_000], [0, 10_000]],
                "area_mm2": 100_000_000,
                "version": 1,
                "is_locked": locked,
                "erp_area_code": "F1",
            }
        ],
        "racks": [],
        "pallets": [],
    }
    floor["revision"] = _floor_revision(floor)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "generated_at": "old", "floors": {"3F": floor}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _add_floor_one_to_published_layout(path: Path) -> None:
    document = json.loads(path.read_text(encoding='utf-8'))
    floor = {
        'layout_id': 'layout-1f-p1-47b',
        'floor_code': '1F',
        'bounds_mm': {'min_x': 0, 'min_y': 0, 'max_x': 8_000, 'max_y': 8_000},
        'features': [
            {
                'id': 'zone-1f',
                'feature_code': 'ZONE-1F-TEST-001',
                'name': '1F 测试区',
                'feature_kind': 'zone',
                'subtype': 'rack_storage',
                'points': [[0, 0], [8_000, 0], [8_000, 8_000], [0, 8_000]],
                'area_mm2': 64_000_000,
                'version': 1,
                'is_locked': False,
            }
        ],
        'racks': [],
        'pallets': [],
    }
    floor['revision'] = _floor_revision(floor)
    document['floors']['1F'] = floor
    path.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )


def _isolate_layout_paths(tmp_path: Path, monkeypatch, *, locked: bool = False) -> tuple[Path, Path]:
    published = _published_layout(tmp_path / "published.json", locked=locked)
    draft = tmp_path / "runtime" / "layout.draft.json"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BASELINE_PATH", published)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", tmp_path / "runtime" / "published.json")
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BACKUP_DIR", tmp_path / "backups")
    return published, draft


def _revision(path: Path, floor_code: str = "3F") -> str:
    return str(
        json.loads(path.read_text(encoding="utf-8"))["floors"][floor_code]["revision"]
    )


def _backup_manifest(path: Path) -> dict[str, bytes]:
    if not path.exists():
        return {}
    return {item.name: item.read_bytes() for item in path.glob('*.json')}


def _request() -> Request:
    return Request(
        {
            'type': 'http',
            'method': 'PATCH',
            'path': '/api/warehouse/twin-layout/floors/3F/zones/zone-f1',
            'headers': [],
            'client': ('testclient', 50000),
        }
    )


def _rack_values(*, levels: int, level_cell_counts: list[int]) -> dict:
    return {
        'name': 'R01 左架',
        'x_mm': 1_000,
        'y_mm': 1_000,
        'width_mm': 2_000,
        'depth_mm': 800,
        'height_mm': 3_000,
        'levels': levels,
        'level_heights_mm': [1_500] if levels == 2 else [1_000, 2_000] if levels == 3 else [],
        'cargo_rows': 3,
        'level_cell_counts': level_cell_counts,
        'bays': 1,
        'rotation_deg': 0,
        'access_side': 'south',
        'min_aisle_width_mm': 0,
        'mold_rack_code': 'R01',
    }


def _confirm_area_payload(
    *,
    revision: str,
    published_revision: str | None = None,
    operation_key: str,
    usage: str = 'finished',
    storage_layout: str = 'pallet_ground',
    capacity: int = 12,
    area_code: str = 'F1',
    area_name: str = '三楼成品区',
    existing_area_id: int | None = None,
    expected_version: int = 1,
):
    return warehouse_api.TwinZoneConfirmAreaPayload(
        expected_revision=revision,
        expected_published_revision=published_revision or revision,
        expected_version=expected_version,
        operation_key=operation_key,
        primary_inventory_type=usage,
        storage_layout=storage_layout,
        max_pallet_capacity=capacity,
        erp_area_code=area_code,
        area_name=area_name,
        existing_area_id=existing_area_id,
        confirmed=True,
    )


def _database(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / 'p1-47b.sqlite3')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username='p1-47b-admin',
                    password_hash='test-only',
                    role='admin',
                    real_name='P1-47B 管理员',
                    is_active=True,
                    must_change_password=False,
                    customer_access_mode='all',
                    ui_mode='standard',
                ),
                WarehouseFloor(
                    floor_code='3F',
                    floor_name='三楼',
                    floor_number=3,
                    construction_status='enabled',
                    planning_reference_pallet_capacity=0,
                ),
            ]
        )
        db.commit()
    return engine, factory


def _add_production_task(db, *, status: str) -> ProductionTask:
    customer = Customer(
        name=f'P1-47B production {status}',
        payment_term_days=0,
        credit_limit=Decimal('0'),
    )
    db.add(customer)
    db.flush()
    product = Product(
        customer_id=customer.id,
        product_code=f'P147B-{status}',
        customer_material_code=f'P147B-{status}',
        product_name=f'P1-47B {status} product',
    )
    db.add(product)
    db.flush()
    order = Order(
        order_number=f'P1-47B-{status}',
        customer_id=customer.id,
        order_date=date(2026, 8, 12),
        status='pending_production',
        payment_status='unpaid',
        total_amount=Decimal('0'),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_order_number=f'P1-47B-{status}-001',
        quantity=100,
        delivered_quantity=0,
        is_force_closed=False,
        unit_price=Decimal('0'),
        subtotal=Decimal('0'),
        material_status='pending',
        snapshot_product_name=f'P1-47B {status} product',
        special_process='无',
    )
    db.add(item)
    db.flush()
    task = ProductionTask(
        order_item_id=item.id,
        status=status,
        planned_quantity=100,
        ordered_quantity_snapshot=100,
        material_input_quantity=100,
        output_factor=1,
        printing_plate_mode_snapshot='no_plate',
        version=1,
    )
    db.add(task)
    db.flush()
    return task


def _policy_payload(*, revision: str, version: int, operation_key: str):
    return warehouse_api.TwinZoneStoragePolicyPayload(
        expected_revision=revision,
        expected_version=version,
        operation_key=operation_key,
        allowed_inventory_types=['finished'],
        storage_layout='pallet_ground',
        erp_area_code='F1',
        area_name='三楼成品区',
    )


def _geometry_payload(*, revision: str, version: int, operation_key: str):
    return warehouse_api.TwinZoneGeometryPayload(
        expected_revision=revision,
        expected_version=version,
        operation_key=operation_key,
        points=[(500, 500), (9_500, 500), (9_500, 7_500), (500, 7_500)],
    )


def _bind_formal_area(
    db,
    *,
    admin: User,
    area_code: str = 'F1',
    floor_code: str = '3F',
) -> tuple[WarehouseArea, WarehouseAreaStoragePolicy]:
    floor = db.scalar(
        select(WarehouseFloor).where(WarehouseFloor.floor_code == floor_code)
    )
    assert floor is not None
    area = WarehouseArea(
        floor_id=floor.id,
        area_code=area_code,
        area_name=f'三楼 {area_code} 区',
        planned_location_count=0,
        planned_pallet_capacity=0,
        construction_status='layout_building',
        capacity_review_status='pending',
        capacity_eligible=False,
    )
    db.add(area)
    db.flush()
    policy = WarehouseAreaStoragePolicy(
        area_id=area.id,
        map_feature_id='zone-f1',
        allowed_inventory_types_json=json.dumps(['finished']),
        storage_layout='pallet_ground',
        status='draft',
        version=1,
        updated_by=admin.id,
    )
    db.add(policy)
    db.flush()
    return area, policy


def _change_policy_payload(*, revision: str, version: int, operation_key: str):
    payload = _policy_payload(
        revision=revision,
        version=version,
        operation_key=operation_key,
    )
    return payload.model_copy(
        update={
            'allowed_inventory_types': ['semi_finished'],
            'storage_layout': 'rack',
        }
    )


def _formal_snapshot(db, *, policy_id: int, location_id: int | None = None) -> dict:
    policy = db.get(WarehouseAreaStoragePolicy, policy_id)
    assert policy is not None
    lot = db.scalar(select(InventoryLot).order_by(InventoryLot.id))
    pallet = db.scalar(select(InventoryPallet).order_by(InventoryPallet.id))
    return {
        'policy': (
            policy.version,
            policy.status,
            policy.allowed_inventory_types_json,
            policy.storage_layout,
            policy.draft_map_revision,
            policy.published_map_revision,
        ),
        'location': (
            None
            if location_id is None
            else (
                db.get(WarehouseLocation, location_id).is_active,
                db.get(WarehouseLocation, location_id).warehouse_type,
                db.get(WarehouseLocation, location_id).storage_type,
            )
        ),
        'lot': (
            None
            if lot is None
            else (
                lot.quantity_available,
                lot.quantity_reserved,
                lot.quantity_damaged,
                lot.status,
                lot.warehouse_location_id,
            )
        ),
        'pallet': (
            None
            if pallet is None
            else (pallet.location_id, pallet.is_current, pallet.status, pallet.version)
        ),
        'logs': db.scalar(select(func.count(OperationLog.id))),
    }


def _assert_policy_blocked_without_changes(
    db,
    *,
    draft: Path,
    policy_id: int,
    before: dict,
    location_id: int | None = None,
) -> None:
    assert not draft.exists()
    db.expire_all()
    assert _formal_snapshot(
        db, policy_id=policy_id, location_id=location_id
    ) == before


@pytest.mark.parametrize('occupancy_kind', ('lot', 'pallet'))
def test_occupied_location_blocks_incompatible_policy_without_changes(
    tmp_path: Path,
    monkeypatch,
    occupancy_kind: str,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            _area, policy = _bind_formal_area(db, admin=admin)
            location = WarehouseLocation(
                location_code=f'F1-{occupancy_kind}-001',
                location_name=f'F1 {occupancy_kind}',
                warehouse_type='finished',
                is_active=occupancy_kind == 'location',
                warehouse_floor=3,
                area_code='F1',
                storage_type='ground',
                sort_order=1,
                source_version='TWIN_V1',
                placement_status='placed',
            )
            db.add(location)
            db.flush()
            if occupancy_kind == 'lot':
                db.add(
                    InventoryLot(
                        lot_number='P1-47B-LOT-001',
                        inventory_type='finished',
                        warehouse_location_id=location.id,
                        quantity_available=12,
                        quantity_reserved=3,
                        quantity_consumed=0,
                        quantity_damaged=1,
                        quantity_scrapped=0,
                        unit='boxes',
                        status='active',
                        source_type='stocktake',
                        stock_date=date(2026, 8, 12),
                        stock_date_accuracy='exact',
                        last_movement_at=datetime(2026, 8, 12, 9, 0),
                        version=1,
                    )
                )
            elif occupancy_kind == 'pallet':
                db.add(
                    InventoryPallet(
                        pallet_code='P1-47B-PALLET-001',
                        location_id=location.id,
                        location_occupancy_key='PRIMARY',
                        status='active',
                        is_current=True,
                        needs_relocation=False,
                        version=1,
                    )
                )
            db.commit()
            before = _formal_snapshot(
                db, policy_id=policy.id, location_id=location.id
            )

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.update_twin_zone_storage_policy(
                    '3F',
                    'zone-f1',
                    _change_policy_payload(
                        revision=_revision(published),
                        version=1,
                        operation_key=f'p1-47b-block-{occupancy_kind}',
                    ),
                    _request(),
                    db,
                    admin,
                )
            assert caught.value.status_code == 409
            _assert_policy_blocked_without_changes(
                db,
                draft=draft,
                policy_id=policy.id,
                before=before,
                location_id=location.id,
            )
            if occupancy_kind == 'pallet':
                pallet = db.scalar(select(InventoryPallet))
                assert pallet is not None
                pallet.is_current = False
                pallet.status = 'closed'
                pallet.location_id = None
                db.commit()

                retried = warehouse_api.update_twin_zone_storage_policy(
                    '3F',
                    'zone-f1',
                    _change_policy_payload(
                        revision=_revision(published),
                        version=1,
                        operation_key='p1-47b-retry-after-pallet-moved',
                    ),
                    _request(),
                    db,
                    admin,
                )
                assert retried['applied'] is True
                assert draft.is_file()
    finally:
        engine.dispose()


@pytest.mark.parametrize('asset_kind', ('archived_mold', 'active_plate', 'damaged_plate'))
def test_physical_asset_blocks_policy_without_draft_or_database_changes(
    tmp_path: Path,
    monkeypatch,
    asset_kind: str,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    if asset_kind.endswith('plate'):
        document = json.loads(published.read_text(encoding='utf-8'))
        feature = document['floors']['3F']['features'][0]
        feature['feature_code'] = 'ZONE-1F-PLATE-002'
        document['floors']['3F']['floor_code'] = '1F'
        document['floors']['3F']['layout_id'] = 'layout-1f-p1-47b'
        document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
        document['floors']['1F'] = document['floors'].pop('3F')
        published.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        monkeypatch.setattr(editor, '_normalize_floor_code', lambda _code: '1F')
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None and floor is not None
            if asset_kind.endswith('plate'):
                floor.floor_code = '1F'
                floor.floor_number = 1
            area_code = 'AB2-N' if asset_kind == 'archived_mold' else 'F1'
            _area, policy = _bind_formal_area(
                db,
                admin=admin,
                area_code=area_code,
                floor_code='1F' if asset_kind.endswith('plate') else '3F',
            )
            if asset_kind == 'archived_mold':
                db.add(
                    MoldTool(
                        mold_code='P1-47B-ARCHIVED-MOLD',
                        mold_name='AB2-N 归档模具',
                        rack_location='3F-M-ARCHIVE-AB2-N',
                        location_version=2,
                        is_active=False,
                        archive_status='archived',
                        archived_at=datetime(2026, 8, 12, 8, 0),
                        archived_by=admin.id,
                        archive_reason='长期不用',
                        pre_archive_location='1F-M-R01',
                    )
                )
            else:
                customer = Customer(name=f'P1-47B {asset_kind} customer')
                db.add(customer)
                db.flush()
                db.add(
                    PrintingPlate(
                        plate_code=f'P147B-{asset_kind}',
                        customer_id=customer.id,
                        plate_name='P1-47B 挂板',
                        color_name='黑色',
                        rack_location='1F-PL-R01-L2-P01',
                        status='active' if asset_kind == 'active_plate' else 'damaged',
                        version=1,
                        location_version=1,
                    )
                )
            db.commit()
            before = _formal_snapshot(db, policy_id=policy.id)
            floor_code = '1F' if asset_kind.endswith('plate') else '3F'

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.update_twin_zone_storage_policy(
                    floor_code,
                    'zone-f1',
                    _change_policy_payload(
                        revision=_revision(published, floor_code),
                        version=1,
                        operation_key=f'p1-47b-block-{asset_kind}',
                    ).model_copy(update={'erp_area_code': area_code}),
                    _request(),
                    db,
                    admin,
                )
            assert caught.value.status_code == 409
            _assert_policy_blocked_without_changes(
                db, draft=draft, policy_id=policy.id, before=before
            )
    finally:
        engine.dispose()


def _seed_optional_draft(
    tmp_path: Path,
    monkeypatch,
    *,
    had_draft: bool,
) -> tuple[Path, str, int, bytes | None]:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    if not had_draft:
        return draft, _revision(published), 1, None
    seeded = update_warehouse_twin_zone_geometry(
        '3F',
        'zone-f1',
        expected_revision=_revision(published),
        expected_version=1,
        operation_key='p1-47b-seed-draft-0001',
        points=[[0, 0], [9_000, 0], [9_000, 9_000], [0, 9_000]],
    )
    return draft, seeded.floor_revision, 2, draft.read_bytes()


def _assert_draft_restored(draft: Path, before: bytes | None) -> None:
    if before is None:
        assert not draft.exists()
    else:
        assert draft.read_bytes() == before


@pytest.mark.parametrize('had_draft', (False, True))
@pytest.mark.parametrize('failure_point', ('audit', 'commit'))
def test_geometry_failure_restores_exact_prior_draft(
    tmp_path: Path,
    monkeypatch,
    had_draft: bool,
    failure_point: str,
) -> None:
    draft, revision, version, before = _seed_optional_draft(
        tmp_path, monkeypatch, had_draft=had_draft
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None

            def fail(*_args, **_kwargs) -> None:
                raise RuntimeError(f'{failure_point} failed')

            if failure_point == 'audit':
                monkeypatch.setattr(warehouse_api, '_twin_layout_asset_log', fail)
            else:
                monkeypatch.setattr(db, 'commit', fail)

            with pytest.raises(RuntimeError, match='failed'):
                warehouse_api.update_twin_zone_geometry(
                    '3F',
                    'zone-f1',
                    _geometry_payload(
                        revision=revision,
                        version=version,
                        operation_key=f'p1-47b-geometry-{failure_point}-{had_draft}',
                    ),
                    _request(),
                    db,
                    admin,
                )

            _assert_draft_restored(draft, before)
            assert not db.new
            assert not db.dirty
            assert db.scalar(select(func.count(OperationLog.id))) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize('had_draft', (False, True))
@pytest.mark.parametrize('failure_point', ('audit', 'commit'))
def test_storage_policy_failure_restores_draft_and_formal_database(
    tmp_path: Path,
    monkeypatch,
    had_draft: bool,
    failure_point: str,
) -> None:
    draft, revision, version, before = _seed_optional_draft(
        tmp_path, monkeypatch, had_draft=had_draft
    )
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None

            def fail(*_args, **_kwargs) -> None:
                raise RuntimeError(f'{failure_point} failed')

            if failure_point == 'audit':
                monkeypatch.setattr(warehouse_api, '_twin_layout_asset_log', fail)
            else:
                monkeypatch.setattr(db, 'commit', fail)

            with pytest.raises(RuntimeError, match='failed'):
                warehouse_api.update_twin_zone_storage_policy(
                    '3F',
                    'zone-f1',
                    _policy_payload(
                        revision=revision,
                        version=version,
                        operation_key=f'p1-47b-policy-{failure_point}-{had_draft}',
                    ),
                    _request(),
                    db,
                    admin,
                )

            _assert_draft_restored(draft, before)
            assert not db.new
            assert not db.dirty
            db.rollback()

        with factory() as verify:
            assert verify.scalar(select(func.count(WarehouseArea.id))) == 0
            assert verify.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
            assert verify.scalar(select(func.count(OperationLog.id))) == 0
    finally:
        engine.dispose()


def test_empty_area_first_policy_is_not_blocked_when_mapping_store_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)

    def missing_mapping_store(*_args, **_kwargs):
        raise WarehouseTwinProductionError('mapping store missing')

    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        missing_mapping_store,
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            response = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                _policy_payload(
                    revision=_revision(published),
                    version=1,
                    operation_key='p1-47b-empty-first-policy',
                ),
                _request(),
                db,
                admin,
            )
            assert response['applied'] is True
            assert response['item']['formal_binding_status'] == 'draft'
            assert response['item']['formal_policy_status'] is None

        with factory() as verify:
            assert verify.scalar(select(func.count(WarehouseArea.id))) == 0
            assert verify.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
            assert verify.scalar(select(func.count(OperationLog.id))) == 1
            draft_feature = json.loads(
                Path(editor.TWIN_LAYOUT_DRAFT_PATH).read_text(encoding='utf-8')
            )['floors']['3F']['features'][0]
            assert draft_feature['erp_area_code'] == 'F1'
            assert draft_feature['formal_area_name'] == '三楼成品区'
            assert draft_feature['allowed_inventory_types'] == ['finished']
    finally:
        engine.dispose()


def test_bound_area_mapping_store_unreadable_does_not_freeze_formal_warehouse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)

    def unreadable_mapping_store(*_args, **_kwargs):
        raise WarehouseTwinProductionError('mapping store unreadable')

    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        unreadable_mapping_store,
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            _area, policy = _bind_formal_area(db, admin=admin)
            db.commit()
            version_before = policy.version

            result = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                _change_policy_payload(
                    revision=_revision(published),
                    version=1,
                    operation_key='p1-47b-bound-unreadable',
                ),
                _request(),
                db,
                admin,
            )
            assert result['applied'] is True
            assert draft.exists()
            db.expire_all()
            assert db.get(WarehouseAreaStoragePolicy, policy.id).version == version_before
            assert db.scalar(select(func.count(OperationLog.id))) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ('task_status', 'expected_blocked'),
    (('pending', True), ('completed', False)),
)
def test_only_pending_production_mapping_blocks_policy_change(
    tmp_path: Path,
    monkeypatch,
    task_status: str,
    expected_blocked: bool,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    mappings: list[dict] = []
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: list(mappings),
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            _area, policy = _bind_formal_area(db, admin=admin)
            task = _add_production_task(db, status=task_status)
            db.commit()
            mappings.append(
                {
                    'source_task_id': task.id,
                    'target_kind': 'zone',
                    'target_id': 'zone-f1',
                }
            )
            before = _formal_snapshot(db, policy_id=policy.id)
            payload = _change_policy_payload(
                revision=_revision(published),
                version=1,
                operation_key=f'p1-47b-production-{task_status}',
            )

            if expected_blocked:
                with pytest.raises(warehouse_api.HTTPException) as caught:
                    warehouse_api.update_twin_zone_storage_policy(
                        '3F', 'zone-f1', payload, _request(), db, admin
                    )
                assert caught.value.status_code == 409
                assert '待生产任务地图占用' in str(caught.value.detail)
                assert not draft.exists()
                db.expire_all()
                assert _formal_snapshot(db, policy_id=policy.id) == before
            else:
                response = warehouse_api.update_twin_zone_storage_policy(
                    '3F', 'zone-f1', payload, _request(), db, admin
                )
                assert response['applied'] is True
                assert draft.is_file()
                db.expire_all()
                changed = db.get(WarehouseAreaStoragePolicy, policy.id)
                assert changed is not None
                formal_after = _formal_snapshot(db, policy_id=policy.id)
                assert formal_after['policy'] == before['policy']
                assert formal_after['location'] == before['location']
                assert formal_after['lot'] == before['lot']
                assert formal_after['pallet'] == before['pallet']
                assert formal_after['logs'] == before['logs'] + 1
                draft_feature = json.loads(draft.read_text(encoding='utf-8'))[
                    'floors'
                ]['3F']['features'][0]
                assert draft_feature['allowed_inventory_types'] == ['semi_finished']
                assert draft_feature['storage_layout'] == 'rack'
                assert db.get(ProductionTask, task.id).status == 'completed'
                assert db.scalar(select(func.count(OperationLog.id))) == 1
    finally:
        engine.dispose()


def test_old_policy_key_replay_after_geometry_does_not_bump_policy_or_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            first_payload = _policy_payload(
                revision=_revision(published),
                version=1,
                operation_key='p1-47b-policy-then-geometry',
            )
            first = warehouse_api.update_twin_zone_storage_policy(
                '3F', 'zone-f1', first_payload, _request(), db, admin
            )
            geometry = warehouse_api.update_twin_zone_geometry(
                '3F',
                'zone-f1',
                _geometry_payload(
                    revision=first['revision'],
                    version=2,
                    operation_key='p1-47b-geometry-after-policy',
                ),
                _request(),
                db,
                admin,
            )
            assert geometry['applied'] is True
            assert db.scalar(select(WarehouseAreaStoragePolicy)) is None
            log_count_before = db.scalar(select(func.count(OperationLog.id)))
            draft_before_replay = Path(editor.TWIN_LAYOUT_DRAFT_PATH).read_bytes()

            replay = warehouse_api.update_twin_zone_storage_policy(
                '3F', 'zone-f1', first_payload, _request(), db, admin
            )
            assert replay['applied'] is False
            db.expire_all()
            assert db.scalar(select(WarehouseAreaStoragePolicy)) is None
            assert db.scalar(select(func.count(OperationLog.id))) == log_count_before
            assert Path(editor.TWIN_LAYOUT_DRAFT_PATH).read_bytes() == draft_before_replay
    finally:
        engine.dispose()


def test_orphan_formal_location_blocks_first_policy_binding_before_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)

    def mapping_lookup_must_not_run(*_args, **_kwargs):
        pytest.fail('orphan location must block before production mapping lookup')

    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        mapping_lookup_must_not_run,
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            location = WarehouseLocation(
                location_code='ORPHAN-F1-001',
                location_name='历史 F1 库位',
                warehouse_type='finished',
                is_active=False,
                warehouse_floor=3,
                area_code='F1',
                storage_type='ground',
                sort_order=1,
                source_version='TWIN_V1',
                placement_status='placed',
            )
            db.add(location)
            db.commit()
            before = (
                location.location_code,
                location.location_name,
                location.warehouse_type,
                location.is_active,
                location.warehouse_floor,
                location.area_code,
                location.storage_type,
                location.source_version,
                location.placement_status,
            )

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.update_twin_zone_storage_policy(
                    '3F',
                    'zone-f1',
                    _policy_payload(
                        revision=_revision(published),
                        version=1,
                        operation_key='p1-47b-orphan-location',
                    ),
                    _request(),
                    db,
                    admin,
                )

            assert caught.value.status_code == 409
            assert '未纳入正式区域台账的历史库位' in str(caught.value.detail)
            assert not draft.exists()
            db.expire_all()
            restored = db.get(WarehouseLocation, location.id)
            assert restored is not None
            assert (
                restored.location_code,
                restored.location_name,
                restored.warehouse_type,
                restored.is_active,
                restored.warehouse_floor,
                restored.area_code,
                restored.storage_type,
                restored.source_version,
                restored.placement_status,
            ) == before
            assert db.scalar(select(func.count(WarehouseArea.id))) == 0
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
            assert db.scalar(select(func.count(OperationLog.id))) == 0
    finally:
        engine.dispose()


def test_sql_map_policy_drift_still_checks_active_location_occupancy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    floor = document['floors']['3F']
    feature = next(item for item in floor['features'] if item['id'] == 'zone-f1')
    feature['allowed_inventory_types'] = ['finished']
    feature['storage_layout'] = 'pallet_ground'
    floor['revision'] = _floor_revision(floor)
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            _area, policy = _bind_formal_area(db, admin=admin)
            policy.allowed_inventory_types_json = json.dumps(['semi_finished'])
            policy.storage_layout = 'rack'
            location = WarehouseLocation(
                location_code='F1-DRIFT-001',
                location_name='F1 漂移核对库位',
                warehouse_type='semi_finished',
                is_active=True,
                warehouse_floor=3,
                area_code='F1',
                storage_type='rack',
                sort_order=1,
                source_version='TWIN_V1',
                placement_status='placed',
            )
            db.add(location)
            db.flush()
            db.add(
                InventoryPallet(
                    pallet_code='P1-47B-DRIFT-PALLET-001',
                    location_id=location.id,
                    location_occupancy_key='PRIMARY',
                    status='active',
                    is_current=True,
                    needs_relocation=False,
                    version=1,
                )
            )
            db.commit()
            before = _formal_snapshot(
                db, policy_id=policy.id, location_id=location.id
            )

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.update_twin_zone_storage_policy(
                    '3F',
                    'zone-f1',
                    _policy_payload(
                        revision=_revision(published),
                        version=1,
                        operation_key='p1-47b-sql-map-drift',
                    ),
                    _request(),
                    db,
                    admin,
                )

            assert caught.value.status_code == 409
            assert '库存或实体栈板' in str(caught.value.detail)
            _assert_policy_blocked_without_changes(
                db,
                draft=draft,
                policy_id=policy.id,
                before=before,
                location_id=location.id,
            )
    finally:
        engine.dispose()


def _validated_policy_draft(
    tmp_path: Path,
    monkeypatch,
    *,
    operation_suffix: str,
) -> tuple[Path, Path, Path, str, str]:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    policy_seed = editor.update_warehouse_twin_zone_policy(
        '3F',
        'zone-f1',
        expected_revision=_revision(published),
        expected_version=1,
        operation_key=f'p1-47b-policy-{operation_suffix}',
        allowed_inventory_types=['finished'],
        storage_layout='pallet_ground',
        erp_area_code='F1',
        area_name='三楼成品区',
    )
    validate_warehouse_twin_layout_draft(
        '3F', expected_revision=policy_seed.floor_revision
    )
    return (
        published,
        draft,
        Path(editor.TWIN_LAYOUT_PATH),
        _revision(published),
        policy_seed.floor_revision,
    )


@pytest.mark.parametrize('runtime_existed', (False, True))
def test_publish_receipt_write_failure_restores_runtime_draft_and_backups_exactly(
    tmp_path: Path,
    monkeypatch,
    runtime_existed: bool,
) -> None:
    published, draft, runtime, published_revision, draft_revision = _validated_policy_draft(
        tmp_path, monkeypatch, operation_suffix=f'receipt-{runtime_existed}'
    )
    backups = Path(editor.TWIN_LAYOUT_BACKUP_DIR)
    if runtime_existed:
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_bytes(published.read_bytes())
    baseline_before = published.read_bytes()
    runtime_before = runtime.read_bytes() if runtime.exists() else None
    draft_before = draft.read_bytes()
    backup_before = {
        path.name: path.read_bytes() for path in backups.glob('*.json')
    } if backups.exists() else {}
    real_write = editor._write_document
    write_targets: list[Path] = []

    def fail_publish_receipt(path: Path, payload: dict) -> None:
        target = Path(path)
        write_targets.append(target)
        if (
            target == draft
            and (payload.get('draft_meta') or {}).get('status') == 'published'
        ):
            raise OSError('draft publish receipt failed')
        real_write(target, payload)

    monkeypatch.setattr(editor, '_write_document', fail_publish_receipt)
    with pytest.raises(WarehouseTwinLayoutEditError) as caught:
        editor.publish_warehouse_twin_layout_draft(
            '3F',
            expected_published_revision=published_revision,
            expected_draft_revision=draft_revision,
            operation_key=f'p1-47b-publish-receipt-{runtime_existed}',
        )
    assert isinstance(caught.value.__cause__, OSError)
    assert write_targets[:2] == [runtime, draft]
    assert published.read_bytes() == baseline_before
    assert (runtime.read_bytes() if runtime.exists() else None) == runtime_before
    assert draft.read_bytes() == draft_before
    assert (
        {path.name: path.read_bytes() for path in backups.glob('*.json')}
        if backups.exists()
        else {}
    ) == backup_before
    assert not list(runtime.parent.glob('.*.tmp'))
    assert not list(draft.parent.glob('.*.tmp'))
    if backups.exists():
        assert not list(backups.glob('.*.tmp'))


@pytest.mark.parametrize(
    'failure_point', ('activation', 'spatial_validation', 'audit', 'commit')
)
def test_publish_failure_restores_published_draft_policy_and_audit(
    tmp_path: Path,
    monkeypatch,
    failure_point: str,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime_published = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda _floor_code: json.loads(
            runtime_published.read_text(encoding='utf-8')
        )['floors']['3F'],
    )
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            area, policy = _bind_formal_area(db, admin=admin)
            db.commit()
            seeded = update_warehouse_twin_zone_geometry(
                '3F',
                'zone-f1',
                expected_revision=_revision(published),
                expected_version=1,
                operation_key=f'p1-47b-publish-seed-{failure_point}',
                points=[[0, 0], [9_000, 0], [9_000, 9_000], [0, 9_000]],
            )
            policy_seed = editor.update_warehouse_twin_zone_policy(
                '3F',
                'zone-f1',
                expected_revision=seeded.floor_revision,
                expected_version=2,
                operation_key=f'p1-47b-publish-policy-{failure_point}',
                allowed_inventory_types=['finished'],
                storage_layout='pallet_ground',
                erp_area_code='F1',
                area_name='三楼成品区',
            )
            validated = validate_warehouse_twin_layout_draft(
                '3F', expected_revision=policy_seed.floor_revision
            )
            published_before = published.read_bytes()
            draft_before = draft.read_bytes()
            policy_before = (policy.status, policy.version, policy.published_map_revision)

            failure_hit = []

            def fail(*_args, **_kwargs):
                failure_hit.append(True)
                if failure_point in {'activation', 'spatial_validation'}:
                    raise WarehouseAreaActivationError('activation failed', status_code=409)
                raise RuntimeError(f'{failure_point} failed')

            if failure_point == 'activation':
                monkeypatch.setattr(warehouse_api, 'publish_floor_area_policies', fail)
            elif failure_point == 'spatial_validation':
                monkeypatch.setattr(
                    warehouse_api,
                    '_validate_published_area_layouts_for_floor',
                    fail,
                )
            elif failure_point == 'audit':
                monkeypatch.setattr(warehouse_api, '_twin_layout_asset_log', fail)
            else:
                monkeypatch.setattr(db, 'commit', fail)

            payload = warehouse_api.TwinLayoutDraftPublishPayload(
                expected_published_revision=_revision(published),
                expected_draft_revision=validated.value['draft_revision'],
                operation_key=f'p1-47b-publish-failure-{failure_point}',
            )
            expected_error = (
                warehouse_api.HTTPException
                if failure_point in {'activation', 'spatial_validation'}
                else RuntimeError
            )
            with pytest.raises(expected_error):
                warehouse_api.publish_twin_layout_draft(
                    '3F', payload, _request(), db, admin
                )
            assert failure_hit == [True]

            assert published.read_bytes() == published_before
            assert not runtime_published.exists()
            assert draft.read_bytes() == draft_before
            assert not db.new
            assert not db.dirty
            db.expire_all()
            restored_policy = db.get(WarehouseAreaStoragePolicy, policy.id)
            assert restored_policy is not None
            assert (
                restored_policy.status,
                restored_policy.version,
                restored_policy.published_map_revision,
            ) == policy_before
            assert db.scalar(select(func.count(OperationLog.id))) == 0
            assert db.get(WarehouseArea, area.id) is not None
    finally:
        engine.dispose()


def test_publish_same_key_replay_does_not_reactivate_policy_or_duplicate_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft, runtime, published_revision, draft_revision = _validated_policy_draft(
        tmp_path, monkeypatch, operation_suffix='same-key-replay'
    )
    backups = Path(editor.TWIN_LAYOUT_BACKUP_DIR)
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            area, policy = _bind_formal_area(db, admin=admin)
            policy.draft_map_revision = draft_revision
            db.commit()
            payload = warehouse_api.TwinLayoutDraftPublishPayload(
                expected_published_revision=published_revision,
                expected_draft_revision=draft_revision,
                operation_key='p1-47b-publish-same-key',
            )

            first = warehouse_api.publish_twin_layout_draft(
                '3F', payload, _request(), db, admin
            )
            assert first['applied'] is True
            assert first['formal_area_count'] == 1
            db.expire_all()
            published_policy = db.get(WarehouseAreaStoragePolicy, policy.id)
            published_area = db.get(WarehouseArea, area.id)
            assert published_policy is not None
            assert published_area is not None
            policy_after_first = (
                published_policy.status,
                published_policy.version,
                published_policy.draft_map_revision,
                published_policy.published_map_revision,
                published_policy.updated_by,
            )
            assert published_area.construction_status == 'enabled'
            publish_logs_after_first = db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == 'TWIN_LAYOUT_PUBLISH'
                )
            )
            assert publish_logs_after_first == 1
            runtime_after_first = runtime.read_bytes()
            draft_after_first = draft.read_bytes()
            backups_after_first = _backup_manifest(backups)

            replay = warehouse_api.publish_twin_layout_draft(
                '3F', payload, _request(), db, admin
            )

            assert replay['applied'] is False
            assert replay['formal_area_count'] == 0
            assert replay['published_revision'] == first['published_revision']
            db.expire_all()
            replayed_policy = db.get(WarehouseAreaStoragePolicy, policy.id)
            replayed_area = db.get(WarehouseArea, area.id)
            assert replayed_policy is not None
            assert replayed_area is not None
            assert (
                replayed_policy.status,
                replayed_policy.version,
                replayed_policy.draft_map_revision,
                replayed_policy.published_map_revision,
                replayed_policy.updated_by,
            ) == policy_after_first
            assert replayed_area.construction_status == 'enabled'
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == 'TWIN_LAYOUT_PUBLISH'
                )
            ) == publish_logs_after_first
            assert runtime.read_bytes() == runtime_after_first
            assert draft.read_bytes() == draft_after_first
            assert _backup_manifest(backups) == backups_after_first
    finally:
        engine.dispose()


def test_three_floor_new_map_zone_materializes_formal_policy_only_on_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft, runtime, published_revision, draft_revision = _validated_policy_draft(
        tmp_path, monkeypatch, operation_suffix='missing-formal-policy'
    )
    backups = Path(editor.TWIN_LAYOUT_BACKUP_DIR)
    published_before = published.read_bytes()
    draft_before = draft.read_bytes()
    runtime_before = runtime.read_bytes() if runtime.exists() else None
    backups_before = _backup_manifest(backups)
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            payload = warehouse_api.TwinLayoutDraftPublishPayload(
                expected_published_revision=published_revision,
                expected_draft_revision=draft_revision,
                operation_key='p1-47b-publish-without-policy',
            )

            assert db.scalar(select(func.count(WarehouseArea.id))) == 0
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
            result = warehouse_api.publish_twin_layout_draft(
                '3F', payload, _request(), db, admin
            )

            assert result['applied'] is True
            assert published.read_bytes() == published_before
            assert runtime.is_file()
            assert (runtime.read_bytes() if runtime.exists() else None) != runtime_before
            assert draft.read_bytes() != draft_before
            assert _backup_manifest(backups) != backups_before
            area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == 'F1'))
            assert area is not None
            assert area.construction_status == 'enabled'
            assert area.storage_policy is not None
            assert area.storage_policy.status == 'published'
            assert area.storage_policy.allowed_inventory_types_json == json.dumps(
                ['finished'], ensure_ascii=False, separators=(',', ':')
            )
            assert db.scalar(select(func.count(OperationLog.id))) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ('action_name', 'failure_point'),
    (
        ('validate', 'audit'),
        ('validate', 'commit'),
        ('discard', 'audit'),
        ('discard', 'commit'),
    ),
)
def test_validate_or_discard_failure_restores_original_draft_and_zero_audit(
    tmp_path: Path,
    monkeypatch,
    action_name: str,
    failure_point: str,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    seeded = editor.update_warehouse_twin_zone_geometry(
        '3F',
        'zone-f1',
        expected_revision=_revision(published),
        expected_version=1,
        operation_key=f'p1-47b-{action_name}-{failure_point}-seed',
        points=[[500, 500], [9_500, 500], [9_500, 8_000], [500, 8_000]],
    )
    draft_before = draft.read_bytes()
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None

            def fail(*_args, **_kwargs):
                raise RuntimeError(f'{action_name} {failure_point} failed')

            if failure_point == 'audit':
                monkeypatch.setattr(warehouse_api, '_twin_layout_asset_log', fail)
            else:
                monkeypatch.setattr(db, 'commit', fail)

            with pytest.raises(RuntimeError):
                if action_name == 'validate':
                    warehouse_api.validate_twin_layout_draft(
                        '3F',
                        warehouse_api.TwinLayoutDraftValidatePayload(
                            expected_revision=seeded.floor_revision
                        ),
                        _request(),
                        db,
                        admin,
                    )
                else:
                    warehouse_api.discard_twin_layout_draft(
                        '3F',
                        warehouse_api.TwinLayoutDraftDiscardPayload(
                            expected_revision=seeded.floor_revision
                        ),
                        _request(),
                        db,
                        admin,
                    )

            assert draft.read_bytes() == draft_before
            assert not db.new
            assert not db.dirty
            assert db.scalar(select(func.count(OperationLog.id))) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize('location_active', (False, True))
def test_empty_location_policy_change_then_publish_keeps_identity_and_syncs_fields(
    tmp_path: Path,
    monkeypatch,
    location_active: bool,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    floor_document = document['floors']['3F']
    feature = next(
        item for item in floor_document['features'] if item['id'] == 'zone-f1'
    )
    feature['feature_code'] = 'ZONE-3F-FORMAL-X1'
    feature['name'] = 'X1'
    feature['erp_area_code'] = 'X1'
    floor_document['revision'] = _floor_revision(floor_document)
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            area, policy = _bind_formal_area(db, admin=admin, area_code='X1')
            location = WarehouseLocation(
                location_code='X1-INACTIVE-EMPTY-001',
                location_name='X1 停用空库位',
                warehouse_type='finished',
                is_active=location_active,
                warehouse_floor=3,
                area_code='X1',
                storage_type='ground',
                sort_order=1,
                source_version='TWIN_V1',
                placement_status='placed',
            )
            db.add(location)
            db.commit()
            location_id = location.id

            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=_revision(published),
                    expected_version=1,
                    operation_key='p1-47b-inactive-empty-rack',
                    allowed_inventory_types=['finished'],
                    storage_layout='rack',
                    erp_area_code='X1',
                    area_name='三楼 X1 区',
                ),
                _request(),
                db,
                admin,
            )
            assert changed['applied'] is True
            validated = warehouse_api.validate_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftValidatePayload(
                    expected_revision=changed['revision']
                ),
                _request(),
                db,
                admin,
            )
            assert validated['status'] == 'validated'

            result = warehouse_api.publish_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftPublishPayload(
                    expected_published_revision=_revision(published),
                    expected_draft_revision=changed['revision'],
                    operation_key='p1-47b-inactive-empty-publish',
                ),
                _request(),
                db,
                admin,
            )
            assert result['applied'] is True
            db.expire_all()
            restored_location = db.get(WarehouseLocation, location_id)
            restored_policy = db.get(WarehouseAreaStoragePolicy, policy.id)
            restored_area = db.get(WarehouseArea, area.id)
            assert restored_location is not None
            assert restored_policy is not None
            assert restored_area is not None
            assert restored_location.id == location_id
            assert restored_location.warehouse_type == 'finished'
            assert restored_location.storage_type == 'rack'
            assert restored_location.is_active is location_active
            assert restored_policy.allowed_inventory_types_json == json.dumps(
                ['finished'], ensure_ascii=False, separators=(',', ':')
            )
            assert restored_policy.storage_layout == 'rack'
            assert restored_policy.status == 'published'
            assert restored_area.construction_status == 'enabled'
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 0
            assert draft.is_file()
    finally:
        engine.dispose()


def test_draft_policy_is_visible_only_in_admin_draft_overlay_until_published(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            area, policy = _bind_formal_area(db, admin=admin)
            area.area_name = '仅草稿可见的 F1 名称'
            area.planned_pallet_capacity = 77
            policy.allowed_inventory_types_json = json.dumps(['semi_finished'])
            policy.storage_layout = 'rack'
            policy.status = 'draft'
            db.commit()
            monkeypatch.setattr(
                warehouse_api,
                'load_warehouse_twin_floor',
                lambda _floor_code: json.loads(published.read_text(encoding='utf-8'))[
                    'floors'
                ]['3F'],
            )
            monkeypatch.setattr(
                warehouse_api,
                'load_warehouse_twin_layout_draft',
                lambda _floor_code: json.loads(published.read_text(encoding='utf-8'))[
                    'floors'
                ]['3F'],
            )

            formal = warehouse_api.get_warehouse_twin_floor_layout('3F', db, admin)
            formal_feature = next(
                item for item in formal['features'] if item['id'] == 'zone-f1'
            )
            for key in (
                'formal_area_id',
                'allowed_inventory_types',
                'storage_layout',
                'formal_binding_status',
                'formal_area_name',
                'planned_pallet_capacity',
                'capacity_review_status',
            ):
                assert key not in formal_feature

            draft_overlay = warehouse_api.get_warehouse_twin_floor_layout_draft(
                '3F', db, admin
            )
            draft_feature = next(
                item for item in draft_overlay['features'] if item['id'] == 'zone-f1'
            )
            assert draft_feature['allowed_inventory_types'] == ['semi_finished']
            assert draft_feature['storage_layout'] == 'rack'
            assert draft_feature['formal_binding_status'] == 'draft'
            assert draft_feature['formal_area_name'] == '仅草稿可见的 F1 名称'
            assert draft_feature['planned_pallet_capacity'] == 77
            assert draft_feature['capacity_review_status'] == 'pending'

            policy.status = 'published'
            db.commit()
            formal_after_publish = warehouse_api.get_warehouse_twin_floor_layout(
                '3F', db, admin
            )
            published_feature = next(
                item
                for item in formal_after_publish['features']
                if item['id'] == 'zone-f1'
            )
            assert published_feature['allowed_inventory_types'] == ['semi_finished']
            assert published_feature['storage_layout'] == 'rack'
            assert published_feature['formal_binding_status'] == 'published'
            assert published_feature['formal_area_id'] == area.id
            assert published_feature['formal_area_name'] == '仅草稿可见的 F1 名称'
            assert published_feature['planned_pallet_capacity'] == 77
            assert published_feature['capacity_review_status'] == 'pending'
    finally:
        engine.dispose()


def test_cross_floor_drafts_publish_one_floor_and_preserve_the_other(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    _add_floor_one_to_published_layout(published)
    baseline_document = json.loads(published.read_text(encoding='utf-8'))
    baseline_document['floors']['3F']['features'][0].pop('erp_area_code', None)
    baseline_document['floors']['3F']['revision'] = _floor_revision(
        baseline_document['floors']['3F']
    )
    published.write_text(
        json.dumps(baseline_document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    backups = Path(editor.TWIN_LAYOUT_BACKUP_DIR)
    published_before = published.read_bytes()
    original_1f_revision = _revision(published, '1F')
    changed_3f = editor.update_warehouse_twin_zone_geometry(
        '3F',
        'zone-f1',
        expected_revision=_revision(published, '3F'),
        expected_version=1,
        operation_key='p1-47b-cross-floor-3f',
        points=[[500, 500], [9_500, 500], [9_500, 8_000], [500, 8_000]],
    )
    changed_1f = editor.update_warehouse_twin_zone_geometry(
        '1F',
        'zone-1f',
        expected_revision=original_1f_revision,
        expected_version=1,
        operation_key='p1-47b-cross-floor-1f',
        points=[[250, 250], [7_750, 250], [7_750, 7_500], [250, 7_500]],
    )
    assert changed_1f.applied is True
    initial_3f_control = editor.load_warehouse_twin_layout_draft('3F')[
        'draft_control'
    ]
    assert initial_3f_control['has_draft'] is True
    assert initial_3f_control['has_other_floor_drafts'] is True

    editor.validate_warehouse_twin_layout_draft(
        '3F', expected_revision=changed_3f.floor_revision
    )
    first = editor.publish_warehouse_twin_layout_draft(
        '3F',
        expected_published_revision=_revision(published, '3F'),
        expected_draft_revision=changed_3f.floor_revision,
        operation_key='p1-47b-publish-only-3f',
    )
    assert first.applied is True
    assert first.value['remaining_draft_floor_codes'] == ['1F']
    live_after_3f = json.loads(runtime.read_text(encoding='utf-8'))
    assert live_after_3f['floors']['3F']['revision'] == changed_3f.floor_revision
    assert live_after_3f['floors']['1F']['revision'] == original_1f_revision
    assert published.read_bytes() == published_before

    control_3f = editor.load_warehouse_twin_layout_draft('3F')['draft_control']
    control_1f = editor.load_warehouse_twin_layout_draft('1F')['draft_control']
    assert control_3f['has_draft'] is False
    assert control_3f['has_other_floor_drafts'] is True
    assert control_1f['has_draft'] is True
    assert control_1f['status'] == 'draft'
    assert control_1f['dirty_floor_codes'] == ['1F']

    repeated = editor.publish_warehouse_twin_layout_draft(
        '3F',
        expected_published_revision=_revision(published, '3F'),
        expected_draft_revision=changed_3f.floor_revision,
        operation_key='p1-47b-publish-only-3f',
    )
    assert repeated.applied is False
    assert repeated.value == first.value
    with pytest.raises(WarehouseTwinLayoutEditConflictError, match='不同版本'):
        editor.publish_warehouse_twin_layout_draft(
            '3F',
            expected_published_revision='different-published-revision',
            expected_draft_revision=changed_3f.floor_revision,
            operation_key='p1-47b-publish-only-3f',
        )

    runtime_before_unvalidated = runtime.read_bytes()
    draft_before_unvalidated = draft.read_bytes()
    backups_before_unvalidated = _backup_manifest(backups)
    with pytest.raises(WarehouseTwinLayoutEditConflictError, match='先校验当前楼层'):
        editor.publish_warehouse_twin_layout_draft(
            '1F',
            expected_published_revision=original_1f_revision,
            expected_draft_revision=changed_1f.floor_revision,
            operation_key='p1-47b-unvalidated-1f',
        )
    assert runtime.read_bytes() == runtime_before_unvalidated
    assert draft.read_bytes() == draft_before_unvalidated
    assert _backup_manifest(backups) == backups_before_unvalidated

    editor.validate_warehouse_twin_layout_draft(
        '1F', expected_revision=changed_1f.floor_revision
    )
    second = editor.publish_warehouse_twin_layout_draft(
        '1F',
        expected_published_revision=original_1f_revision,
        expected_draft_revision=changed_1f.floor_revision,
        operation_key='p1-47b-publish-only-1f',
    )
    assert second.value['remaining_draft_floor_codes'] == []
    final_live = json.loads(runtime.read_text(encoding='utf-8'))
    assert final_live['floors']['3F']['revision'] == changed_3f.floor_revision
    assert final_live['floors']['1F']['revision'] == changed_1f.floor_revision
    assert json.loads(draft.read_text(encoding='utf-8'))['draft_meta']['status'] == 'published'
    assert len(_backup_manifest(backups)) == 2


def test_discard_removes_only_the_selected_floor_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    _add_floor_one_to_published_layout(published)
    original_3f_revision = _revision(published, '3F')
    original_1f_revision = _revision(published, '1F')
    changed_3f = editor.update_warehouse_twin_zone_geometry(
        '3F',
        'zone-f1',
        expected_revision=original_3f_revision,
        expected_version=1,
        operation_key='p1-47b-discard-floor-3f',
        points=[[500, 500], [9_500, 500], [9_500, 8_000], [500, 8_000]],
    )
    changed_1f = editor.update_warehouse_twin_zone_geometry(
        '1F',
        'zone-1f',
        expected_revision=original_1f_revision,
        expected_version=1,
        operation_key='p1-47b-discard-floor-1f',
        points=[[250, 250], [7_750, 250], [7_750, 7_500], [250, 7_500]],
    )

    discarded = editor.discard_warehouse_twin_layout_draft(
        '1F', expected_revision=changed_1f.floor_revision
    )
    assert discarded.applied is True
    assert discarded.value['remaining_draft_floor_codes'] == ['3F']
    assert draft.is_file()
    assert editor.load_warehouse_twin_layout_draft('1F')['draft_control']['has_draft'] is False
    assert editor.load_warehouse_twin_layout_draft('3F')['draft_control']['has_draft'] is True

    editor.discard_warehouse_twin_layout_draft(
        '3F', expected_revision=changed_3f.floor_revision
    )
    assert not draft.exists()


def test_cross_floor_publish_rebase_write_failure_rolls_back_every_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    _add_floor_one_to_published_layout(published)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    backups = Path(editor.TWIN_LAYOUT_BACKUP_DIR)
    changed_3f = editor.update_warehouse_twin_zone_geometry(
        '3F',
        'zone-f1',
        expected_revision=_revision(published, '3F'),
        expected_version=1,
        operation_key='p1-47b-rebase-failure-3f',
        points=[[500, 500], [9_500, 500], [9_500, 8_000], [500, 8_000]],
    )
    editor.update_warehouse_twin_zone_geometry(
        '1F',
        'zone-1f',
        expected_revision=_revision(published, '1F'),
        expected_version=1,
        operation_key='p1-47b-rebase-failure-1f',
        points=[[250, 250], [7_750, 250], [7_750, 7_500], [250, 7_500]],
    )
    editor.validate_warehouse_twin_layout_draft(
        '3F', expected_revision=changed_3f.floor_revision
    )
    baseline_before = published.read_bytes()
    draft_before = draft.read_bytes()
    backups_before = _backup_manifest(backups)
    real_write = editor._write_document

    def fail_rebased_draft(path: Path, payload: dict) -> None:
        meta = payload.get('draft_meta') or {}
        if (
            Path(path) == draft
            and meta.get('status') == 'draft'
            and (meta.get('last_publish') or {}).get('operation_key')
            == 'p1-47b-rebase-failure-publish'
        ):
            raise OSError('rebased draft write failed')
        real_write(path, payload)

    monkeypatch.setattr(editor, '_write_document', fail_rebased_draft)
    with pytest.raises(WarehouseTwinLayoutEditError) as caught:
        editor.publish_warehouse_twin_layout_draft(
            '3F',
            expected_published_revision=_revision(published, '3F'),
            expected_draft_revision=changed_3f.floor_revision,
            operation_key='p1-47b-rebase-failure-publish',
        )
    assert isinstance(caught.value.__cause__, OSError)
    assert published.read_bytes() == baseline_before
    assert not runtime.exists()
    assert draft.read_bytes() == draft_before
    assert _backup_manifest(backups) == backups_before


def test_published_area_policy_draft_and_discard_preserve_formal_employee_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    published_document = json.loads(published.read_text(encoding='utf-8'))
    published_feature = published_document['floors']['3F']['features'][0]
    published_feature['allowed_inventory_types'] = ['finished']
    published_feature['storage_layout'] = 'pallet_ground'
    published_document['floors']['3F']['revision'] = _floor_revision(
        published_document['floors']['3F']
    )
    published.write_text(
        json.dumps(published_document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda _floor_code: json.loads(published.read_text(encoding='utf-8'))[
            'floors'
        ]['3F'],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_layout_draft',
        lambda _floor_code: editor.load_warehouse_twin_layout_draft('3F'),
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None
            assert floor is not None
            area, policy = _bind_formal_area(db, admin=admin)
            area.construction_status = 'enabled'
            policy.status = 'published'
            policy.published_map_revision = _revision(published)
            location = WarehouseLocation(
                location_code='F1-EMPLOYEE-001',
                location_name='F1 员工作业库位',
                warehouse_type='finished',
                is_active=True,
                warehouse_floor=3,
                area_code='F1',
                storage_type='ground',
                source_version='V11',
                placement_status='placed',
            )
            db.add(location)
            db.commit()
            formal_before = (
                area.area_name,
                area.construction_status,
                area.planned_pallet_capacity,
                policy.status,
                policy.version,
                policy.allowed_inventory_types_json,
                policy.storage_layout,
                policy.draft_map_revision,
                policy.published_map_revision,
            )
            candidates_before = warehouse_api.list_location_candidates(
                inventory_type='finished',
                empty_only=True,
                pallet_storage_only=False,
                include_hierarchy=False,
                db=db,
                _user=admin,
            )
            assert [item['id'] for item in candidates_before['items']] == [
                location.id
            ]

            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=_revision(published),
                    expected_version=1,
                    operation_key='p1-47b-published-area-draft',
                    allowed_inventory_types=['finished'],
                    storage_layout='pallet_ground',
                    erp_area_code='F1',
                    area_name='F1 草稿新名称',
                ),
                _request(),
                db,
                admin,
            )
            assert changed['applied'] is True
            assert changed['item']['formal_binding_status'] == 'draft'
            assert changed['item']['formal_policy_status'] == 'published'
            db.expire_all()
            formal_area = db.get(WarehouseArea, area.id)
            formal_policy = db.get(WarehouseAreaStoragePolicy, policy.id)
            assert formal_area is not None
            assert formal_policy is not None
            assert (
                formal_area.area_name,
                formal_area.construction_status,
                formal_area.planned_pallet_capacity,
                formal_policy.status,
                formal_policy.version,
                formal_policy.allowed_inventory_types_json,
                formal_policy.storage_layout,
                formal_policy.draft_map_revision,
                formal_policy.published_map_revision,
            ) == formal_before
            assert [
                item['id']
                for item in warehouse_api.list_location_candidates(
                    inventory_type='finished',
                    empty_only=True,
                    pallet_storage_only=False,
                    include_hierarchy=False,
                    db=db,
                    _user=admin,
                )['items']
            ] == [location.id]
            formal_get = warehouse_api.get_warehouse_twin_floor_layout(
                '3F', db, admin
            )
            formal_get_feature = next(
                item for item in formal_get['features'] if item['id'] == 'zone-f1'
            )
            assert formal_get_feature['allowed_inventory_types'] == ['finished']
            assert formal_get_feature['storage_layout'] == 'pallet_ground'
            assert formal_get_feature['formal_area_name'] == formal_before[0]
            draft_get = warehouse_api.get_warehouse_twin_floor_layout_draft(
                '3F', db, admin
            )
            draft_get_feature = next(
                item for item in draft_get['features'] if item['id'] == 'zone-f1'
            )
            assert draft_get_feature['allowed_inventory_types'] == ['finished']
            assert draft_get_feature['storage_layout'] == 'pallet_ground'
            assert draft_get_feature['formal_area_name'] == 'F1 草稿新名称'

            discarded = warehouse_api.discard_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftDiscardPayload(
                    expected_revision=changed['revision']
                ),
                _request(),
                db,
                admin,
            )
            assert discarded['applied'] is True
            assert not draft.exists()
            db.expire_all()
            discarded_area = db.get(WarehouseArea, area.id)
            discarded_policy = db.get(WarehouseAreaStoragePolicy, policy.id)
            assert discarded_area is not None
            assert discarded_policy is not None
            assert (
                discarded_area.area_name,
                discarded_area.construction_status,
                discarded_area.planned_pallet_capacity,
                discarded_policy.status,
                discarded_policy.version,
                discarded_policy.allowed_inventory_types_json,
                discarded_policy.storage_layout,
                discarded_policy.draft_map_revision,
                discarded_policy.published_map_revision,
            ) == formal_before
    finally:
        engine.dispose()


def test_legacy_v11_area_name_draft_survives_overlay_and_publish_keeps_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding="utf-8"))
    feature = document["floors"]["3F"]["features"][0]
    feature["allowed_inventory_types"] = ["finished"]
    feature["storage_layout"] = "pallet_ground"
    document["floors"]["3F"]["revision"] = _floor_revision(
        document["floors"]["3F"]
    )
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)

    def load_published_floor(floor_code: str) -> dict:
        source = runtime if runtime.exists() else published
        return json.loads(source.read_text(encoding="utf-8"))["floors"][
            floor_code.upper()
        ]

    monkeypatch.setattr(
        warehouse_api,
        "list_production_projection_mappings",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        load_published_floor,
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_layout_draft",
        lambda _floor_code: editor.load_warehouse_twin_layout_draft("3F"),
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-47b-admin"))
            floor = db.scalar(
                select(WarehouseFloor).where(WarehouseFloor.floor_code == "3F")
            )
            assert admin is not None and floor is not None
            area = WarehouseArea(
                floor_id=floor.id,
                area_code="F1",
                area_name="F1 区",
                planned_location_count=1,
                planned_pallet_capacity=1,
                construction_status="enabled",
                capacity_review_status="pending",
                capacity_eligible=False,
                confirmed_pallet_capacity=None,
            )
            db.add(area)
            db.flush()
            location = WarehouseLocation(
                location_code="F1-L01",
                location_name="F1-L01",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="F1",
                storage_type="ground",
                sort_order=1,
                source_version="V11",
                placement_status="placed",
            )
            location.floor3_layout = Floor3LocationLayout(
                left_pct=5,
                top_pct=5,
                width_pct=10,
                height_pct=10,
                version=1,
                source_type="manual",
                created_by=admin.id,
                updated_by=admin.id,
            )
            db.add(location)
            db.flush()
            lot = InventoryLot(
                lot_number="P1-102-NAME-LOT-001",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=12,
                quantity_reserved=3,
                quantity_consumed=0,
                quantity_damaged=1,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="stocktake",
                stock_date=date(2026, 8, 25),
                stock_date_accuracy="exact",
                last_movement_at=datetime(2026, 8, 25, 9, 0),
                version=1,
            )
            db.add(lot)
            db.commit()
            area_id = area.id
            location_id = location.id
            lot_id = lot.id
            published_revision = _revision(published)

            formal_before = warehouse_api.get_warehouse_twin_floor_layout(
                "3F", db, admin
            )
            formal_before_feature = next(
                item for item in formal_before["features"] if item["id"] == "zone-f1"
            )
            assert formal_before_feature["formal_area_id"] == area_id
            assert formal_before_feature["employee_area_name"] == "右区F1"

            changed = warehouse_api.update_twin_zone_storage_policy(
                "3F",
                "zone-f1",
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=published_revision,
                    expected_version=1,
                    operation_key="p1-102-v11-name-only-draft",
                    allowed_inventory_types=["finished"],
                    storage_layout="pallet_ground",
                    erp_area_code="F1",
                    area_name="三楼右侧成品整箱区",
                    existing_area_id=area_id,
                ),
                _request(),
                db,
                admin,
            )
            assert changed["applied"] is True
            assert changed["item"]["formal_area_name"] == "三楼右侧成品整箱区"
            assert changed["item"]["employee_area_name"] == "三楼右侧成品整箱区"
            assert changed["item"]["legacy_v11_name_only"] is True
            assert db.get(WarehouseArea, area_id).area_name == "F1 区"

            draft_overlay = warehouse_api.get_warehouse_twin_floor_layout_draft(
                "3F", db, admin
            )
            draft_feature = next(
                item
                for item in draft_overlay["features"]
                if item["id"] == "zone-f1"
            )
            assert draft_feature["formal_area_name"] == "三楼右侧成品整箱区"
            assert draft_feature["employee_area_name"] == "三楼右侧成品整箱区"

            validated = warehouse_api.validate_twin_layout_draft(
                "3F",
                warehouse_api.TwinLayoutDraftValidatePayload(
                    expected_revision=changed["revision"]
                ),
                _request(),
                db,
                admin,
            )
            assert validated["status"] == "validated"
            publish_payload = warehouse_api.TwinLayoutDraftPublishPayload(
                expected_published_revision=published_revision,
                expected_draft_revision=changed["revision"],
                operation_key="p1-102-v11-name-only-publish",
            )
            file_only_publish = editor.publish_warehouse_twin_layout_draft(
                "3F",
                expected_published_revision=published_revision,
                expected_draft_revision=changed["revision"],
                operation_key=publish_payload.operation_key,
            )
            assert file_only_publish.applied is True
            assert db.get(WarehouseArea, area_id).area_name == "F1 区"

            result = warehouse_api.publish_twin_layout_draft(
                "3F",
                publish_payload,
                _request(),
                db,
                admin,
            )
            assert result["applied"] is False
            assert result["legacy_area_name_update_count"] == 1
            publish_log_count = db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_PUBLISH"
                )
            )
            assert publish_log_count == 1

            replay = warehouse_api.publish_twin_layout_draft(
                "3F",
                publish_payload,
                _request(),
                db,
                admin,
            )
            assert replay["applied"] is False
            assert replay["legacy_area_name_update_count"] == 0
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_PUBLISH"
                )
            ) == publish_log_count

            db.expire_all()
            restored_area = db.get(WarehouseArea, area_id)
            restored_location = db.get(WarehouseLocation, location_id)
            restored_lot = db.get(InventoryLot, lot_id)
            assert restored_area is not None
            assert restored_location is not None
            assert restored_lot is not None
            assert restored_area.id == area_id
            assert restored_area.area_code == "F1"
            assert restored_area.area_name == "三楼右侧成品整箱区"
            assert restored_location.id == location_id
            assert restored_location.location_code == "F1-L01"
            assert restored_location.area_code == "F1"
            assert restored_lot.warehouse_location_id == location_id
            assert restored_lot.quantity_available == Decimal("12")
            assert restored_area.storage_policy is None
            assert db.scalar(
                select(func.count(WarehouseAreaStoragePolicy.id)).where(
                    WarehouseAreaStoragePolicy.area_id == area_id
                )
            ) == 0

            published_feature = next(
                item
                for item in load_published_floor("3F")["features"]
                if item["id"] == "zone-f1"
            )
            assert published_feature["formal_area_name"] == "三楼右侧成品整箱区"
            assert published_feature["legacy_v11_name_only"] is True
            projection = warehouse_location_projection(
                restored_location,
                floor=floor,
                area=restored_area,
                policy=None,
                published_floor_identity={
                    "revision": result["published_revision"],
                    "zone_ids_by_area": {"F1": ["zone-f1"]},
                },
                layout=restored_location.floor3_layout,
            )
            assert projection["position_status"] == "mapped"
            assert projection["map_feature_id"] == "zone-f1"

            area_payload = warehouse_api._warehouse_area_dict(db, restored_area)
            location_payload = warehouse_api.list_location_candidates(
                inventory_type="finished",
                empty_only=False,
                pallet_storage_only=False,
                include_hierarchy=False,
                db=db,
                _user=admin,
            )
            candidate = next(
                item
                for item in location_payload["items"]
                if item["id"] == location_id
            )
            twin_payload = warehouse_twin_location_payload(
                restored_location,
                lots=[],
                pallets=[],
                as_of=date(2026, 8, 25),
                projection_context={
                    "floor": floor,
                    "area": restored_area,
                    "policy": None,
                },
            )
            address_payload = location_address_payload(
                restored_location,
                area=restored_area,
                floor=floor,
                position_status="mapped",
            )
            assert area_payload["employee_area_name"] == "三楼右侧成品整箱区"
            assert candidate["area_name"] == "三楼右侧成品整箱区"
            assert twin_payload["area_name"] == "三楼右侧成品整箱区"
            assert address_payload["area_name"] == "三楼右侧成品整箱区"
            assert candidate["employee_location_name"].startswith(
                "三楼右侧成品整箱区·"
            )
            assert twin_payload["employee_location_name"] == candidate[
                "employee_location_name"
            ]
            assert address_payload["employee_location_name"] == candidate[
                "employee_location_name"
            ]
            assert "位置名称待完善" not in candidate["employee_location_name"]

            runtime_before_semantic_edit = runtime.read_bytes()
            draft_before_semantic_edit = (
                draft.read_bytes() if draft.exists() else None
            )
            with pytest.raises(warehouse_api.HTTPException) as semantic_error:
                warehouse_api.update_twin_zone_storage_policy(
                    "3F",
                    "zone-f1",
                    warehouse_api.TwinZoneStoragePolicyPayload(
                        expected_revision=result["published_revision"],
                        expected_version=int(published_feature["version"]),
                        operation_key="p1-102-v11-semantic-change-blocked",
                        allowed_inventory_types=["finished"],
                        storage_layout="rack",
                        erp_area_code="F1",
                        area_name="不应发布的货架区名称",
                        existing_area_id=area_id,
                    ),
                    _request(),
                    db,
                    admin,
                )
            assert semantic_error.value.status_code == 409
            assert runtime.read_bytes() == runtime_before_semantic_edit
            assert (
                draft.read_bytes() if draft.exists() else None
            ) == draft_before_semantic_edit
            db.expire_all()
            assert db.get(WarehouseArea, area_id).storage_policy is None
            assert db.get(WarehouseArea, area_id).area_name == "三楼右侧成品整箱区"
    finally:
        engine.dispose()


def test_new_area_policy_stays_json_only_until_publish_creates_formal_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    baseline_document = json.loads(published.read_text(encoding='utf-8'))
    baseline_feature = baseline_document['floors']['3F']['features'][0]
    baseline_feature.pop('erp_area_code', None)
    baseline_document['floors']['3F']['revision'] = _floor_revision(
        baseline_document['floors']['3F']
    )
    published.write_text(
        json.dumps(baseline_document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=_revision(published),
                    expected_version=1,
                    operation_key='p1-47b-new-area-json-only',
                    allowed_inventory_types=['finished'],
                    storage_layout='pallet_ground',
                    erp_area_code='X2',
                    area_name='三楼 X2 新区域',
                ),
                _request(),
                db,
                admin,
            )
            assert changed['applied'] is True
            assert changed['item']['formal_binding_status'] == 'draft'
            assert changed['item']['formal_policy_status'] is None
            assert draft.is_file()
            assert db.scalar(select(func.count(WarehouseArea.id))) == 0
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
            draft_feature = json.loads(draft.read_text(encoding='utf-8'))[
                'floors'
            ]['3F']['features'][0]
            assert draft_feature['erp_area_code'] == 'X2'
            assert draft_feature['formal_area_name'] == '三楼 X2 新区域'

            validated = warehouse_api.validate_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftValidatePayload(
                    expected_revision=changed['revision']
                ),
                _request(),
                db,
                admin,
            )
            assert validated['status'] == 'validated'
            result = warehouse_api.publish_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftPublishPayload(
                    expected_published_revision=_revision(published),
                    expected_draft_revision=changed['revision'],
                    operation_key='p1-47b-new-area-publish',
                ),
                _request(),
                db,
                admin,
            )
            assert result['applied'] is True
            area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == 'X2'))
            assert area is not None
            assert area.area_name == '三楼 X2 新区域'
            assert area.construction_status == 'enabled'
            assert area.storage_policy is not None
            assert area.storage_policy.status == 'published'
            assert area.storage_policy.allowed_inventory_types_json == json.dumps(
                ['finished'], ensure_ascii=False, separators=(',', ':')
            )
            assert area.storage_policy.storage_layout == 'pallet_ground'
    finally:
        engine.dispose()


def test_one_step_area_confirmation_saves_validates_publishes_and_confirms_capacity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            revision = _revision(published)
            result = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=revision,
                    operation_key='p1-60-one-step-area',
                    capacity=12,
                ),
                _request(),
                db,
                admin,
            )
            assert result['status'] == 'published'
            assert result['area']['area_code'] == 'F1'
            assert result['area']['construction_status'] == 'enabled'
            assert result['area']['capacity_review_status'] == 'confirmed'
            assert result['area']['confirmed_pallet_capacity'] == 12
            assert result['area']['planned_pallet_capacity'] == 12
            assert result['area']['storage_policy']['status'] == 'published'
            assert result['area']['storage_policy']['allowed_inventory_types'] == ['finished']
            assert result['area']['storage_policy']['storage_layout'] == 'pallet_ground'
            assert result['created_location_count'] == 12
            assert result['available_location_count'] == 12
            assert result['inventory_changed'] is False
            assert result['pallet_binding_changed'] is False
            assert runtime.is_file()
            assert draft.is_file()
            assert json.loads(draft.read_text(encoding='utf-8'))['draft_meta']['status'] == 'published'
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 1
            assert db.scalar(select(func.count(WarehouseLocation.id))) == 12
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 0
            actions = set(db.scalars(select(OperationLog.action)).all())
            assert 'TWIN_ZONE_ONE_STEP_CONFIRM' in actions
            assert 'CAPACITY_UPDATE' in actions
    finally:
        engine.dispose()


def test_one_step_new_floor3_zone_does_not_force_verified_v11_siblings_to_rebind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding="utf-8"))
    floor_layout = document["floors"]["3F"]
    legacy = floor_layout["features"][0]
    legacy.update(
        {
            "id": "zone-3f-a1",
            "feature_code": "ZONE-3F-ERP-A1",
            "name": "A1 成品区",
            "erp_area_code": "A1",
            "points": [[0, 0], [2500, 0], [2500, 10000], [0, 10000]],
        }
    )
    new_zone = {
        **legacy,
        "id": "zone-3f-fg-002",
        "feature_code": "ZONE-3F-FG-002",
        "name": "FG-002 成品区",
        "points": [[4600, 0], [10000, 0], [10000, 10000], [4600, 10000]],
    }
    new_zone.pop("erp_area_code", None)
    delivery_surplus = {
        **legacy,
        "id": "zone-3f-e4",
        "feature_code": "ZONE-3F-FG-001",
        "name": "E4 送剩零头区",
        "erp_area_code": "E4",
        "points": [[2600, 0], [3400, 0], [3400, 10000], [2600, 10000]],
    }
    unrelated_draft_zone = {
        **legacy,
        "id": "zone-3f-raw-001",
        "feature_code": "ZONE-3F-RAW-001",
        "name": "RAW-001 原料区",
        "points": [[3500, 0], [4500, 0], [4500, 10000], [3500, 10000]],
    }
    unrelated_draft_zone.pop("erp_area_code", None)
    floor_layout["features"].extend(
        [delivery_surplus, unrelated_draft_zone, new_zone]
    )
    floor_layout["revision"] = _floor_revision(floor_layout)
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        "list_production_projection_mappings",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda floor_code: json.loads(runtime.read_text(encoding="utf-8"))["floors"][
            floor_code.upper()
        ],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-47b-admin"))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == "3F"))
            assert admin is not None and floor is not None
            legacy_area = WarehouseArea(
                floor_id=floor.id,
                area_code="A1",
                area_name="A1 成品区",
                planned_location_count=1,
                planned_pallet_capacity=1,
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=1,
                capacity_reviewed_by="P1-60 管理员",
                capacity_reviewed_at=datetime(2026, 8, 14, 16, 0),
            )
            db.add(legacy_area)
            db.flush()
            legacy_location = WarehouseLocation(
                location_code="3F-A1-L001",
                location_name="A1 001号位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                source_version="V11",
                placement_status="placed",
            )
            legacy_location.floor3_layout = Floor3LocationLayout(
                left_pct=5,
                top_pct=5,
                width_pct=20,
                height_pct=20,
                version=1,
                source_type="manual",
                created_by=admin.id,
                updated_by=admin.id,
            )
            db.add(legacy_location)
            e4_area = WarehouseArea(
                floor_id=floor.id,
                area_code="E4",
                area_name="E4 送剩零头区",
                planned_location_count=1,
                planned_pallet_capacity=1,
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=1,
                capacity_reviewed_by="P1-60 管理员",
                capacity_reviewed_at=datetime(2026, 8, 14, 16, 0),
            )
            db.add(e4_area)
            db.flush()
            e4_location = WarehouseLocation(
                location_code="3F-E4-L001",
                location_name="E4 001号位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="E4",
                storage_type="ground",
                source_version="V11",
                placement_status="placed",
            )
            e4_location.floor3_layout = Floor3LocationLayout(
                left_pct=5,
                top_pct=5,
                width_pct=20,
                height_pct=20,
                version=1,
                source_type="manual",
                created_by=admin.id,
                updated_by=admin.id,
            )
            db.add(e4_location)
            raw_area = WarehouseArea(
                floor_id=floor.id,
                area_code="RAW-001",
                area_name="RAW-001 原料区",
                planned_location_count=0,
                planned_pallet_capacity=6,
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=6,
                capacity_reviewed_by="P1-60 管理员",
                capacity_reviewed_at=datetime(2026, 8, 14, 16, 0),
            )
            db.add(raw_area)
            db.flush()
            raw_policy = WarehouseAreaStoragePolicy(
                area_id=raw_area.id,
                map_feature_id="zone-3f-raw-001",
                allowed_inventory_types_json=json.dumps(["raw_material"]),
                storage_layout="pallet_ground",
                status="draft",
                version=1,
                updated_by=admin.id,
            )
            db.add(raw_policy)
            db.commit()

            revision = _revision(published)
            result = warehouse_api.confirm_twin_zone_area(
                "3F",
                "zone-3f-fg-002",
                _confirm_area_payload(
                    revision=revision,
                    operation_key="p1-60-floor3-new-with-v11-sibling",
                    capacity=1,
                    area_code="FG-002",
                    area_name="FG-002 成品区",
                ),
                _request(),
                db,
                admin,
            )

            assert result["status"] == "published"
            assert result["area"]["area_code"] == "FG-002"
            assert result["created_location_count"] == 1
            assert db.get(WarehouseArea, legacy_area.id).storage_policy is None
            assert db.get(WarehouseLocation, legacy_location.id).source_version == "V11"
            assert db.get(WarehouseLocation, legacy_location.id).is_active is True
            assert db.get(WarehouseArea, e4_area.id).storage_policy is None
            assert db.get(WarehouseLocation, e4_location.id).is_active is True
            assert db.get(WarehouseAreaStoragePolicy, raw_policy.id).status == "draft"
            assert db.scalar(select(func.count(WarehouseArea.id))) == 4
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 2
    finally:
        engine.dispose()


def test_one_step_zero_capacity_marks_non_pallet_area_excluded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            result = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=_revision(published),
                    operation_key='p1-60-one-step-mold',
                    usage='mold',
                    storage_layout='rack',
                    capacity=0,
                ),
                _request(),
                db,
                admin,
            )
            assert result['area']['capacity_review_status'] == 'excluded'
            assert result['area']['capacity_eligible'] is False
            assert result['area']['confirmed_pallet_capacity'] is None
            assert result['area']['planned_pallet_capacity'] == 0
            assert result['area']['storage_policy']['allowed_inventory_types'] == ['mold']
            assert result['area']['storage_policy']['storage_layout'] == 'rack'
            assert result['created_location_count'] == 0
            assert result['available_location_count'] == 0
            assert db.scalar(select(func.count(WarehouseLocation.id))) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    (
        "storage_layout",
        "capacity",
        "expected_storage_type",
        "expected_available_count",
    ),
    [
        ("pallet_ground", 3, "ground", 0),
        ("rack", 2, "rack", 2),
    ],
)
def test_one_step_confirmed_capacity_activates_narrow_pallet_and_rack_zones(
    tmp_path: Path,
    monkeypatch,
    storage_layout: str,
    capacity: int,
    expected_storage_type: str,
    expected_available_count: int,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding="utf-8"))
    floor_layout = document["floors"]["3F"]
    feature = floor_layout["features"][0]
    feature.pop("erp_area_code", None)
    feature["feature_code"] = "ZONE-3F-NARROW-001"
    feature["name"] = "三楼窄长实测区域"
    feature["points"] = [[0, 0], [500, 0], [500, 10000], [0, 10000]]
    floor_layout["revision"] = _floor_revision(floor_layout)
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        "list_production_projection_mappings",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda floor_code: json.loads(runtime.read_text(encoding="utf-8"))["floors"][
            floor_code.upper()
        ],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-47b-admin"))
            assert admin is not None
            revision = _revision(published)
            result = warehouse_api.confirm_twin_zone_area(
                "3F",
                feature["id"],
                _confirm_area_payload(
                    revision=revision,
                    operation_key=f"p1-60-narrow-{storage_layout}",
                    storage_layout=storage_layout,
                    capacity=capacity,
                    area_code="NARROW-001",
                    area_name="三楼窄长实测区域",
                ),
                _request(),
                db,
                admin,
            )

            assert result["created_location_count"] == capacity
            assert result["available_location_count"] == expected_available_count
            if storage_layout == "pallet_ground":
                assert result["ground_plan_status"] == "planning_only"
                assert "不能用于收料、入库或移位" in result["location_readiness_issue"]
            else:
                assert result["ground_plan_status"] is None
                assert result["location_readiness_issue"] is None
            rows = list(
                db.scalars(
                    select(WarehouseLocation).order_by(WarehouseLocation.location_code)
                ).all()
            )
            assert len(rows) == capacity
            assert all(row.is_active and row.placement_status == "placed" for row in rows)
            assert {row.storage_type for row in rows} == {expected_storage_type}
            assert all(row.floor3_layout is not None for row in rows)
            rectangles = {
                (
                    row.floor3_layout.left_pct,
                    row.floor3_layout.top_pct,
                    row.floor3_layout.width_pct,
                    row.floor3_layout.height_pct,
                )
                for row in rows
            }
            assert len(rectangles) == capacity
            assert all(
                0 <= value <= 100
                for rectangle in rectangles
                for value in rectangle
            )
    finally:
        engine.dispose()


def test_one_step_raw_material_area_creates_shared_pallet_positions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        lambda _floor_number: {
            "revision": _revision(runtime if runtime.exists() else published),
            "zones_by_id": {"zone-f1": "F1"},
            "zone_ids_by_area": {"F1": ("zone-f1",)},
        },
    )
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            result = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=_revision(published),
                    operation_key='p1-60-one-step-raw-pallets',
                    usage='raw_material',
                    storage_layout='pallet_ground',
                    capacity=4,
                ),
                _request(),
                db,
                admin,
            )

            assert result['status'] == 'published'
            assert result['area']['storage_policy']['allowed_inventory_types'] == [
                'raw_material'
            ]
            assert result['created_location_count'] == 4
            assert result['available_location_count'] == 4
            assert result['ground_plan_status'] == 'published'
            assert result['location_readiness_issue'] is None
            rows = list(
                db.scalars(select(WarehouseLocation).order_by(WarehouseLocation.id))
            )
            assert len(rows) == 4
            assert all(row.warehouse_type == 'shared' for row in rows)
            assert all(row.storage_type == 'ground' for row in rows)
            assert all(row.is_active and row.placement_status == 'placed' for row in rows)
            assert all(row.floor3_layout is not None for row in rows)
            plan = db.scalar(select(WarehouseGroundLayoutPlan))
            assert plan is not None
            assert plan.status == 'published'
            assert plan.published_map_revision == _revision(runtime)
            ground_slots = list(
                db.scalars(
                    select(WarehouseGroundLayoutSlot).order_by(
                        WarehouseGroundLayoutSlot.route_sequence
                    )
                )
            )
            assert {slot.location_id for slot in ground_slots} == {
                row.id for row in rows
            }
            assert [slot.route_sequence for slot in ground_slots] == [1, 2, 3, 4]
            expected_spatial_order = sorted(
                ground_slots,
                key=lambda slot: (
                    float(slot.y_mm) + float(slot.depth_mm) / 2,
                    float(slot.x_mm) + float(slot.width_mm) / 2,
                ),
            )
            assert [slot.id for slot in ground_slots] == [
                slot.id for slot in expected_spatial_order
            ]
            distinct_row_centers = sorted(
                {
                    round(float(slot.y_mm) + float(slot.depth_mm) / 2, 3)
                    for slot in ground_slots
                }
            )
            assert [slot.row_no for slot in ground_slots] == [
                distinct_row_centers.index(
                    round(float(slot.y_mm) + float(slot.depth_mm) / 2, 3)
                )
                + 1
                for slot in ground_slots
            ]

            current_document = json.loads(runtime.read_text(encoding='utf-8'))
            current_floor = current_document['floors']['3F']
            current_feature = next(
                item
                for item in current_floor['features']
                if item['id'] == 'zone-f1'
            )
            runtime_before_rejected_zero = runtime.read_bytes()
            with monkeypatch.context() as scoped:
                scoped.setattr(
                    warehouse_api,
                    '_ensure_one_step_pallet_locations',
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError(
                            'published plan members must not reach legacy count/reflow'
                        )
                    ),
                )
                with pytest.raises(warehouse_api.HTTPException) as zero_capacity:
                    warehouse_api.confirm_twin_zone_area(
                        '3F',
                        'zone-f1',
                        _confirm_area_payload(
                            revision=current_floor['revision'],
                            published_revision=current_floor['revision'],
                            operation_key='p1-60-one-step-existing-plan-zero',
                            usage='raw_material',
                            storage_layout='pallet_ground',
                            capacity=0,
                            expected_version=int(current_feature['version']),
                        ),
                        _request(),
                        db,
                        admin,
                    )
            assert zero_capacity.value.status_code == 409
            assert '不能从一次确认清空' in str(zero_capacity.value.detail)
            assert runtime.read_bytes() == runtime_before_rejected_zero
            assert db.scalar(
                select(func.count(WarehouseLocation.id)).where(
                    WarehouseLocation.is_active.is_(True)
                )
            ) == 4

            claimed_floor_numbers: list[int] = []
            with monkeypatch.context() as scoped:
                scoped.setattr(
                    warehouse_api,
                    'claim_warehouse_floor_projection',
                    lambda _db, *, floor_number: (
                        claimed_floor_numbers.append(int(floor_number)) or True
                    ),
                )
                warehouse_api._claim_floor_projection_for_layout_write(
                    db, floor_code='F3'
                )
            assert claimed_floor_numbers == [3]

            customer = Customer(
                name='P1-47B raw-only ground candidate',
                payment_term_days=0,
                credit_limit=Decimal('0'),
            )
            db.add(customer)
            db.flush()
            product = Product(
                customer_id=customer.id,
                product_code='P147B-RAW-ONLY-CANDIDATE',
                customer_material_code='P147B-RAW-ONLY-CANDIDATE',
                product_name='P1-47B raw-only candidate product',
            )
            db.add(product)
            db.commit()
            with pytest.raises(warehouse_api.HTTPException) as candidate_error:
                warehouse_api.list_ground_storage_candidates(
                    '3F',
                    'F1',
                    customer.id,
                    product.id,
                    1,
                    db,
                    admin,
                )
            assert candidate_error.value.status_code == 409
            assert candidate_error.value.detail['code'] == (
                'GROUND_AREA_INVENTORY_TYPE_NOT_ALLOWED'
            )

            area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == 'F1'))
            policy = db.scalar(select(WarehouseAreaStoragePolicy))
            assert area is not None and policy is not None
            policy.allowed_inventory_types_json = '{'
            db.flush()
            with pytest.raises(warehouse_api.HTTPException) as invalid_policy:
                warehouse_api.list_ground_storage_candidates(
                    '3F',
                    'F1',
                    customer.id,
                    product.id,
                    1,
                    db,
                    admin,
                )
            assert invalid_policy.value.status_code == 409
            assert invalid_policy.value.detail['code'] == 'GROUND_AREA_POLICY_INVALID'
            db.rollback()

            rows = list(
                db.scalars(select(WarehouseLocation).order_by(WarehouseLocation.id))
            )
            area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == 'F1'))
            policy = db.scalar(select(WarehouseAreaStoragePolicy))
            assert area is not None and policy is not None
            layout_versions = {
                row.id: row.floor3_layout.version for row in rows
            }
            first_layout = rows[0].floor3_layout
            assert first_layout is not None
            spatial_snapshot = [
                (
                    row.id,
                    row.is_active,
                    row.placement_status,
                    row.floor3_layout.version,
                    row.floor3_layout.left_pct,
                    row.floor3_layout.top_pct,
                    row.floor3_layout.width_pct,
                    row.floor3_layout.height_pct,
                )
                for row in rows
            ]
            protected_actions = [
                lambda: warehouse_api.set_activated_area_location_count(
                    '3F',
                    'F1',
                    warehouse_api.Floor3AreaLocationCountPayload(
                        target_count=3,
                        confirmed=True,
                        expected_map_revision=policy.published_map_revision,
                        expected_policy_version=policy.version,
                        expected_layout_versions=layout_versions,
                    ),
                    _request(),
                    db,
                    admin,
                ),
                lambda: warehouse_api.auto_arrange_activated_area_locations(
                    '3F',
                    'F1',
                    warehouse_api.AreaLocationAutoArrangePayload(
                        confirmed=True,
                        expected_map_revision=policy.published_map_revision,
                        expected_policy_version=policy.version,
                        expected_layout_versions=layout_versions,
                    ),
                    _request(),
                    db,
                    admin,
                ),
                lambda: warehouse_api.patch_activated_area_location_layout(
                    '3F',
                    'F1',
                    warehouse_api.Floor3LayoutAreaPatchPayload(
                        expected_map_revision=policy.published_map_revision,
                        expected_policy_version=policy.version,
                        slots=[
                            warehouse_api.Floor3LayoutAreaSlotPayload(
                                location_id=rows[0].id,
                                expected_version=first_layout.version,
                                left_pct=first_layout.left_pct,
                                top_pct=first_layout.top_pct,
                                width_pct=first_layout.width_pct,
                                height_pct=first_layout.height_pct,
                                z_index=first_layout.z_index,
                            )
                        ],
                    ),
                    _request(),
                    db,
                    admin,
                ),
                lambda: warehouse_api.disable_activated_area_location(
                    rows[0].id,
                    warehouse_api.Floor3LayoutSlotStatePayload(
                        expected_version=first_layout.version,
                        expected_map_revision=policy.published_map_revision,
                        expected_policy_version=policy.version,
                    ),
                    _request(),
                    db,
                    admin,
                ),
                lambda: warehouse_api.enable_activated_area_location(
                    rows[0].id,
                    warehouse_api.Floor3LayoutSlotStatePayload(
                        expected_version=first_layout.version,
                        expected_map_revision=policy.published_map_revision,
                        expected_policy_version=policy.version,
                    ),
                    _request(),
                    db,
                    admin,
                ),
            ]
            for action in protected_actions:
                with pytest.raises(warehouse_api.HTTPException) as locked:
                    action()
                assert locked.value.status_code == 409
                assert '地堆排位' in str(locked.value.detail)
            after_rows = list(
                db.scalars(select(WarehouseLocation).order_by(WarehouseLocation.id))
            )
            assert [
                (
                    row.id,
                    row.is_active,
                    row.placement_status,
                    row.floor3_layout.version,
                    row.floor3_layout.left_pct,
                    row.floor3_layout.top_pct,
                    row.floor3_layout.width_pct,
                    row.floor3_layout.height_pct,
                )
                for row in after_rows
            ] == spatial_snapshot

            plan = db.scalar(select(WarehouseGroundLayoutPlan))
            assert plan is not None
            with pytest.raises(WarehouseAreaActivationError, match='不能从一次确认清空'):
                warehouse_api._ensure_one_step_ground_plan(
                    db,
                    floor_layout=json.loads(runtime.read_text(encoding='utf-8'))[
                        'floors'
                    ]['3F'],
                    feature_id='zone-f1',
                    area=area,
                    storage_layout='pallet_ground',
                    location_count=0,
                    operation_key='p1-60-one-step-zero-after-plan',
                    operator_id=admin.id,
                )
            drifted_slot = db.scalar(
                select(WarehouseGroundLayoutSlot).order_by(
                    WarehouseGroundLayoutSlot.route_sequence
                )
            )
            assert drifted_slot is not None
            drifted_slot.x_mm += Decimal('10')
            db.flush()
            with pytest.raises(WarehouseAreaActivationError, match='已漂移'):
                warehouse_api._ensure_one_step_ground_plan(
                    db,
                    floor_layout=json.loads(runtime.read_text(encoding='utf-8'))[
                        'floors'
                    ]['3F'],
                    feature_id='zone-f1',
                    area=area,
                    storage_layout='pallet_ground',
                    location_count=4,
                    operation_key='p1-60-one-step-drifted-plan',
                    operator_id=admin.id,
                )
            db.rollback()
            rows = list(
                db.scalars(select(WarehouseLocation).order_by(WarehouseLocation.id))
            )
            pallet_result = warehouse_api.create_floor3_pallet(
                    warehouse_api.Floor3PalletCreatePayload(
                        location_id=rows[0].id,
                        expected_layout_version=rows[0].floor3_layout.version,
                        pallet_code='P1-60-RAW-PALLET-001',
                    items=[
                        warehouse_api.Floor3PalletItemPayload(
                            inventory_code='RAW-BOARD-1430X516',
                            product_name='1430×516 原纸板',
                            item_type='raw_material',
                            quantity=Decimal('100'),
                            unit='sheets',
                            match_status='pending',
                        )
                    ],
                ),
                _request(),
                db,
                admin,
            )
            assert pallet_result['pallet']['location_id'] == rows[0].id
            assert pallet_result['pallet']['items'][0]['item_type'] == 'raw_material'
            dashboard = warehouse_api.get_warehouse_twin_dashboard(30, db, admin)
            dashboard_location = next(
                item
                for item in dashboard['locations']
                if item['location_id'] == rows[0].id
            )
            assert dashboard_location['allowed_inventory_types'] == ['raw_material']
            assert dashboard_location['pallets'][0]['items'][0]['item_type'] == 'raw_material'
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 1
    finally:
        engine.dispose()


def test_formal_area_capacity_is_projected_live_to_the_unique_measured_map_zone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None
            assert floor is not None
            area = WarehouseArea(
                floor_id=floor.id,
                area_code='F1',
                area_name='F1 历史成品区',
                construction_status='enabled',
                planned_location_count=2,
                planned_pallet_capacity=8,
                capacity_review_status='confirmed',
                capacity_eligible=True,
                confirmed_pallet_capacity=6,
                capacity_reviewed_by='现场管理员',
                capacity_reviewed_at=datetime(2026, 8, 14, 10, 30),
            )
            db.add(area)
            db.flush()
            db.add(
                WarehouseLocation(
                    location_code='F1-LEGACY-001',
                    location_name='F1 历史一号位',
                    warehouse_type='finished',
                    is_active=True,
                    warehouse_floor=3,
                    area_code='F1',
                    storage_type='ground',
                    source_version='V11',
                    placement_status='placed',
                )
            )
            db.commit()
            monkeypatch.setattr(
                warehouse_api,
                'load_warehouse_twin_floor',
                lambda _floor_code: json.loads(published.read_text(encoding='utf-8'))[
                    'floors'
                ]['3F'],
            )

            first = warehouse_api.get_warehouse_twin_floor_layout('3F', db, admin)
            feature = next(item for item in first['features'] if item['id'] == 'zone-f1')
            assert feature['formal_area_id'] == area.id
            assert feature['formal_floor_id'] == floor.id
            assert feature['formal_binding_source'] == 'formal_area_code'
            assert feature['formal_binding_status'] == 'published'
            assert feature['formal_construction_status'] == 'enabled'
            assert feature['capacity_review_status'] == 'confirmed'
            assert feature['confirmed_pallet_capacity'] == 6

            area.confirmed_pallet_capacity = 8
            area.planned_pallet_capacity = 9
            db.commit()
            refreshed = warehouse_api.get_warehouse_twin_floor_layout('3F', db, admin)
            refreshed_feature = next(
                item for item in refreshed['features'] if item['id'] == 'zone-f1'
            )
            assert refreshed_feature['confirmed_pallet_capacity'] == 8
            assert refreshed_feature['planned_pallet_capacity'] == 9
    finally:
        engine.dispose()


def test_legacy_v11_overlay_derives_d1_mixed_layout_from_formal_locations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding="utf-8"))
    feature = document["floors"]["3F"]["features"][0]
    feature["feature_code"] = "ZONE-3F-ERP-D1"
    feature["name"] = "D1"
    feature["erp_area_code"] = "D1"
    document["floors"]["3F"]["revision"] = _floor_revision(
        document["floors"]["3F"]
    )
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda _floor_code: json.loads(published.read_text(encoding="utf-8"))[
            "floors"
        ]["3F"],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-47b-admin"))
            floor = db.scalar(
                select(WarehouseFloor).where(WarehouseFloor.floor_code == "3F")
            )
            assert admin is not None and floor is not None
            db.add(
                WarehouseArea(
                    floor_id=floor.id,
                    area_code="D1",
                    area_name="D1 区",
                    construction_status="enabled",
                    planned_location_count=2,
                )
            )
            db.add_all(
                [
                    WarehouseLocation(
                        location_code=f"D1-L0{serial}",
                        location_name=f"D1-L0{serial}",
                        warehouse_type="finished",
                        is_active=True,
                        warehouse_floor=3,
                        area_code="D1",
                        storage_type=storage_type,
                        source_version="V11",
                        placement_status="placed",
                    )
                        for serial, storage_type in enumerate(
                            ("ground", "rack"), start=1
                        )
                ]
            )
            db.commit()

            result = warehouse_api.get_warehouse_twin_floor_layout("3F", db, admin)
            projected = next(
                item for item in result["features"] if item["id"] == "zone-f1"
            )
            assert projected["formal_binding_source"] == "formal_area_code"
            assert projected["allowed_inventory_types"] == ["finished"]
            assert projected["storage_layout"] == "mixed"
            assert projected["employee_area_name"] == "右区D1"
    finally:
        engine.dispose()


def test_formal_area_code_projection_fails_closed_without_unique_measured_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    duplicate = dict(document['floors']['3F']['features'][0])
    duplicate['id'] = 'zone-f1-duplicate'
    duplicate['feature_code'] = 'ZONE-3F-ERP-F1-DUPLICATE'
    document['floors']['3F']['features'].append(duplicate)
    document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None
            assert floor is not None
            db.add(
                WarehouseArea(
                    floor_id=floor.id,
                    area_code='F1',
                    area_name='F1 不可猜测区域',
                    construction_status='enabled',
                    planned_location_count=1,
                    planned_pallet_capacity=1,
                    capacity_review_status='confirmed',
                    capacity_eligible=True,
                    confirmed_pallet_capacity=1,
                    capacity_reviewed_by='现场管理员',
                    capacity_reviewed_at=datetime(2026, 8, 14, 10, 30),
                )
            )
            db.add(
                WarehouseLocation(
                    location_code='F1-LEGACY-AMBIGUOUS',
                    location_name='F1 身份歧义位',
                    warehouse_type='finished',
                    is_active=True,
                    warehouse_floor=3,
                    area_code='F1',
                    storage_type='ground',
                    source_version='V11',
                    placement_status='placed',
                )
            )
            db.commit()
            monkeypatch.setattr(
                warehouse_api,
                'load_warehouse_twin_floor',
                lambda _floor_code: json.loads(published.read_text(encoding='utf-8'))[
                    'floors'
                ]['3F'],
            )

            result = warehouse_api.get_warehouse_twin_floor_layout('3F', db, admin)
            ambiguous = [
                item for item in result['features']
                if item.get('erp_area_code') == 'F1'
            ]
            assert len(ambiguous) == 2
            assert all('formal_area_id' not in item for item in ambiguous)
            assert all('formal_binding_status' not in item for item in ambiguous)
    finally:
        engine.dispose()


def test_one_step_confirmed_capacity_above_standard_fit_creates_logical_positions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    _add_floor_one_to_published_layout(published)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            db.add(
                WarehouseFloor(
                    floor_code='F1',
                    floor_name='一楼',
                    floor_number=1,
                    construction_status='enabled',
                    planning_reference_pallet_capacity=0,
                )
            )
            db.commit()
            result = warehouse_api.confirm_twin_zone_area(
                '1F',
                'zone-1f',
                _confirm_area_payload(
                    revision=_revision(published, '1F'),
                    operation_key='p1-60-capacity-over-measured-slots',
                    capacity=49,
                    area_code='FIN-001',
                    area_name='一楼成品栈板区',
                ),
                _request(),
                db,
                admin,
            )
            assert result['created_location_count'] == 49
            assert result['available_location_count'] == 0
            assert result['ground_plan_status'] == 'planning_only'
            assert '不能用于收料、入库或移位' in result['location_readiness_issue']
            assert '可移动空货位' not in result['message']
            assert draft.exists()
            assert Path(editor.TWIN_LAYOUT_PATH).exists()
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 1
            assert db.scalar(select(func.count(WarehouseLocation.id))) == 49
            assert all(
                row.placement_status == 'placed'
                for row in db.scalars(select(WarehouseLocation)).all()
            )
            assert db.scalar(select(func.count(OperationLog.id))) > 0
    finally:
        engine.dispose()


def test_floor_one_pallet_area_confirmation_creates_real_empty_move_locations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    _add_floor_one_to_published_layout(published)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            db.add(
                WarehouseFloor(
                    floor_code='F1',
                    floor_name='一楼',
                    floor_number=1,
                    construction_status='enabled',
                    planning_reference_pallet_capacity=0,
                )
            )
            db.commit()
            result = warehouse_api.confirm_twin_zone_area(
                '1F',
                'zone-1f',
                _confirm_area_payload(
                    revision=_revision(published, '1F'),
                    operation_key='p1-60-floor-one-real-empty-locations',
                    capacity=6,
                    area_code='FIN-001',
                    area_name='一楼成品栈板区',
                ),
                _request(),
                db,
                admin,
            )

            assert result['created_location_count'] == 6
            assert result['available_location_count'] == 6
            assert '已生成 6 个可移动空货位' in result['message']
            assert result['area']['planned_location_count'] == 6
            rows = list(
                db.scalars(
                    select(WarehouseLocation).order_by(WarehouseLocation.location_code)
                ).all()
            )
            assert [row.location_code for row in rows] == [
                f'F1-FIN-001-L{serial:03d}' for serial in range(1, 7)
            ]
            assert all(row.source_version == 'CURRENT_MAP' for row in rows)
            assert all(row.placement_status == 'placed' for row in rows)
            assert all(row.floor3_layout is not None for row in rows)
            assert all(row.warehouse_type == 'finished' for row in rows)
            assert all(row.storage_type == 'ground' for row in rows)
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 0
    finally:
        engine.dispose()


def test_one_step_confirmation_reuses_explicit_existing_area_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, _draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    document['floors']['3F']['features'][0].pop('erp_area_code', None)
    document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('optional production projection is unavailable')
        ),
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None and floor is not None
            existing = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2 原料区',
                planned_location_count=14,
                planned_pallet_capacity=15,
                construction_status='enabled',
                capacity_review_status='confirmed',
                capacity_eligible=True,
                confirmed_pallet_capacity=6,
                capacity_reviewed_by='现场管理员',
                capacity_reviewed_at=datetime(2026, 8, 13, 13, 19),
            )
            db.add(existing)
            db.commit()
            existing_id = existing.id

            result = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=_revision(published),
                    operation_key='p1-60-one-step-existing-a2',
                    usage='raw_material',
                    capacity=15,
                    area_code='A2',
                    area_name='A2 原料区',
                    existing_area_id=existing_id,
                ),
                _request(),
                db,
                admin,
            )

            assert result['area']['id'] == existing_id
            assert result['area']['area_code'] == 'A2'
            assert result['area']['confirmed_pallet_capacity'] == 15
            assert result['area']['storage_policy']['map_feature_id'] == 'zone-f1'
            assert result['area']['storage_policy']['allowed_inventory_types'] == ['raw_material']
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 1
    finally:
        engine.dispose()


def test_one_step_failure_restores_map_and_rolls_back_formal_area(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    published_before = published.read_bytes()
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            monkeypatch.setattr(
                warehouse_api,
                'validate_warehouse_twin_layout_draft',
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    editor.WarehouseTwinLayoutEditError('模拟校验失败')
                ),
            )
            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.confirm_twin_zone_area(
                    '3F',
                    'zone-f1',
                    _confirm_area_payload(
                        revision=_revision(published),
                        operation_key='p1-60-one-step-rollback',
                    ),
                    _request(),
                    db,
                    admin,
                )
            assert caught.value.status_code == 409
            assert '模拟校验失败' in str(caught.value.detail)
            assert published.read_bytes() == published_before
            assert not Path(editor.TWIN_LAYOUT_PATH).exists()
            assert not draft.exists()
            assert db.scalar(select(func.count(WarehouseArea.id))) == 0
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
            assert db.scalar(select(func.count(OperationLog.id))) == 0
    finally:
        engine.dispose()


def test_one_step_confirmation_consumes_selected_policy_only_advanced_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            area, policy = _bind_formal_area(db, admin=admin)
            area.planned_location_count = 12
            db.commit()
            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=_revision(published),
                    expected_version=1,
                    operation_key='p1-60-advanced-draft',
                    allowed_inventory_types=['finished'],
                    storage_layout='pallet_ground',
                    erp_area_code='F1',
                    area_name='三楼成品区高级草稿',
                    existing_area_id=area.id,
                ),
                _request(),
                db,
                admin,
            )
            assert draft.is_file()
            result = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=changed['revision'],
                    published_revision=_revision(published),
                    operation_key='p1-60-consume-selected-policy-draft',
                    existing_area_id=area.id,
                ),
                _request(),
                db,
                admin,
            )

            assert result['status'] == 'published'
            assert result['advanced_draft_preserved'] is False
            assert result['area']['storage_policy']['status'] == 'published'
            assert not draft.exists()
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 1
            assert result['available_location_count'] == 12
            assert result['area']['recorded_location_count'] == 12
    finally:
        engine.dispose()


def test_one_step_confirmation_preserves_unrelated_advanced_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    _add_floor_one_to_published_layout(published)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        changed = update_warehouse_twin_zone_geometry(
            '1F',
            'zone-1f',
            expected_revision=_revision(published, '1F'),
            expected_version=1,
            operation_key='p1-60-unrelated-advanced-draft',
            points=[[500, 500], [7_500, 500], [7_500, 7_500], [500, 7_500]],
        )
        advanced_before = json.loads(draft.read_text(encoding='utf-8'))
        assert changed.applied is True
        assert advanced_before['draft_meta']['status'] == 'draft'

        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            result = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=_revision(published),
                    operation_key='p1-60-preserve-unrelated-draft',
                ),
                _request(),
                db,
                admin,
            )

            assert result['status'] == 'published'
            assert result['advanced_draft_preserved'] is True
            assert result['area']['storage_policy']['status'] == 'published'
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 1

        live = json.loads(runtime.read_text(encoding='utf-8'))
        rebased = json.loads(draft.read_text(encoding='utf-8'))
        assert live['floors']['1F']['features'][0]['points'] != changed.value['points']
        assert rebased['floors']['1F']['features'][0]['points'] == changed.value['points']
        assert rebased['floors']['3F']['features'][0]['erp_area_code'] == 'F1'
        assert rebased['draft_meta']['status'] == 'draft'
        assert rebased['draft_meta']['base_published_sha256'] == sha256(
            runtime.read_bytes()
        ).hexdigest()
        assert rebased['draft_meta']['base_floor_revisions']['3F'] == live['floors']['3F']['revision']
    finally:
        engine.dispose()


def test_one_step_confirmation_preserves_same_floor_rack_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    original_revision = _revision(published)
    rack = editor.create_warehouse_twin_rack(
        '3F',
        expected_revision=original_revision,
        operation_key='p1-60-same-floor-rack-draft',
        area_feature_id='zone-f1',
        values={
            'name': '待确认货架',
            'width_mm': 2_000,
            'depth_mm': 1_000,
            'height_mm': 2_000,
            'x_mm': 1_000,
            'y_mm': 1_000,
            'levels': 2,
            'level_heights_mm': [1_000],
            'cargo_rows': 3,
            'level_cell_counts': [0, 0],
            'bays': 1,
            'rotation_deg': 0,
            'access_side': 'south',
            'min_aisle_width_mm': 1_500,
        },
    )
    assert rack.applied is True

    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=rack.floor_revision,
                    expected_version=1,
                    operation_key='p1-60-same-zone-policy-beside-rack',
                    allowed_inventory_types=['finished'],
                    storage_layout='rack',
                    erp_area_code='F1',
                    area_name='三楼成品货架区草稿',
                ),
                _request(),
                db,
                admin,
            )
            result = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=changed['revision'],
                    published_revision=original_revision,
                    operation_key='p1-60-publish-beside-rack-draft',
                ),
                _request(),
                db,
                admin,
            )
            assert result['advanced_draft_preserved'] is True
            assert result['area']['storage_policy']['status'] == 'published'

        live = json.loads(runtime.read_text(encoding='utf-8'))
        rebased = json.loads(draft.read_text(encoding='utf-8'))
        assert live['floors']['3F']['racks'] == []
        assert [item['id'] for item in rebased['floors']['3F']['racks']] == [
            rack.value['id']
        ]
        assert rebased['floors']['3F']['features'][0]['erp_area_code'] == 'F1'
        assert rebased['floors']['3F']['features'][0]['storage_layout'] == 'pallet_ground'
        assert rebased['floors']['3F']['features'][0]['formal_area_name'] != '三楼成品货架区草稿'
        assert rebased['draft_meta']['status'] == 'draft'
    finally:
        engine.dispose()


def test_two_areas_can_be_confirmed_in_sequence_while_an_advanced_draft_is_preserved(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    floor = document['floors']['3F']
    floor['features'][0]['points'] = [
        [0, 0], [5_000, 0], [5_000, 10_000], [0, 10_000]
    ]
    floor['features'][0]['area_mm2'] = 50_000_000
    floor['features'].append(
        {
            'id': 'zone-f2',
            'feature_code': 'ZONE-3F-ERP-F2',
            'name': 'F2',
            'feature_kind': 'zone',
            'subtype': 'rack_storage',
            'points': [
                [5_000, 0], [10_000, 0], [10_000, 10_000], [5_000, 10_000]
            ],
            'area_mm2': 50_000_000,
            'version': 1,
            'is_locked': False,
        }
    )
    floor['revision'] = _floor_revision(floor)
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    original_revision = _revision(published)
    rack = editor.create_warehouse_twin_rack(
        '3F',
        expected_revision=original_revision,
        operation_key='p1-60-sequential-area-rack-draft',
        area_feature_id='zone-f1',
        values={
            'name': '保留的高级货架草稿',
            'width_mm': 2_000,
            'depth_mm': 1_000,
            'height_mm': 2_000,
            'x_mm': 1_000,
            'y_mm': 1_000,
            'levels': 2,
            'level_heights_mm': [1_000],
            'cargo_rows': 3,
            'level_cell_counts': [0, 0],
            'bays': 1,
            'rotation_deg': 0,
            'access_side': 'south',
            'min_aisle_width_mm': 1_500,
        },
    )
    assert rack.applied is True

    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            first = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f1',
                _confirm_area_payload(
                    revision=rack.floor_revision,
                    published_revision=original_revision,
                    operation_key='p1-60-sequential-area-f1',
                    area_code='F1',
                    area_name='三楼 F1 成品区',
                ),
                _request(),
                db,
                admin,
            )
            assert first['advanced_draft_preserved'] is True

            effective = load_effective_warehouse_twin_floor_for_edit('3F')
            assert effective['draft_control']['has_draft'] is True
            assert effective['draft_control']['published_revision'] == first['published_revision']
            second = warehouse_api.confirm_twin_zone_area(
                '3F',
                'zone-f2',
                _confirm_area_payload(
                    revision=effective['revision'],
                    published_revision=effective['draft_control']['published_revision'],
                    operation_key='p1-60-sequential-area-f2',
                    capacity=8,
                    area_code='F2',
                    area_name='三楼 F2 成品区',
                ),
                _request(),
                db,
                admin,
            )
            assert second['status'] == 'published'
            assert second['advanced_draft_preserved'] is True
            assert second['area']['area_code'] == 'F2'
            assert second['area']['confirmed_pallet_capacity'] == 8
            assert db.scalar(select(func.count(WarehouseArea.id))) == 2
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 2
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == 'TWIN_ZONE_ONE_STEP_CONFIRM'
                )
            ) == 2

        live = json.loads(runtime.read_text(encoding='utf-8'))
        rebased = json.loads(draft.read_text(encoding='utf-8'))
        assert live['floors']['3F']['racks'] == []
        assert [row['id'] for row in rebased['floors']['3F']['racks']] == [
            rack.value['id']
        ]
        assert rebased['draft_meta']['base_floor_revisions']['3F'] == second[
            'published_revision'
        ]
    finally:
        engine.dispose()


def test_existing_unbound_area_is_reused_and_keeps_confirmed_capacity_on_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    document['floors']['3F']['features'][0].pop('erp_area_code', None)
    document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(warehouse_api, 'list_production_projection_mappings', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))['floors'][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None and floor is not None
            area = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2原料区',
                planned_location_count=14,
                planned_pallet_capacity=15,
                construction_status='enabled',
                capacity_review_status='confirmed',
                capacity_eligible=True,
                confirmed_pallet_capacity=6,
                capacity_reviewed_by='现场管理员',
                capacity_reviewed_at=datetime(2026, 8, 13, 13, 19),
            )
            db.add(area)
            db.commit()
            original_id = area.id

            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=_revision(published),
                    expected_version=1,
                    operation_key='p1-47b-existing-a2-draft',
                    allowed_inventory_types=['raw_material'],
                    storage_layout='pallet_ground',
                    erp_area_code='A2',
                    area_name='A2原料区',
                    existing_area_id=original_id,
                ),
                _request(),
                db,
                admin,
            )
            assert changed['formal_area']['id'] == original_id
            assert changed['item']['formal_area_id'] == original_id
            assert changed['item']['formal_binding_status'] == 'draft'
            assert changed['item']['formal_policy_status'] is None
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
            db.refresh(area)
            assert (area.capacity_review_status, area.confirmed_pallet_capacity) == ('confirmed', 6)
            saved_draft_feature = json.loads(draft.read_text(encoding='utf-8'))[
                'floors'
            ]['3F']['features'][0]
            assert saved_draft_feature['erp_area_code'] == 'A2'
            assert saved_draft_feature['formal_area_id'] == original_id
            assert saved_draft_feature['formal_floor_id'] == floor.id

            validated = warehouse_api.validate_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftValidatePayload(expected_revision=changed['revision']),
                _request(),
                db,
                admin,
            )
            assert validated['status'] == 'validated'
            result = warehouse_api.publish_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftPublishPayload(
                    expected_published_revision=_revision(published),
                    expected_draft_revision=changed['revision'],
                    operation_key='p1-47b-existing-a2-publish',
                ),
                _request(),
                db,
                admin,
            )
            assert result['applied'] is True
            db.expire_all()
            reused = db.get(WarehouseArea, original_id)
            assert reused is not None
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert reused.storage_policy is not None
            assert reused.storage_policy.map_feature_id == 'zone-f1'
            assert reused.storage_policy.status == 'published'
            assert (reused.capacity_review_status, reused.confirmed_pallet_capacity) == ('confirmed', 6)
    finally:
        engine.dispose()


def test_existing_area_id_mismatch_is_rejected_without_draft_or_formal_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    document['floors']['3F']['features'][0].pop('erp_area_code', None)
    document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None and floor is not None
            area = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2 现有未绑定区域',
                planned_location_count=8,
                planned_pallet_capacity=9,
                construction_status='enabled',
                capacity_review_status='pending',
                capacity_eligible=False,
            )
            db.add(area)
            db.commit()
            area_before = (
                area.id,
                area.floor_id,
                area.area_code,
                area.area_name,
                area.planned_location_count,
                area.planned_pallet_capacity,
                area.construction_status,
                area.capacity_review_status,
            )
            area_count_before = db.scalar(select(func.count(WarehouseArea.id)))
            policy_count_before = db.scalar(
                select(func.count(WarehouseAreaStoragePolicy.id))
            )
            assert not draft.exists()

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.update_twin_zone_storage_policy(
                    '3F',
                    'zone-f1',
                    warehouse_api.TwinZoneStoragePolicyPayload(
                        expected_revision=_revision(published),
                        expected_version=1,
                        operation_key='p1-47b-existing-area-identity-mismatch',
                        allowed_inventory_types=['finished'],
                        storage_layout='pallet_ground',
                        erp_area_code='B9',
                        area_name='B9 错误提交区域',
                        existing_area_id=area.id,
                    ),
                    _request(),
                    db,
                    admin,
                )

            assert caught.value.status_code == 409
            assert not draft.exists()
            db.expire_all()
            unchanged = db.get(WarehouseArea, area_before[0])
            assert unchanged is not None
            assert (
                unchanged.id,
                unchanged.floor_id,
                unchanged.area_code,
                unchanged.area_name,
                unchanged.planned_location_count,
                unchanged.planned_pallet_capacity,
                unchanged.construction_status,
                unchanged.capacity_review_status,
            ) == area_before
            assert db.scalar(select(func.count(WarehouseArea.id))) == area_count_before
            assert (
                db.scalar(select(func.count(WarehouseAreaStoragePolicy.id)))
                == policy_count_before
            )
    finally:
        engine.dispose()


def test_existing_unbound_area_code_requires_explicit_area_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    document['floors']['3F']['features'][0].pop('erp_area_code', None)
    document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None and floor is not None
            area = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2 现有未绑定区域',
                planned_location_count=8,
                planned_pallet_capacity=9,
                construction_status='enabled',
                capacity_review_status='pending',
                capacity_eligible=False,
            )
            db.add(area)
            db.commit()
            area_before = (
                area.id,
                area.floor_id,
                area.area_code,
                area.area_name,
                area.planned_location_count,
                area.planned_pallet_capacity,
                area.construction_status,
                area.capacity_review_status,
            )
            area_count_before = db.scalar(select(func.count(WarehouseArea.id)))
            policy_count_before = db.scalar(
                select(func.count(WarehouseAreaStoragePolicy.id))
            )
            log_count_before = db.scalar(select(func.count(OperationLog.id)))
            assert not draft.exists()

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.update_twin_zone_storage_policy(
                    '3F',
                    'zone-f1',
                    warehouse_api.TwinZoneStoragePolicyPayload(
                        expected_revision=_revision(published),
                        expected_version=1,
                        operation_key='p1-47b-existing-area-id-required',
                        allowed_inventory_types=['finished'],
                        storage_layout='pallet_ground',
                        erp_area_code='A2',
                        area_name='A2 现有未绑定区域',
                    ),
                    _request(),
                    db,
                    admin,
                )

            assert caught.value.status_code == 409
            assert not draft.exists()
            db.expire_all()
            unchanged = db.get(WarehouseArea, area_before[0])
            assert unchanged is not None
            assert (
                unchanged.id,
                unchanged.floor_id,
                unchanged.area_code,
                unchanged.area_name,
                unchanged.planned_location_count,
                unchanged.planned_pallet_capacity,
                unchanged.construction_status,
                unchanged.capacity_review_status,
            ) == area_before
            assert db.scalar(select(func.count(WarehouseArea.id))) == area_count_before
            assert (
                db.scalar(select(func.count(WarehouseAreaStoragePolicy.id)))
                == policy_count_before
            )
            assert db.scalar(select(func.count(OperationLog.id))) == log_count_before
    finally:
        engine.dispose()


def test_existing_area_identity_drift_is_blocked_before_formal_map_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    document['floors']['3F']['features'][0].pop('erp_area_code', None)
    document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None and floor is not None
            original = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2 原始未绑定区域',
                planned_location_count=0,
                planned_pallet_capacity=7,
                construction_status='enabled',
                capacity_review_status='pending',
                capacity_eligible=False,
            )
            db.add(original)
            db.commit()
            original_id = original.id
            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=_revision(published),
                    expected_version=1,
                    operation_key='p1-47b-existing-a2-drift-draft',
                    allowed_inventory_types=['finished'],
                    storage_layout='pallet_ground',
                    erp_area_code='A2',
                    area_name='A2 原始未绑定区域',
                    existing_area_id=original_id,
                ),
                _request(),
                db,
                admin,
            )
            assert changed['item']['formal_area_id'] == original_id
            # Seed the identity fields expected from a correct save so this test
            # independently exercises validation/publish drift protection.
            draft_document = json.loads(draft.read_text(encoding='utf-8'))
            draft_floor = draft_document['floors']['3F']
            draft_feature = draft_floor['features'][0]
            draft_feature['formal_area_id'] = original_id
            draft_feature['formal_floor_id'] = floor.id
            draft_floor['revision'] = _floor_revision(draft_floor)
            draft.write_text(
                json.dumps(
                    draft_document,
                    ensure_ascii=False,
                    separators=(',', ':'),
                ),
                encoding='utf-8',
            )
            seeded_revision = draft_floor['revision']

            original.area_code = 'A2-HISTORY'
            replacement = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2 后建同码区域',
                planned_location_count=0,
                planned_pallet_capacity=99,
                construction_status='enabled',
                capacity_review_status='pending',
                capacity_eligible=False,
            )
            db.add(replacement)
            db.commit()
            replacement_id = replacement.id
            published_before = published.read_bytes()
            runtime_before = runtime.read_bytes() if runtime.exists() else None

            blocked_at = None
            try:
                validated = warehouse_api.validate_twin_layout_draft(
                    '3F',
                    warehouse_api.TwinLayoutDraftValidatePayload(
                        expected_revision=seeded_revision
                    ),
                    _request(),
                    db,
                    admin,
                )
            except warehouse_api.HTTPException as error:
                assert error.status_code == 409
                blocked_at = 'validate'
            else:
                with pytest.raises(warehouse_api.HTTPException) as caught:
                    warehouse_api.publish_twin_layout_draft(
                        '3F',
                        warehouse_api.TwinLayoutDraftPublishPayload(
                            expected_published_revision=_revision(published),
                            expected_draft_revision=validated.get(
                                'revision', seeded_revision
                            ),
                            operation_key='p1-47b-existing-a2-drift-publish',
                        ),
                        _request(),
                        db,
                        admin,
                    )
                assert caught.value.status_code == 409
                blocked_at = 'publish'

            assert blocked_at in {'validate', 'publish'}
            assert published.read_bytes() == published_before
            assert (runtime.read_bytes() if runtime.exists() else None) == runtime_before
            db.expire_all()
            original_after = db.get(WarehouseArea, original_id)
            replacement_after = db.get(WarehouseArea, replacement_id)
            assert original_after is not None and replacement_after is not None
            assert original_after.area_code == 'A2-HISTORY'
            assert replacement_after.area_code == 'A2'
            assert original_after.storage_policy is None
            assert replacement_after.storage_policy is None
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 0
    finally:
        engine.dispose()


def test_publish_rechecks_validated_existing_area_identity_after_late_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    document['floors']['3F']['features'][0].pop('erp_area_code', None)
    document['floors']['3F']['revision'] = _floor_revision(document['floors']['3F'])
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        'load_warehouse_twin_floor',
        lambda floor_code: json.loads(runtime.read_text(encoding='utf-8'))[
            'floors'
        ][floor_code.upper()],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == '3F'))
            assert admin is not None and floor is not None
            original = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2 已选正式区域',
                planned_location_count=0,
                planned_pallet_capacity=7,
                construction_status='enabled',
                capacity_review_status='pending',
                capacity_eligible=False,
            )
            db.add(original)
            db.commit()
            original_id = original.id

            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                warehouse_api.TwinZoneStoragePolicyPayload(
                    expected_revision=_revision(published),
                    expected_version=1,
                    operation_key='p1-47b-existing-a2-late-drift-draft',
                    allowed_inventory_types=['finished'],
                    storage_layout='pallet_ground',
                    erp_area_code='A2',
                    area_name='A2 已选正式区域',
                    existing_area_id=original_id,
                ),
                _request(),
                db,
                admin,
            )
            assert changed['item']['formal_area_id'] == original_id
            assert changed['item']['formal_floor_id'] == floor.id

            validated = warehouse_api.validate_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftValidatePayload(
                    expected_revision=changed['revision']
                ),
                _request(),
                db,
                admin,
            )
            assert validated['status'] == 'validated'
            validated_draft_before = draft.read_bytes()
            published_before = published.read_bytes()
            runtime_before = runtime.read_bytes() if runtime.exists() else None

            original.area_code = 'A2-HISTORY'
            replacement = WarehouseArea(
                floor_id=floor.id,
                area_code='A2',
                area_name='A2 同码替身区域',
                planned_location_count=0,
                planned_pallet_capacity=99,
                construction_status='enabled',
                capacity_review_status='pending',
                capacity_eligible=False,
            )
            db.add(replacement)
            db.commit()
            replacement_id = replacement.id
            area_count_before_publish = db.scalar(select(func.count(WarehouseArea.id)))
            policy_count_before_publish = db.scalar(
                select(func.count(WarehouseAreaStoragePolicy.id))
            )

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.publish_twin_layout_draft(
                    '3F',
                    warehouse_api.TwinLayoutDraftPublishPayload(
                        expected_published_revision=_revision(published),
                        expected_draft_revision=validated.get(
                            'draft_revision', changed['revision']
                        ),
                        operation_key='p1-47b-existing-a2-late-drift-publish',
                    ),
                    _request(),
                    db,
                    admin,
                )

            assert caught.value.status_code == 409
            assert published.read_bytes() == published_before
            assert (runtime.read_bytes() if runtime.exists() else None) == runtime_before
            assert draft.read_bytes() == validated_draft_before
            db.expire_all()
            original_after = db.get(WarehouseArea, original_id)
            replacement_after = db.get(WarehouseArea, replacement_id)
            assert original_after is not None and replacement_after is not None
            assert original_after.area_code == 'A2-HISTORY'
            assert replacement_after.area_code == 'A2'
            assert original_after.storage_policy is None
            assert replacement_after.storage_policy is None
            assert (
                db.scalar(select(func.count(WarehouseArea.id)))
                == area_count_before_publish
            )
            assert (
                db.scalar(select(func.count(WarehouseAreaStoragePolicy.id)))
                == policy_count_before_publish
            )
    finally:
        engine.dispose()


def test_mold_rack_publish_reassigns_invalid_positions_once_and_rolls_back_on_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import mold_location

    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    floor = {
        'layout_id': 'layout-1f-mold-reassignment',
        'floor_code': '1F',
        'bounds_mm': {'min_x': 0, 'min_y': 0, 'max_x': 8_000, 'max_y': 8_000},
        'features': [],
        'racks': [
            {
                'id': 'rack-r01',
                'layout_id': 'layout-1f-mold-reassignment',
                'rack_code': 'RACK-1F-MOLD-R01-001',
                'area_feature_id': '',
                'area_code': 'ZONE-1F-MOLD-002',
                'status': 'confirmed',
                'is_locked': False,
                'version': 1,
                **_rack_values(levels=3, level_cell_counts=[0, 2, 2]),
            }
        ],
        'pallets': [],
    }
    floor['revision'] = _floor_revision(floor)
    document['floors']['1F'] = floor
    published.write_text(
        json.dumps(document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    published_revision = floor['revision']
    reduced = editor.update_warehouse_twin_rack(
        '1F',
        'rack-r01',
        expected_revision=published_revision,
        expected_version=1,
        operation_key='mold-rack-reduce-to-two-levels',
        values=_rack_values(levels=2, level_cell_counts=[0, 2]),
    )
    runtime = Path(editor.TWIN_LAYOUT_PATH)

    def runtime_floor(floor_code: str) -> dict:
        source = runtime if runtime.is_file() else published
        return json.loads(source.read_text(encoding='utf-8'))['floors'][floor_code.upper()]

    monkeypatch.setattr(warehouse_api, '_formal_area_identity_blockers', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(warehouse_api, '_formal_area_publish_blockers', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(warehouse_api, 'publish_floor_area_policies', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(warehouse_api, '_validate_published_area_layouts_for_floor', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(warehouse_api, 'load_warehouse_twin_floor', runtime_floor)
    monkeypatch.setattr(mold_location, 'load_warehouse_twin_floor', runtime_floor)

    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            db.add_all(
                [
                    MoldTool(
                        mold_code='MOLD-L3-TO-FIRST-GRID',
                        mold_name='第三层失效后归入首格',
                        rack_location='1F-M-R01-L3-G01',
                        location_version=1,
                        created_by=admin.id,
                    ),
                    MoldTool(
                        mold_code='MOLD-R04-RACK-ONLY',
                        mold_name='R04 靠墙特大模具区',
                        rack_location='1F-M-R04',
                        location_version=1,
                        created_by=admin.id,
                    ),
                ]
            )
            db.commit()

            validated = warehouse_api.validate_twin_layout_draft(
                '1F',
                warehouse_api.TwinLayoutDraftValidatePayload(
                    expected_revision=reduced.floor_revision
                ),
                _request(),
                db,
                admin,
            )
            assert validated['status'] == 'validated'
            assert any('R01 有1件模具' in warning for warning in validated['warnings'])
            reloaded_draft = editor.load_effective_warehouse_twin_floor_for_edit('1F')
            assert any(
                'R01 有1件模具' in warning
                for warning in reloaded_draft['draft_control']['warnings']
            )
            before_publish = db.scalar(
                select(MoldTool).where(MoldTool.mold_code == 'MOLD-L3-TO-FIRST-GRID')
            )
            assert before_publish is not None
            assert before_publish.rack_location == '1F-M-R01-L3-G01'
            assert db.scalar(select(func.count(MoldLocationMovement.id))) == 0

            payload = warehouse_api.TwinLayoutDraftPublishPayload(
                expected_published_revision=published_revision,
                expected_draft_revision=reduced.floor_revision,
                operation_key='mold-rack-publish-auto-first-grid',
            )
            result = warehouse_api.publish_twin_layout_draft(
                '1F', payload, _request(), db, admin
            )
            assert result['applied'] is True
            assert result['mold_location_reassignment_count'] == 1
            assert result['mold_location_changed'] is True
            db.expire_all()
            moved = db.scalar(
                select(MoldTool).where(MoldTool.mold_code == 'MOLD-L3-TO-FIRST-GRID')
            )
            r04 = db.scalar(
                select(MoldTool).where(MoldTool.mold_code == 'MOLD-R04-RACK-ONLY')
            )
            movement = db.scalar(select(MoldLocationMovement))
            assert moved is not None and r04 is not None and movement is not None
            assert moved.rack_location == '1F-M-R01-L2-G01'
            assert moved.location_version == 2
            assert r04.rack_location == '1F-M-R04'
            assert r04.location_version == 1
            assert movement.from_location == '1F-M-R01-L3-G01'
            assert movement.to_location == '1F-M-R01-L2-G01'
            assert movement.source == 'layout_publish'
            published_options = mold_location.one_floor_mold_location_options(
                runtime_floor('1F')
            )
            published_r01 = next(
                option for option in published_options if option['rack_code'] == 'R01'
            )
            assert published_r01['levels'] == [
                {'level': 2, 'kind': 'flat', 'grid_count': 2, 'grids': [1, 2]}
            ]

            replay = warehouse_api.publish_twin_layout_draft(
                '1F', payload, _request(), db, admin
            )
            assert replay['applied'] is False
            assert replay['mold_location_reassignment_count'] == 1
            assert db.scalar(select(func.count(MoldLocationMovement.id))) == 1

            current_floor = runtime_floor('1F')
            second = editor.update_warehouse_twin_rack(
                '1F',
                'rack-r01',
                expected_revision=current_floor['revision'],
                expected_version=2,
                operation_key='mold-rack-reduce-to-rack-only',
                values=_rack_values(levels=1, level_cell_counts=[0]),
            )
            second_validation = warehouse_api.validate_twin_layout_draft(
                '1F',
                warehouse_api.TwinLayoutDraftValidatePayload(
                    expected_revision=second.floor_revision
                ),
                _request(),
                db,
                admin,
            )
            assert second_validation['status'] == 'validated'
            runtime_before_failure = runtime.read_bytes()
            draft_before_failure = draft.read_bytes()

            def fail_mold_move(*_args, **_kwargs):
                raise mold_location.MoldLocationError(
                    '模拟模具移动失败', status_code=409
                )

            monkeypatch.setattr(warehouse_api, 'confirm_mold_location_move', fail_mold_move)
            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.publish_twin_layout_draft(
                    '1F',
                    warehouse_api.TwinLayoutDraftPublishPayload(
                        expected_published_revision=current_floor['revision'],
                        expected_draft_revision=second.floor_revision,
                        operation_key='mold-rack-publish-rollback-on-move-failure',
                    ),
                    _request(),
                    db,
                    admin,
                )
            assert caught.value.status_code == 409
            assert runtime.read_bytes() == runtime_before_failure
            assert draft.read_bytes() == draft_before_failure
            db.expire_all()
            rolled_back = db.scalar(
                select(MoldTool).where(MoldTool.mold_code == 'MOLD-L3-TO-FIRST-GRID')
            )
            assert rolled_back is not None
            assert rolled_back.rack_location == '1F-M-R01-L2-G01'
            assert rolled_back.location_version == 2
            assert db.scalar(select(func.count(MoldLocationMovement.id))) == 1
    finally:
        engine.dispose()


def test_new_occupancy_after_validation_is_rechecked_before_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = Path(editor.TWIN_LAYOUT_PATH)
    monkeypatch.setattr(
        warehouse_api,
        'list_production_projection_mappings',
        lambda *_args, **_kwargs: [],
    )
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            area, policy = _bind_formal_area(db, admin=admin)
            area.construction_status = 'enabled'
            policy.status = 'published'
            policy.published_map_revision = _revision(published)
            db.commit()
            changed = warehouse_api.update_twin_zone_storage_policy(
                '3F',
                'zone-f1',
                _change_policy_payload(
                    revision=_revision(published),
                    version=1,
                    operation_key='p1-47b-late-occupancy-draft',
                ),
                _request(),
                db,
                admin,
            )
            validated = warehouse_api.validate_twin_layout_draft(
                '3F',
                warehouse_api.TwinLayoutDraftValidatePayload(
                    expected_revision=changed['revision']
                ),
                _request(),
                db,
                admin,
            )
            assert validated['status'] == 'validated'
            location = WarehouseLocation(
                location_code='F1-LATE-OCCUPANCY-001',
                location_name='发布前新占用库位',
                warehouse_type='finished',
                is_active=False,
                warehouse_floor=3,
                area_code='F1',
                storage_type='ground',
                source_version='V11',
                placement_status='placed',
            )
            db.add(location)
            db.flush()
            lot = InventoryLot(
                lot_number='P1-47B-LATE-LOT-001',
                inventory_type='finished',
                warehouse_location_id=location.id,
                quantity_available=5,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit='boxes',
                status='active',
                source_type='stocktake',
                stock_date=date(2026, 8, 12),
                stock_date_accuracy='exact',
                last_movement_at=datetime(2026, 8, 12, 12, 0),
                version=1,
            )
            db.add(lot)
            db.commit()
            draft_before = draft.read_bytes()
            published_before = published.read_bytes()
            runtime_before = runtime.read_bytes() if runtime.exists() else None
            database_before = _formal_snapshot(
                db, policy_id=policy.id, location_id=location.id
            )

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.publish_twin_layout_draft(
                    '3F',
                    warehouse_api.TwinLayoutDraftPublishPayload(
                        expected_published_revision=_revision(published),
                        expected_draft_revision=changed['revision'],
                        operation_key='p1-47b-late-occupancy-publish',
                    ),
                    _request(),
                    db,
                    admin,
                )

            assert caught.value.status_code == 409
            assert '库存' in str(caught.value.detail)
            assert draft.read_bytes() == draft_before
            assert published.read_bytes() == published_before
            assert (runtime.read_bytes() if runtime.exists() else None) == runtime_before
            db.expire_all()
            assert _formal_snapshot(
                db, policy_id=policy.id, location_id=location.id
            ) == database_before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    'partial_field',
    ('erp_area_code', 'allowed_inventory_types', 'storage_layout', 'formal_area_name'),
)
def test_legacy_policy_fallback_requires_all_json_policy_fields_absent(
    tmp_path: Path,
    partial_field: str,
) -> None:
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            area, policy = _bind_formal_area(db, admin=admin)
            area.construction_status = 'enabled'
            policy.status = 'published'
            policy.published_map_revision = 'legacy-revision'
            db.commit()
            policy_before = _formal_snapshot(db, policy_id=policy.id)
            legacy_feature = {
                'id': 'zone-f1',
                'feature_code': 'ZONE-3F-ERP-F1',
                'name': 'F1 legacy zone',
                'feature_kind': 'zone',
            }

            republished = warehouse_api.publish_floor_area_policies(
                db,
                floor_code='3F',
                published_revision='legacy-next-revision',
                operator_id=admin.id,
                published_features=[legacy_feature],
            )
            assert republished == [policy]
            assert policy.published_map_revision == 'legacy-next-revision'
            assert policy.version == policy_before['policy'][0] + 1
            db.rollback()
            db.expire_all()
            assert _formal_snapshot(db, policy_id=policy.id) == policy_before

            partial_feature = dict(legacy_feature)
            partial_feature[partial_field] = {
                'erp_area_code': 'F1',
                'allowed_inventory_types': ['finished'],
                'storage_layout': 'pallet_ground',
                'formal_area_name': 'F1 partial name',
            }[partial_field]
            with pytest.raises(WarehouseAreaActivationError) as caught:
                warehouse_api.publish_floor_area_policies(
                    db,
                    floor_code='3F',
                    published_revision='partial-json-revision',
                    operator_id=admin.id,
                    published_features=[partial_feature],
                )
            assert caught.value.status_code == 409
            assert '策略不完整' in str(caught.value)
            db.rollback()
            db.expire_all()
            assert _formal_snapshot(db, policy_id=policy.id) == policy_before
    finally:
        engine.dispose()


def test_zone_geometry_uses_a_versioned_draft_and_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    published_before = sha256(published.read_bytes()).hexdigest()
    revision = _revision(published)
    points = [[1_000, 1_000], [9_000, 1_000], [9_000, 6_000], [1_000, 6_000]]

    changed = update_warehouse_twin_zone_geometry(
        "3F",
        "zone-f1",
        expected_revision=revision,
        expected_version=1,
        operation_key="p1-47b-zone-geometry-0001",
        points=points,
    )

    assert changed.applied is True
    assert changed.value["points"] == points
    assert changed.value["area_mm2"] == 40_000_000
    assert changed.value["status"] == "candidate"
    assert changed.value["version"] == 2
    assert draft.is_file()
    assert sha256(published.read_bytes()).hexdigest() == published_before

    retried = update_warehouse_twin_zone_geometry(
        "3F",
        "zone-f1",
        expected_revision=revision,
        expected_version=1,
        operation_key="p1-47b-zone-geometry-0001",
        points=points,
    )
    assert retried.applied is False
    assert retried.value == changed.value

    effective = load_effective_warehouse_twin_floor_for_edit("3F")
    feature = next(item for item in effective["features"] if item["id"] == "zone-f1")
    assert feature["points"] == points
    assert effective["draft_control"]["has_draft"] is True


@pytest.mark.parametrize(
    "points",
    (
        [[0, 0], [1, 1]],
        [[0, 0], [1, 1], [2, 2]],
        [[0, 0], [10_000_001, 0], [0, 1]],
        [[0, 0], [float("nan"), 1], [0, 1]],
    ),
)
def test_invalid_zone_geometry_fails_before_creating_a_draft(
    tmp_path: Path,
    monkeypatch,
    points: list[list[float]],
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    published_before = sha256(published.read_bytes()).hexdigest()

    with pytest.raises(WarehouseTwinLayoutEditError):
        update_warehouse_twin_zone_geometry(
            "3F",
            "zone-f1",
            expected_revision=_revision(published),
            expected_version=1,
            operation_key="p1-47b-zone-invalid-0001",
            points=points,
        )

    assert not draft.exists()
    assert sha256(published.read_bytes()).hexdigest() == published_before


def test_stale_or_locked_geometry_fails_without_a_half_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    published_before = sha256(published.read_bytes()).hexdigest()
    points = [[0, 0], [8_000, 0], [8_000, 8_000], [0, 8_000]]

    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        update_warehouse_twin_zone_geometry(
            "3F",
            "zone-f1",
            expected_revision="stale-revision",
            expected_version=1,
            operation_key="p1-47b-zone-stale-0001",
            points=points,
        )
    assert not draft.exists()

    published, draft = _isolate_layout_paths(tmp_path, monkeypatch, locked=True)
    published_before = sha256(published.read_bytes()).hexdigest()
    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        update_warehouse_twin_zone_geometry(
            "3F",
            "zone-f1",
            expected_revision=_revision(published),
            expected_version=1,
            operation_key="p1-47b-zone-locked-0001",
            points=points,
        )

    assert not draft.exists()
    assert sha256(published.read_bytes()).hexdigest() == published_before


def test_layout_mutation_api_wrappers_share_one_transaction_lock() -> None:
    api_source = (ROOT / 'app' / 'api' / 'warehouse.py').read_text(encoding='utf-8')
    for endpoint_name in (
        'publish_twin_layout_draft',
        'validate_twin_layout_draft',
        'discard_twin_layout_draft',
        'update_twin_zone_geometry',
        'update_twin_zone_storage_policy',
    ):
        marker = f'def {endpoint_name}('
        start = api_source.index(marker)
        next_function = api_source.find('\ndef ', start + len(marker))
        source = api_source[start:next_function if next_function >= 0 else None]
        assert 'with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:' in source


def test_new_area_draft_frontend_defers_location_planning_until_publish() -> None:
    assert '策略已保存为管理员草稿；请先校验并发布建立正式区域，再规划库位。' in TWIN_SOURCE
    assert 'selectedAreaCreatesInventoryLocations && selectedAreaHasFormalLedger' in TWIN_SOURCE
    assert 'selectedAreaCode && !selectedAreaHasFormalLedger' in TWIN_SOURCE
    assert '区域策略仍是管理员草稿；请先校验并发布建立正式区域，再规划正式库位。' in TWIN_SOURCE


def test_p1_47b_frontend_exposes_admin_planning_without_leaking_drafts_to_lookup() -> None:
    assert "区域规划" in TWIN_SOURCE
    assert 'title="P1-47B 独立阶段启用"' not in TWIN_SOURCE
    assert "canEditLocations" in TWIN_SOURCE
    assert "returnToLookupMode" in TWIN_SOURCE
    assert "refreshPublishedTwinFloor" in TWIN_SOURCE
    assert "/api/warehouse/twin-layout/floors/${floorCode}/draft`" in TWIN_SOURCE
    assert "/zones/${selectedAreaFeature.id}/geometry" in TWIN_SOURCE
    assert "容量待复核" in TWIN_SOURCE
    assert "confirmed_pallet_capacity" in TWIN_SOURCE
    assert 'floorCode === "3F"\n          ? `/api/warehouse/floor3/layout/areas/' not in TWIN_SOURCE
