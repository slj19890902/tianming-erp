import copy
import json
from types import SimpleNamespace

import pytest
from app.models.mold_tool import MoldTool
from app.services import stock_purchase_identity
from app.services.stock_completion_basis import completion_basis
from tests.test_inventory_cost_snapshot import db


def frozen_case(db, monkeypatch):
    mold = MoldTool(mold_code='FROZEN-DIE', mold_name='原模具', rack_location='R01', is_active=True)
    db.add(mold); db.flush()
    basis = json.dumps({'product_id': 42, 'mold_tool_id': str(mold.id), 'spec': None,
                       'unit': '只', 'material': 'TEST', 'assembly': []})
    fields = {'product_code': '000042', 'customer_id': 1, 'mold_tool_id': mold.id,
              'length_mm': None, 'width_mm': None, 'unit': '只',
              'sheet_cutting_settings': {'schema_version': 2, 'whole': {
                  'length_parts': 2, 'width_parts': 1, 'mold_count': 3, 'is_die_cut': True}}}
    identity = {'product_id': 42, 'customer_id': 1, 'fields': fields, 'physical_basis': basis}
    item = SimpleNamespace(sheet_cutting_snapshot=None, material_code_snapshot='TEST',
                           layer_count=3, flute_type='B')
    snapshot = {'product_id': 42, 'physical_basis': basis, 'purchase_identity': copy.deepcopy(identity)}
    monkeypatch.setattr(stock_purchase_identity, 'resolve', lambda *_args: identity)
    return mold, item, snapshot, identity


def test_frozen_registered_die_fills_description_without_dimensions_or_source_rewrite(db, monkeypatch):
    mold, item, snapshot, identity = frozen_case(db, monkeypatch)
    before = copy.deepcopy(snapshot)
    value, proof = completion_basis(db, item, snapshot)
    result = json.loads(value)
    assert '000042' in result['spec'] and '3模' in result['spec']
    assert '单片尺寸未登记' in result['spec']
    assert result['mold_tool_id'] == str(mold.id)
    assert snapshot == before and identity['physical_basis'] == before['physical_basis']
    assert not any(k in result for k in ('length_mm', 'width_mm'))
    assert proof['dimensions_invented'] is False


@pytest.mark.parametrize('invalid', ['inactive', 'different_identity', 'different_mold', 'no_cutting'])
def test_unproven_legacy_identity_stays_incomplete(db, monkeypatch, invalid):
    mold, item, snapshot, identity = frozen_case(db, monkeypatch)
    if invalid == 'inactive':
        mold.is_active = False
    elif invalid == 'different_identity':
        identity['fields']['customer_id'] = 2
    elif invalid == 'different_mold':
        document = json.loads(snapshot['physical_basis']); document['mold_tool_id'] = '999999'
        snapshot['physical_basis'] = identity['physical_basis'] = json.dumps(document)
        snapshot['purchase_identity'] = copy.deepcopy(identity)
    else:
        identity['fields']['sheet_cutting_settings'] = None
        snapshot['purchase_identity'] = copy.deepcopy(identity)
    value, proof = completion_basis(db, item, snapshot)
    assert value == snapshot['physical_basis'] and proof is None
