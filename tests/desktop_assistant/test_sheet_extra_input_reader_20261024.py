import hashlib
import json
import sqlite3
import pytest
from desktop_assistant.supplier_sheet_reader import require_extra_input_reader, EXTRA_INPUT_CAPABILITY
from desktop_assistant.reader_contract import target_readers


def database(tmp_path, credit):
    path=tmp_path/'sheet-reader.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table inventory_reservations(credited_requirement_quantity integer, cut_plan_json text)')
        db.execute('insert into inventory_reservations values(?,?)',(credit,json.dumps({'actual_sheet_input_v1':True,'full_sheet_output_v1':True})))
    return path


@pytest.mark.parametrize('value',[None,True,'1',0,2])
def test_extra_material_rejects_old_or_invalid_reader_without_writes(tmp_path,value):
    path=database(tmp_path,0)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='额外片料'):
        require_extra_input_reader(path,{'reader_capabilities':{EXTRA_INPUT_CAPABILITY:value}})
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert require_extra_input_reader(path,{'reader_capabilities':{EXTRA_INPUT_CAPABILITY:1}})


def test_positive_credit_legacy_input_remains_compatible(tmp_path):
    assert require_extra_input_reader(database(tmp_path,5),{}) is False


def test_target_admission_preserves_new_reader_and_detects_persisted_fact(tmp_path):
    path=database(tmp_path,0)
    with pytest.raises(ValueError):target_readers(path,{}, {},None,None,factory=False)
    manifest={'reader_capabilities':{EXTRA_INPUT_CAPABILITY:1}}
    target_readers(path,{},manifest,None,None,factory=False)
    with sqlite3.connect(path) as db:db.execute('delete from inventory_reservations')
    with pytest.raises(ValueError):target_readers(path,manifest,{},None,None,factory=False)
