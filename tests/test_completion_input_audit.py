from app.services.completion_input_audit import input_evidence


def receipt(i, qty, plan=100):
    return dict(id=i,received_quantity=qty,planned_quantity=plan,resolution_action=None,supplier_order_item_id=1)


def test_partial_receipts_compare_lifetime_at_same_source():
    rows=[receipt(1,100),receipt(2,20,20)]
    completions=[dict(id=1,origin='manual',material_input_quantity=100),dict(id=2,origin='manual',material_input_quantity=20)]
    # Same source has a 100-sheet ceiling; second receipt does not enlarge it.
    result=input_evidence(rows,completions,{}, {})
    assert result['severity']=='error' and result['evidence']['allowed_production_input']==100
    rows[-1]['resolution_action']='all_to_production'
    assert input_evidence(rows,completions,{}, {}) is None
    rows[-1]['supplier_order_item_id']=2
    assert input_evidence(rows,completions,{}, {})['severity']=='review'


def test_auto_cover_base_batches_are_sets_not_sum_of_sheets():
    rows=[receipt(1,100),receipt(2,100),receipt(3,20,20),receipt(4,20,20)]
    completions=[dict(id=1,origin='receipt_auto',material_input_quantity=100,actual_output_quantity=100),
                 dict(id=2,origin='receipt_auto',material_input_quantity=20,actual_output_quantity=20)]
    allocations={r['id']:[dict(status='posted',component_type='cover' if r['id']%2 else 'base',
       production_completion_id=r['id']//2 if not r['id']%2 else None,finished_output_qty_delta=r['received_quantity'] if not r['id']%2 else 0,
       receipt_order_purpose_sheet_qty=r['received_quantity'],purchase_purpose_source_snapshot_id=1)] for r in rows}
    snapshots={1:dict(yield_per_sheet_snapshot=1,pieces_per_finished_snapshot=1)}
    assert input_evidence(rows,completions,allocations,snapshots) is None
    allocations[3][0]['receipt_order_purpose_sheet_qty']=10
    result=input_evidence(rows,completions,allocations,snapshots)
    assert result['severity']=='error' and result['evidence']['allowed_production_input']==110
    # Reversed/missing allocation is not proof of a surviving input budget.
    del allocations[4]
    assert input_evidence(rows,completions,allocations,snapshots)['severity']=='review'


def test_manual_frozen_whole_excludes_reserve_and_cut_units_need_evidence():
    rows=[receipt(1,120)];completions=[dict(id=1,origin='manual',material_input_quantity=105)]
    allocations={1:[dict(status='posted',component_type='whole',receipt_order_purpose_sheet_qty=100)]}
    result=input_evidence(rows,completions,allocations,{})
    assert result['evidence']['allowed_production_input']==100
    assert input_evidence(rows,completions,allocations,{},independent_cutting=True)['severity']=='review'
