from test_processed_sheet_matching import setup, candidates
from test_semi_finished_lot_eligibility import eligibility_db, _add_lot
import pytest
from app.models.material import Material
from app.models.supplier_paper_code import SupplierPaperCode


def test_remote_sizes_are_secondary_even_when_approved(eligibility_db):
    db,data=eligibility_db
    p,lot,_,_=setup(db,data,length=1345,approved=True)
    row=candidates(db,p)[0]
    assert row.recommendation_tier == 'more'
    assert not row.automatic_recommendation
    assert row.direct_deduction_eligible  # manual use remains valid, not a new identity rule


def test_close_sizes_rank_before_distant_and_exact_first(eligibility_db):
    db,data=eligibility_db
    p,lot,_,_=setup(db,data,length=790,approved=True)
    other=_add_lot(db,data,key='exact-ranking',customer_id=p.customer_id,length=800)
    other.semi_finished_detail.customer_generic_eligible=True
    db.flush()
    rows=candidates(db,p)
    assert [r.lot.id for r in rows] == [other.id,lot.id]
    assert all(r.recommendation_tier=='near' for r in rows)


def test_boundary_is_each_axis_not_average(eligibility_db):
    db,data=eligibility_db
    p,lot,_,_=setup(db,data,length=880,approved=True)
    assert candidates(db,p)[0].recommendation_tier=='near'
    lot.semi_finished_detail.board_length_mm=881
    db.flush()
    assert candidates(db,p)[0].recommendation_tier=='more'


@pytest.mark.parametrize('stock_color,target_color', [('white','kraft'),('kraft','white'),('white','white'),('kraft','kraft')])
def test_supplier_face_color_in_candidates_both_directions(eligibility_db,stock_color,target_color):
    db,data=eligibility_db
    p,lot,_,_=setup(db,data,length=800,approved=True)
    material=Material(code='Y1Y',supplier_name='ranking-test',is_white_face=False)
    db.add(material);db.flush();p.material_id=material.id
    lot.semi_finished_detail.material_code_snapshot='X1X'
    lot.semi_finished_detail.supplier_name='ranking-test'
    db.add_all([SupplierPaperCode(supplier_name='ranking-test',code_char=char,paper_name='测试',gram_weight=150,color=color)
                for char,color in [('X',stock_color),('Y',target_color)]])
    db.flush()
    assert bool(candidates(db,p)) == (stock_color==target_color)
