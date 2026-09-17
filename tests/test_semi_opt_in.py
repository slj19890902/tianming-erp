import json
import subprocess
from pathlib import Path
from dataclasses import replace
from test_processed_sheet_matching import setup, candidates
from test_semi_finished_lot_eligibility import eligibility_db
from app.api.warehouse import _semi_candidate_dict


def test_general_candidate_is_discovery_only_but_explicit_customer_is_preserved(eligibility_db):
    db,data=eligibility_db
    product,lot,profile,facts=setup(db,data,approved=True)
    row=candidates(db,product)[0]
    assert _semi_candidate_dict(row)['customer_bound']
    assert _semi_candidate_dict(row)['automatic_recommendation']
    facts.update(scope='general',customer_ids=[])
    profile.data_json=json.dumps(facts);db.flush()
    result=_semi_candidate_dict(replace(row,automatic_recommendation=True))
    assert not result['customer_bound'] and not result['automatic_recommendation']
    assert result['selectable']==row.selectable


def test_opt_in_and_decline_page_methods():
    result=subprocess.run(['node','tests/semi_opt_in_harness.cjs'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
