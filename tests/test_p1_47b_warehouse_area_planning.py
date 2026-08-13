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
from app.models.mold_tool import MoldTool
from app.models.order import Order, OrderItem
from app.models.printing_plate import PrintingPlate
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.user import User
from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
    InventoryLot,
    InventoryPallet,
)
from app.services import warehouse_twin_layout_editor as editor
from app.services.warehouse_area_activation import WarehouseAreaActivationError
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


@pytest.mark.parametrize('occupancy_kind', ('location', 'lot', 'pallet'))
def test_formal_location_lot_or_pallet_blocks_incompatible_policy_without_changes(
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


def test_bound_area_mapping_store_unreadable_fails_closed_without_draft(
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

            with pytest.raises(warehouse_api.HTTPException) as caught:
                warehouse_api.update_twin_zone_storage_policy(
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
            assert caught.value.status_code == 409
            assert not draft.exists()
            db.expire_all()
            assert db.get(WarehouseAreaStoragePolicy, policy.id).version == version_before
            assert db.scalar(select(func.count(OperationLog.id))) == 0
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
            assert '启用中的正式库位' in str(caught.value.detail)
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


@pytest.mark.parametrize('failure_point', ('activation', 'audit', 'commit'))
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
                if failure_point == 'activation':
                    raise WarehouseAreaActivationError('activation failed', status_code=409)
                raise RuntimeError(f'{failure_point} failed')

            if failure_point == 'activation':
                monkeypatch.setattr(warehouse_api, 'publish_floor_area_policies', fail)
            elif failure_point == 'audit':
                monkeypatch.setattr(warehouse_api, '_twin_layout_asset_log', fail)
            else:
                monkeypatch.setattr(db, 'commit', fail)

            payload = warehouse_api.TwinLayoutDraftPublishPayload(
                expected_published_revision=_revision(published),
                expected_draft_revision=validated.value['draft_revision'],
                operation_key=f'p1-47b-publish-failure-{failure_point}',
            )
            expected_error = warehouse_api.HTTPException if failure_point == 'activation' else RuntimeError
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


def test_inactive_empty_location_policy_change_then_publish_keeps_identity_and_syncs_fields(
    tmp_path: Path,
    monkeypatch,
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
                is_active=False,
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
            assert restored_location.is_active is False
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


def test_cross_floor_edit_and_dirty_multi_floor_publish_are_rejected_atomically(
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
    changed_3f = editor.update_warehouse_twin_zone_geometry(
        '3F',
        'zone-f1',
        expected_revision=_revision(published, '3F'),
        expected_version=1,
        operation_key='p1-47b-cross-floor-3f',
        points=[[500, 500], [9_500, 500], [9_500, 8_000], [500, 8_000]],
    )
    draft_after_3f = draft.read_bytes()

    with pytest.raises(WarehouseTwinLayoutEditConflictError) as edit_conflict:
        editor.update_warehouse_twin_zone_geometry(
            '1F',
            'zone-1f',
            expected_revision=_revision(published, '1F'),
            expected_version=1,
            operation_key='p1-47b-cross-floor-1f',
            points=[[250, 250], [7_750, 250], [7_750, 7_500], [250, 7_500]],
        )
    assert '只能规划一个楼层' in str(edit_conflict.value)
    assert draft.read_bytes() == draft_after_3f

    dirty_document = json.loads(draft.read_text(encoding='utf-8'))
    floor_1f = dirty_document['floors']['1F']
    floor_1f['features'][0]['points'] = [
        [250, 250], [7_750, 250], [7_750, 7_500], [250, 7_500]
    ]
    floor_1f['features'][0]['area_mm2'] = 54_375_000
    floor_1f['features'][0]['version'] = 2
    floor_1f['revision'] = _floor_revision(floor_1f)
    dirty_document['draft_meta']['status'] = 'validated'
    dirty_document['draft_meta']['validated_at'] = '2026-08-12T00:00:00Z'
    dirty_document['draft_meta']['validated_floor_revisions'] = {
        code: floor['revision'] for code, floor in dirty_document['floors'].items()
    }
    draft.write_text(
        json.dumps(dirty_document, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    dirty_draft_before = draft.read_bytes()
    runtime_before = runtime.read_bytes() if runtime.exists() else None
    backups_before = _backup_manifest(backups)
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == 'p1-47b-admin'))
            assert admin is not None
            database_before = (
                db.scalar(select(func.count(WarehouseArea.id))),
                db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))),
                db.scalar(select(func.count(OperationLog.id))),
            )

            with pytest.raises(warehouse_api.HTTPException) as publish_conflict:
                warehouse_api.publish_twin_layout_draft(
                    '3F',
                    warehouse_api.TwinLayoutDraftPublishPayload(
                        expected_published_revision=_revision(published, '3F'),
                        expected_draft_revision=changed_3f.floor_revision,
                        operation_key='p1-47b-double-dirty-publish',
                    ),
                    _request(),
                    db,
                    admin,
                )

            assert publish_conflict.value.status_code == 409
            assert '只能发布一个楼层' in str(publish_conflict.value.detail)
            assert published.read_bytes() == published_before
            assert draft.read_bytes() == dirty_draft_before
            assert (runtime.read_bytes() if runtime.exists() else None) == runtime_before
            assert _backup_manifest(backups) == backups_before
            assert (
                db.scalar(select(func.count(WarehouseArea.id))),
                db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))),
                db.scalar(select(func.count(OperationLog.id))),
            ) == database_before
    finally:
        engine.dispose()


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
