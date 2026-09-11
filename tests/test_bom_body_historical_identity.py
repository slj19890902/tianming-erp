"""An existing body remains tied to its original physical inventory stage."""
import pytest
import json
from sqlalchemy import select
from fastapi.testclient import TestClient

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources


@pytest.mark.parametrize("invalid_source", [None, -1])
def test_body_identity_survives_later_rule_without_body_stage(composite_requisition_app, _p181_published_map_identity, invalid_source):
    from app.models.warehouse_inventory import InventoryLot
    from app.models.order import OrderItem
    from app.models.user import User
    from app.services.multilevel_bom_body_inventory import stock_product_identity
    from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
    from app.services.multilevel_bom_rule_cutover import persist_reviewed_rule
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_receipts import own_output_lots
    from tests.test_multilevel_bom_master import save
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory, liner=True, body=True)
    with TestClient(app) as client:
        _login(client)
        source = purchase_sources(client, factory, material_id, snapshots)[0]
        fact = _freeze_receipt_fact(client, source, idempotency_key="body-history-price", unit_price="0.1234")
        assert fact.status_code == 200, fact.text
        receipt = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
            idempotency_key="body-history-receipt")
        assert receipt.status_code == 200, receipt.text
    with factory() as db:
        body = db.scalar(select(InventoryLot).where(InventoryLot.inventory_type == "assembly_body"))
        assert body is not None and body.quantity_available == 10
        before = {column.key: getattr(body, column.key) for column in body.__table__.columns}
        frozen = read_compiled_order_bom(db, 1)
        liner_id = next(edge.child_id for edge in frozen.graph.edges if edge.parent_id == 1)
        actor = db.get(User, 1)
        save(db, actor, 1, "manufactured", [(liner_id, 1, "accompany")])
        db.commit()
        review = review_current_rule_requirements(db, order_item_id=1, customer_id=1)
        # Test the historical reader only, not authorization to convert this
        # body into a finished carton under the new accompaniment rule.
        persist_reviewed_rule(db, review=review, item=db.get(OrderItem, 1), previous=None,
            expected_revision=0, reviewed_hash=review.checksum, request_hash="9" * 64,
            operation_key="isolated-body-history", actor=actor)
        db.commit()
        assert stock_product_identity(db, body) == (1, 1)
        assert {column.key: getattr(body, column.key) for column in body.__table__.columns} == before
        assert body.id not in {row.id for row in own_output_lots(db, 1)}
        assert body.finished_detail is None and body.quantity_reserved == 0
        from app.services.bom_subkits import SubkitError
        cost = json.loads(body.cost_snapshot_detail_json)
        cost["bom_snapshot_id"] = invalid_source
        body.cost_snapshot_detail_json = json.dumps(cost)
        with pytest.raises(SubkitError, match="来源"):
            stock_product_identity(db, body)
        db.rollback()
