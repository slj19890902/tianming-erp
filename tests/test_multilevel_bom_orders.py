from datetime import date
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.multilevel_bom import OrderBomGraph, OrderBomGraphProduct
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.multilevel_bom_orders import freeze_order_graph, read_order_graph
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from tests.test_multilevel_bom_plan import liner_graph


@pytest.fixture
def context(tmp_path):
    engine = create_sqlite_engine(tmp_path / "multilevel-order-test.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        graph = liner_graph()
        db.add(Customer(id=136, name="隔离测试客户"))
        actor = User(username="test-bom-admin", password_hash="unusable-test-only",
                     role="admin", real_name="测试管理员")
        db.add(actor)
        db.flush()
        db.add_all([Product(id=n.product_id, customer_id=n.customer_id,
            product_code="SHARED-CODE", customer_material_code="SHARED-CODE",
            product_name=n.name, unit=n.unit, version=n.version) for n in graph.nodes])
        db.flush()
        order = Order(order_number="ISOLATED-MULTILEVEL-1", customer_id=136, order_date=date(2026, 9, 9))
        db.add(order)
        db.flush()
        item = OrderItem(order_id=order.id, product_id=1, quantity=100,
                         unit_price=Decimal("5"), subtotal=Decimal("500"),
                         snapshot_product_name="纸盒")
        db.add(item)
        db.commit()
        yield db, actor, item, graph
        db.rollback()
    engine.dispose()


def test_freeze_persists_real_products_and_survives_master_rename(context):
    db, actor, item, graph = context
    freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    db.commit()
    assert db.scalar(select(func.count()).select_from(OrderBomGraphProduct)) == 4
    kit = db.get(Product, 2)
    kit.product_name = "后续新版内衬"
    kit.version += 1
    db.commit()
    frozen = read_order_graph(db, item.id)
    assert next(n for n in frozen.nodes if n.product_id == 2).name == "内衬"
    assert dict(plan_bom(frozen, 100).picking) == {1: 100, 2: 100}
    freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    assert db.scalar(select(func.count()).select_from(OrderBomGraph)) == 1


def test_existing_order_graph_cannot_be_replaced(context):
    db, actor, item, graph = context
    freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    changed = replace(graph, edges=graph.edges[:1] + (replace(graph.edges[1], quantity=3),) + graph.edges[2:])
    with pytest.raises(BomPlanError, match="不能用新配方覆盖"):
        freeze_order_graph(db, order_item_id=item.id, graph=changed, actor=actor)
    assert read_order_graph(db, item.id).edges == graph.edges


def test_stale_child_version_rolls_back_entire_freeze(context):
    db, actor, item, graph = context
    db.get(Product, 4).version += 1
    db.commit()
    with pytest.raises(BomPlanError, match="版本已变化"):
        freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    db.commit()
    assert db.get(OrderBomGraph, item.id) is None
    assert db.scalar(select(func.count()).select_from(OrderBomGraphProduct)) == 0


def test_snapshot_hash_and_identity_corruption_fail_closed(context):
    db, actor, item, graph = context
    freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    db.commit()
    row = db.get(OrderBomGraphProduct, (item.id, 2))
    row.product_version += 1
    db.flush()
    with pytest.raises(BomPlanError, match="身份校验失败"):
        read_order_graph(db, item.id)
    db.rollback()
    db.get(OrderBomGraph, item.id).document_json = "{}"
    with pytest.raises(BomPlanError, match="校验失败"):
        read_order_graph(db, item.id)


def test_frozen_child_cannot_be_physically_deleted(context):
    db, actor, item, graph = context
    freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text("DELETE FROM products WHERE id = 2"))
        db.flush()


def test_delivered_order_requires_separate_audited_conversion(context):
    db, actor, item, graph = context
    item.delivered_quantity = 1
    db.commit()
    with pytest.raises(BomPlanError, match="已有送货"):
        freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    assert db.get(OrderBomGraph, item.id) is None


def test_order_customer_cannot_be_changed_by_graph(context):
    db, actor, item, graph = context
    another = replace(graph, customer_id=137, nodes=tuple(replace(n, customer_id=137) for n in graph.nodes))
    with pytest.raises(BomPlanError, match="产品或客户不一致"):
        freeze_order_graph(db, order_item_id=item.id, graph=another, actor=actor)


def test_caller_rollback_removes_snapshot_and_identity_rows(context):
    db, actor, item, graph = context
    item_id = item.id
    freeze_order_graph(db, order_item_id=item_id, graph=graph, actor=actor)
    db.rollback()
    assert db.get(OrderBomGraph, item_id) is None
    assert db.scalar(select(func.count()).select_from(OrderBomGraphProduct)) == 0


def test_audit_failure_rolls_back_freeze_even_if_caller_catches_it(context, monkeypatch):
    db, actor, item, graph = context
    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr("app.services.multilevel_bom_orders.append_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        freeze_order_graph(db, order_item_id=item.id, graph=graph, actor=actor)
    db.commit()
    assert db.get(OrderBomGraph, item.id) is None
    assert db.scalar(select(func.count()).select_from(OrderBomGraphProduct)) == 0
