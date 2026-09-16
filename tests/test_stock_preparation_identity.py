import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_stock_replenishment_flow import stock_replenishment_app
from test_stock_preparation_disposition import arranged
from app.api.stock_preparation import router
from app.models.product import Product
from app.models.user import User
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot
from app.services.stock_preparation_identity import confirm_legacy_group, confirmation_key
from app.services.warehouse_inventory import WarehouseInventoryError


def test_explicit_old_identity_keeps_inputs_and_requires_actual_assembly(stock_replenishment_app):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        pid, body = arranged(app, factory, client, 'semi')
        url = '/api/production/stock-preparation/group-actions'
        result = client.post(url, json=body)
        assert result.status_code == 200, result.text
        with factory() as db:
            from sqlalchemy import text
            db.execute(text("CREATE TRIGGER test_commands_immutable BEFORE UPDATE ON stock_preparation_commands BEGIN SELECT RAISE(ABORT, 'commands immutable'); END"))
            jobs = list(db.scalars(select(Job).order_by(Job.id)))
            for job in jobs:
                snap = json.loads(job.product_snapshot)
                snap['preparation_group'].pop('parent_basis', None)
                job.product_snapshot = json.dumps(snap)
            parent = db.get(Product, pid)
            parent.version += 1
            db.commit()
            before = [(l.id, l.quantity_available, l.quantity_consumed, l.version)
                      for l in db.scalars(select(InventoryLot).order_by(InventoryLot.id))]
            members = [dict(job_id=j.id, job_version=j.version, lot_version=1,
                           output_version=db.get(InventoryLot, j.output_lot_id).version) for j in jobs]
        assembly = dict(action='assemble', operation_key='legacy-assemble-confirmed',
                        parent_id=pid, group_key=result.json()['group_key'], sets=3,
                        location_id=7, layout_version=1, jobs=members)
        blocked = client.post(url, json=assembly)
        assert blocked.status_code == 409 and '原配方' in blocked.text
        with factory() as db:
            jobs = list(db.scalars(select(Job).order_by(Job.id)))
            actor = db.scalar(select(User).where(User.role == 'admin'))
            kwargs = dict(jobs=jobs, parent_version=db.get(Product, pid).version,
                          job_versions={j.id:j.version for j in jobs}, actor=actor,
                          confirmed_same_product=True, reason='现场确认3长4短仍可组成同款')
            with pytest.raises(WarehouseInventoryError):
                confirm_legacy_group(db, **dict(kwargs, confirmed_same_product=False))
            with pytest.raises(WarehouseInventoryError):
                confirm_legacy_group(db, **dict(kwargs, parent_version=999))
            confirmed = confirm_legacy_group(db, **kwargs)
            assert confirm_legacy_group(db, **kwargs) == confirmed
            db.commit()
            assert before == [(l.id,l.quantity_available,l.quantity_consumed,l.version)
                              for l in db.scalars(select(InventoryLot).order_by(InventoryLot.id))]
            assert db.get(Command, confirmation_key(assembly['group_key']))
        success = client.post(url, json=assembly)
        assert success.status_code == 200, success.text
        assert client.post(url, json=assembly).json() == success.json()
        with factory() as db:
            jobs = list(db.scalars(select(Job)))
            assert sorted(db.get(InventoryLot,j.output_lot_id).quantity_available for j in jobs) == [6,8]
            parent = db.get(InventoryLot, success.json()['output_lot_id'])
            assert parent.quantity_available == 3
            actual = json.loads(parent.finished_detail.physical_basis_json)
            assert len(actual['assembly']) == 2
            assert parent.estimated_unit_cost_snapshot is not None


def test_unconfirmed_changed_recipe_stays_blocked(stock_replenishment_app):
    from app.models.product_bom import ProductBomComponent
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        pid, body = arranged(app, factory, client, 'semi')
        assert client.post('/api/production/stock-preparation/group-actions', json=body).status_code == 200
        with factory() as db:
            jobs = list(db.scalars(select(Job).order_by(Job.id)))
            for job in jobs:
                snap = json.loads(job.product_snapshot)
                snap['preparation_group'].pop('parent_basis', None)
                job.product_snapshot = json.dumps(snap)
            edge = db.scalar(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == pid))
            edge.quantity_per_set += 1
            db.flush()
            with pytest.raises(WarehouseInventoryError, match='每套用量'):
                confirm_legacy_group(db, jobs=jobs, parent_version=db.get(Product,pid).version,
                    job_versions={j.id:j.version for j in jobs}, actor=db.scalar(select(User).where(User.role=='admin')),
                    confirmed_same_product=True, reason='不能以确认绕过不同配方')


def test_destination_uses_surviving_output_not_exhausted_original(stock_replenishment_app):
    from app.services.stock_preparation import job_dict
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        _, body = arranged(app, factory, client, 'semi')
        assert client.post('/api/production/stock-preparation/group-actions', json=body).status_code == 200
        with factory() as db:
            job = db.scalar(select(Job).order_by(Job.id))
            old = db.get(InventoryLot, job.output_lot_id)
            quantity = old.quantity_available
            values = {c.name:getattr(old,c.name) for c in InventoryLot.__table__.columns
                      if c.name not in {'id','lot_number'}}
            moved = InventoryLot(**dict(values, lot_number='MOVED-OUTPUT-TEST', warehouse_location_id=6))
            old.quantity_available = 0
            db.add(moved); db.flush()
            result = job_dict(db,job)
            assert result['output_location_id'] == 6
            assert result['output_remaining'] == quantity
            assert result['output_locations'][0]['lot_id'] == moved.id
            assert result['output_locations'][0]['quantity'] == quantity
            assert result['output_available'] == 0  # write commands still use the original lot CAS
