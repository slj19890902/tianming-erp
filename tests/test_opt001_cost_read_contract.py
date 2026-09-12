"""Current cost evidence must not be replaced by legacy price fields."""
import json
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app.services.material_cost_supplement import _reference
from tests.test_phase11_requisition import requisition_app


def gap(source='inventory_confirmed_material', evidence=None):
    lot = NS(id=1, inventory_type='finished',
        finished_detail=NS(product_id=3, length_mm=800, width_mm=400, height_mm=300),
        estimated_unit_cost_snapshot=Decimal('4.25'), cost_snapshot_source=source,
        cost_snapshot_detail_json=json.dumps(evidence or {'currency':'CNY'}),
        cost_snapshot_at=None, source_ref_type='stocktake', source_ref_id=None)
    return dict(item=NS(product_id=3,order_item_id=None),
        source=dict(kind='unordered_inventory_allocation',lot=lot),
        reason='estimate_only',quantity=10)


def test_confirmed_entry_cost_is_valid_without_an_order():
    ref, error = _reference(None,gap())
    assert error is None
    assert ref['unit_cost']==Decimal('4.25')
    assert ref['reference_kind']=='lot_cost_snapshot'


def test_old_centimetre_snapshot_is_not_proposed_as_approved_cost():
    g=gap('material_quote_area',{'currency':'CNY','components':[
        {'component':'whole','length_mm':160,'width_mm':50,'pieces_per_box':1}]})
    ref,error=_reference(None,g)
    assert ref is None and '厘米' in error


@pytest.mark.parametrize(('source', 'evidence'), [
    ('inventory_confirmed_material', {'currency': 'USD'}),
    ('inventory_confirmed_material', {'not_currency': 'CNY'}),
    ('unverified_guess', {'currency': 'CNY'}),
])
def test_invalid_frozen_snapshot_does_not_fall_back_to_current_price(source, evidence):
    reference, error = _reference(None, gap(source, evidence))
    assert reference is None and error


def test_owner_reference_requires_authorization_and_retains_basis():
    g=gap('owner_current_reference_backfill',{'currency':'CNY'})
    assert _reference(None,g)[0] is None
    g=gap('owner_current_reference_backfill',{'currency':'CNY','authorization':'owner-approved-test',
        'basis':'current_reference_cost_not_historical_purchase_fact'})
    ref,error=_reference(None,g)
    assert error is None and ref['evidence']['original_detail']['authorization']=='owner-approved-test'


def test_overview_cost_contract_sums_month_report_and_redacts(monkeypatch):
    from app.services import material_cost_lineage as service
    calls=[]
    def report(db, *, month, visible_customer_ids=None):
        calls.append((month,visible_customer_ids))
        return dict(total_delivery_lines=10,management_covered_lines=8,management_uncovered_lines=2,
            actual_material_cost=Decimal('10.12'), supplemental_material_cost=Decimal('4.13'),
            management_material_cost=Decimal('14.25'))
    monkeypatch.setattr(service,'material_cost_coverage_report',report)
    result=service.material_cost_overview(None,months=['2026-08','2026-09'],can_view_costs=True,
        visible_customer_ids={7})
    assert calls==[('2026-08',{7}),('2026-09',{7})]
    assert result['covered_lines']==16 and result['total_lines']==20
    assert result['coverage_rate']==.8 and result['missing_lines']==4
    assert result['material_cost_amount']==Decimal('28.50')
    assert result['material_gross_profit_reference'] is None
    result=service.material_cost_overview(None,months=['2026-09'],can_view_costs=False)
    for key in ['material_cost_amount','actual_material_cost','supplemental_material_cost','covered_revenue','material_gross_profit_reference']:
        assert result[key] is None


def test_empty_overview_does_not_claim_zero_cost_profit(monkeypatch):
    from app.services import material_cost_lineage as service
    result=service.material_cost_overview(None,months=[],can_view_costs=True)
    assert result['total_lines']==0 and result['coverage_rate']==0
    assert result['material_gross_profit_reference'] is None


def test_rounded_full_percentage_never_hides_a_remaining_gap(monkeypatch):
    from pathlib import Path
    from app.services import material_cost_lineage as service
    monkeypatch.setattr(service, 'material_cost_coverage_report', lambda *args, **kwargs: dict(
        total_delivery_lines=20001, management_covered_lines=20000,
        actual_material_cost=Decimal('10'), supplemental_material_cost=Decimal('0'),
        management_material_cost=Decimal('10')))
    result = service.material_cost_overview(None, months=['2026-09'], can_view_costs=True)
    assert result['coverage_rate'] == 1
    assert result['missing_lines'] == 1
    html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')
    assert 'v-if="Number(financeOverview.cost_coverage.missing_lines)>0"' in html


def test_month_report_batches_order_reads_and_api_uses_same_contract(requisition_app):
    from datetime import date
    from fastapi.testclient import TestClient
    from sqlalchemy import event, select
    from app.models.access_control import UserPermissionOverride
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem
    from app.models.user import User
    from app.api.finance import router
    from app.services.material_cost_lineage import material_cost_overview
    from tests.test_phase11_requisition import _login

    app, factory = requisition_app
    app.include_router(router, prefix='/api/finance')
    with factory() as db:
        delivery = Delivery(delivery_number='OPT001-BATCH', customer_id=1,
                            delivery_date=date(2026, 9, 1), status='dispatched')
        db.add(delivery)
        db.flush()
        for index in range(25):
            order_item = OrderItem(order_id=1, product_id=1, quantity=1,
                                   unit_price=3, subtotal=3,
                                   snapshot_product_name=f'batch-{index}')
            db.add(order_item)
            db.flush()
            db.add(DeliveryItem(delivery_id=delivery.id, order_item_id=order_item.id,
                                source_type='order', delivered_quantity=1))
        db.commit()
    statements = []
    engine = factory.kw['bind']
    def capture(_connection, _cursor, statement, *args):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with factory() as db:
            overview = material_cost_overview(db, months=['2026-09'], can_view_costs=True)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    order_reads = [sql for sql in statements if 'FROM sales_order_items' in sql]
    assert len(order_reads) == 1
    assert overview['total_lines'] == overview['missing_lines'] == 25
    assert overview['covered_lines'] == 0
    with TestClient(app) as client:
        _login(client, 'admin')
        response = client.get('/api/finance/overview', params={'through_month': '2026-09'})
        assert response.status_code == 200, response.text
        result = response.json()['cost_coverage']
        assert result['total_lines'] == result['missing_lines'] == 25
        assert result['accounting_basis'] == 'delivery_date'
        assert result['material_gross_profit_reference'] is None
        with factory() as db:
            finance_id = db.scalar(select(User.id).where(User.username == 'finance'))
            db.add(UserPermissionOverride(user_id=finance_id, permission_code='cost.view', is_allowed=False))
            db.commit()
        _login(client, 'finance')
        response = client.get('/api/finance/overview', params={'through_month': '2026-09'})
        assert response.status_code == 200, response.text
        result = response.json()['cost_coverage']
        assert result['total_lines'] == 25
        for key in ['material_cost_amount', 'actual_material_cost', 'supplemental_material_cost',
                    'covered_revenue', 'material_gross_profit_reference']:
            assert result[key] is None
