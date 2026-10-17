import json
import pytest
from app.services import shared_finished_stock as shared


def identity(unit='只',**overrides):
    return json.dumps({'basis':{'unit':unit,'box_category':'die_cut','mold_tool_id':'358',
        'pieces_per_box':'1','splice_mode':'single','assembly':[],
        'production_process':'模切,无需结合',**overrides},'extra':{}},ensure_ascii=False)


def test_flat_no_joining_piece_names_match_without_rewriting_identities():
    a=identity();b=identity('片',production_process='无需结合,模切');before=(a,b)
    assert shared._interchangeability_key(a)==shared._interchangeability_key(b)
    assert (a,b)==before


@pytest.mark.parametrize('overrides',[{'pieces_per_box':'2'},{'splice_mode':'double'},
    {'mold_tool_id':None},{'box_category':'normal'},{'production_process':'模切'},
    {'production_process':'模切,粘贴'},{'production_process':'模切,无需结合,粘贴'}])
def test_ambiguous_or_different_physical_units_remain_blocked(overrides):
    assert shared._interchangeability_key(identity(**overrides))!=shared._interchangeability_key(identity('片',**overrides))


@pytest.mark.parametrize('unit',['套','张','公斤'])
def test_no_set_sheet_or_weight_conversion(unit):
    assert shared._interchangeability_key(identity(unit))!=shared._interchangeability_key(identity('片'))
