import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.services.multilevel_bom_receipts import own_output_lots
from app.services.multilevel_bom_external_costs import receipt_output_cost
from app.services.bom_subkit_costs import source_cost, cost_slice
from app.services.bom_subkits import SubkitError
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from tests.test_multilevel_bom_external_receipts import prepare, receive, _login, _confirm
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


@pytest.mark.parametrize('tax_mode', ['tax_inclusive', 'tax_exclusive'])
def test_fractional_cost_conserves_purchase_line_across_receipts(purchase_app, _p181_published_map_identity, tax_mode):
    _seed_material_and_staging(purchase_app.state.session_factory)
    order_id, item_id, _ = prepare(purchase_app, stock_basis=3, purchase_basis=1)
    with purchase_app.state.session_factory() as db:
        price = db.scalar(select(ExternalPackagingPriceVersion).where(
            ExternalPackagingPriceVersion.external_product_id == purchase_app.state.fixture['not_frozen_product_id']))
        # Fixture contract is set before actual purchase confirmation.
        price.unit_price = Decimal('0.022222')
        price.tax_mode = tax_mode
        db.commit()
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        _confirm(client, order_id)
        with purchase_app.state.session_factory() as db:
            row = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            purchase_id, line_id = row.purchase_order_id, row.id
            assert row.purchase_quantity == 7 and row.line_amount == Decimal('0.16')
        first = receive(client, purchase_id, line_id, 'cost-first', 1)
        assert first.status_code == 200, first.text
        second = receive(client, purchase_id, line_id, 'cost-second', 6)
        assert second.status_code == 200, second.text
        with purchase_app.state.session_factory() as db:
            lots = [l for l in own_output_lots(db, item_id) if l.source_ref_type == 'bom_external_receipt']
            assert sorted(l.quantity_available + l.quantity_consumed for l in lots) == [3,18]
            costs = [receipt_output_cost(db, l.source_ref_id) for l in lots]
            assert sum((Decimal(c['capitalized_material_cost']) for c in costs), Decimal(0)) == Decimal('0.1600')
            assert all(c['tax_included'] == (tax_mode == 'tax_inclusive') for c in costs)
            assert all(c['currency'] == 'CNY' and Decimal(c['tax_rate']) == Decimal('0.13') for c in costs)
            for lot, detail in zip(lots, costs):
                total = Decimal(detail['capitalized_material_cost'])
                assert source_cost(db, lot, lot.quantity_available)[0] == cost_slice(
                    total, detail['quantity'], lot.quantity_consumed, lot.quantity_available)
                assert sum((cost_slice(total, detail['quantity'], i, 1) for i in range(detail['quantity'])), Decimal(0)) == total
            # Corrupting the stored provenance cannot silently fall back to an estimate.
            lot = lots[0]
            changed = json.loads(lot.cost_snapshot_detail_json)
            changed['sources'][0]['amount'] = '999'
            lot.cost_snapshot_detail_json = json.dumps(changed)
            with pytest.raises(SubkitError, match='身份不一致'):
                source_cost(db, lot, 1)
