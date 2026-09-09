import json
from decimal import Decimal

import pytest

from tests.test_multilevel_bom_body_inventory import setup, composite_requisition_app, _p181_published_map_identity
from app.services.multilevel_bom_body_inventory import receive_body_inventory
from app.services.bom_subkit_costs import source_cost
from app.services.bom_subkits import SubkitError
from app.models.multilevel_bom import BomBodyInventoryDetail


def test_body_actual_cost_uses_completion_identity_not_finished_detail(composite_requisition_app, _p181_published_map_identity):
    _, factory = composite_requisition_app
    cid, lid = setup(factory)
    with factory() as db:
        lot = receive_body_inventory(db, completion_id=cid, location_id=lid, operator_id=1,
            idempotency_key="body-cost", expected_layout_version=2)
        with pytest.raises(SubkitError):
            source_cost(db, lot, 1)
        lot.cost_snapshot_detail_json = json.dumps({"bom_material_product_id":1,
            "capitalized_material_cost":"1.2345", "currency":"CNY"})
        db.flush()
        amount, evidence = source_cost(db, lot, 2)
        assert amount == Decimal("0.4938")
        assert evidence["bom_material_product_id"] == 1
        assert source_cost(db, lot, 5)[0] == Decimal("1.2345")
        db.get(BomBodyInventoryDetail,lot.id).product_id = 2
        db.flush()
        with pytest.raises(SubkitError):
            source_cost(db, lot, 1)
