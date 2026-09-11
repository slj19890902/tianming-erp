import json
from dataclasses import replace
from decimal import Decimal

import pytest

from app.services.multilevel_bom_plan import BomPlanError, FrozenBom, ProductNode, PurchaseUnits
from app.services.multilevel_bom_snapshot import dump_graph, load_graph
from app.services.multilevel_bom_purchase_units import purchase_quantity_for_stock, cumulative_receipt_conversion


def node(stock='1', purchase='3', unit='片'):
    return ProductNode(1, 1, 1, '外购件', '套', 'purchased', (), PurchaseUnits(unit, stock, purchase))


def test_cumulative_batches_form_stock_only_when_complete():
    n = node()
    assert purchase_quantity_for_stock(n, 5) == 15
    assert cumulative_receipt_conversion(n, received_before=0, received_now=2) == (0, Decimal(2))
    assert cumulative_receipt_conversion(n, received_before=2, received_now=2) == (1, Decimal(1))
    assert cumulative_receipt_conversion(n, received_before=4, received_now=2) == (1, Decimal(0))


def test_discrete_purchase_rounds_up_without_multiplying_stock_twice():
    n = node('5', '1', '箱')
    assert purchase_quantity_for_stock(n, 6) == 2
    assert cumulative_receipt_conversion(n, received_before=0, received_now=2) == (10, Decimal(0))
    n = node('1', '0.123456', '米')
    assert purchase_quantity_for_stock(n, 3) == Decimal('0.370368')
    assert cumulative_receipt_conversion(n, received_before='0.1', received_now='0.023456') == (1, Decimal(0))


@pytest.mark.parametrize('value', [True, 0.1, '-1', 'NaN', 'Infinity', '0.0000001', '1e12'])
def test_bad_receipt_quantity_rejected(value):
    with pytest.raises(BomPlanError):
        cumulative_receipt_conversion(node(), received_before=0, received_now=value)


@pytest.mark.parametrize('value', ['0', '-1', 'NaN', '1e99', '0.0000001', True, 1.5, None])
def test_bad_frozen_ratio_rejected(value):
    with pytest.raises(BomPlanError):
        dump_graph(FrozenBom(1, 1, (node(stock=value),), ()))


def test_version_two_roundtrip_and_old_snapshot_no_invented_ratio():
    graph = FrozenBom(1, 1, (node(),), ())
    document = dump_graph(graph)
    assert json.loads(document)['schema_version'] == 2
    assert load_graph(document) == graph
    old = replace(graph, nodes=(replace(node(), purchase_units=None),))
    document = dump_graph(old)
    assert json.loads(document)['schema_version'] == 1
    assert 'purchase_units' not in json.loads(document)['nodes'][0]
    with pytest.raises(BomPlanError, match='未冻结'):
        purchase_quantity_for_stock(load_graph(document).nodes[0], 1)


def test_wrong_source_and_unknown_ratio_fields_fail_closed():
    graph = FrozenBom(1, 1, (node(),), ())
    data = json.loads(dump_graph(graph))
    data['nodes'][0]['purchase_units']['ignored'] = 1
    with pytest.raises(BomPlanError):
        load_graph(json.dumps(data))
    with pytest.raises(BomPlanError):
        dump_graph(replace(graph, nodes=(replace(node(), source='assembled'),)))
