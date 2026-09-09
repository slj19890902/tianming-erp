from decimal import Decimal
import pytest
from sqlalchemy import select, func

from app.models.product import Product
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.models.multilevel_bom import OrderBomExternalComponent
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.multilevel_bom_external_identity import bind_external_component, read_external_node
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_master import save


def prepare(db, actor, item):
    product = db.get(Product, 3)
    product.supply_mode = 'external_purchase'
    product.external_packaging_category_code = 'other_packaging'
    product.external_packaging_specification_json = '{}'
    product.external_packaging_specification_summary = '外购子件'
    product.external_packaging_purchase_unit = '片'
    product.external_packaging_candidate_snapshot_json = '[]'
    product.external_packaging_default_order_quantity_basis = 1
    product.external_packaging_default_purchase_quantity_basis = 3
    save(db, actor, 1, 'assembled', [(3, 2, 'assembly')])
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    row = SalesOrderItemExternalComponent(sales_order_item_id=item.id, source_kind='direct_product',
        source_component_set_version=product.version, display_order=1, purpose='外购子件',
        quantity_per_finished_unit=Decimal(6), waste_rate=Decimal(0), consumption_unit='片',
        is_required=True, category_code='other_packaging', specification_json='{}', specification_summary='规格')
    db.add(row)
    db.commit()
    return row


def test_bind_read_and_replay_preserve_exact_child_identity(context):
    db, actor, item, _ = context
    row = prepare(db, actor, item)
    link = bind_external_component(db, external_component_id=row.id, order_item_id=item.id, product_id=3, actor=actor)
    db.commit()
    assert read_external_node(db, row.id).product_id == 3
    assert bind_external_component(db, external_component_id=row.id, order_item_id=item.id, product_id=3, actor=actor).bom_snapshot_id == link.bom_snapshot_id
    assert db.scalar(select(func.count()).select_from(OrderBomExternalComponent)) == 1
    with pytest.raises(BomPlanError):
        bind_external_component(db, external_component_id=row.id, order_item_id=item.id, product_id=1, actor=actor)


@pytest.mark.parametrize('field,value', [('consumption_unit','箱'), ('quantity_per_finished_unit',Decimal('5.999999')),
    ('source_component_set_version',999), ('is_required',False), ('waste_rate',Decimal('0.1'))])
def test_mismatched_contract_never_binds(context, field, value):
    db, actor, item, _ = context
    row = prepare(db, actor, item)
    setattr(row, field, value)
    db.commit()
    with pytest.raises(BomPlanError):
        bind_external_component(db, external_component_id=row.id, order_item_id=item.id, product_id=3, actor=actor)
    assert db.scalar(select(func.count()).select_from(OrderBomExternalComponent)) == 0


def test_outer_rollback_does_not_commit_link(context):
    db, actor, item, _ = context
    row = prepare(db, actor, item)
    bind_external_component(db, external_component_id=row.id, order_item_id=item.id, product_id=3, actor=actor)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(OrderBomExternalComponent)) == 0


def test_audit_failure_rolls_back_binding(context, monkeypatch):
    db, actor, item, _ = context
    row = prepare(db, actor, item)
    def fail(*args, **kwargs):
        raise RuntimeError('audit-test-failure')
    monkeypatch.setattr('app.services.audit_log.append_audit_event', fail)
    with pytest.raises(RuntimeError, match='audit-test-failure'):
        bind_external_component(db, external_component_id=row.id, order_item_id=item.id, product_id=3, actor=actor)
    assert db.scalar(select(func.count()).select_from(OrderBomExternalComponent)) == 0


def test_inactive_actor_and_other_order_rejected(context):
    db, actor, item, _ = context
    row = prepare(db, actor, item)
    with pytest.raises(BomPlanError):
        bind_external_component(db, external_component_id=row.id, order_item_id=item.id+999, product_id=3, actor=actor)
    actor.is_active = False
    with pytest.raises(BomPlanError, match='操作人已失效'):
        bind_external_component(db, external_component_id=row.id, order_item_id=item.id, product_id=3, actor=actor)
    assert db.scalar(select(func.count()).select_from(OrderBomExternalComponent)) == 0
