import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from tests.test_p1_32a2_requisition_production_print import production_print_app

ROOT = Path(__file__).resolve().parents[1]


def test_paper_grouping_quantities_and_operation_parameters():
    subprocess.run(['node', '-e', r"""
const assert = require('node:assert/strict');
const p = require('./static/ui/production-task-paper.js');
const component = {product_code:'80011946',product_name:'长片',report_length_mm:990,report_width_mm:540,
 material_code:'VIK',flute_type:'B',customer_order_quantity:100,finished_deduction_quantity:8,planned_finished_quantity:92,
 finished_unit:'片',joining_method:'无需结合',production_notes:['无需结合'],printing_situation:'无印刷',
 sheet_cutting_snapshot:{cutting_factor:3,length_parts:3,width_parts:1,theoretical_length_mm:330,
 theoretical_width_mm:540,is_die_cut:true,mold_count:2},mold_code:'80011946长模',mold_location_display:'A11'};
const card = {paper_group_id:'supplier-order:1',customer_id:1,customer_name:'研光',components:[component]};
const other = structuredClone(card); other.components[0].product_code='80011947';
assert.equal(p.groups([card,other]).length,1);
assert.equal(p.groups([card,{...other,customer_id:2}]).length,2);
assert.equal(p.groups([card,{...other,paper_group_id:'supplier-order:2'}]).length,2);
assert.equal(p.groups([{...card,paper_phase:'actual_receipt'},{...other,paper_phase:'actual_receipt'}]).length,2);
assert.equal(p.groups([card,{...other,source_type:'stock_replenishment'}]).length,2);
const ops = p.operations(component,card);
assert(ops.some(x=>x.label==='分切' && x.text.includes('330×540')));
assert(ops.some(x=>x.label==='模切' && x.text.includes('2模')));
assert(ops.some(x=>x.label==='模具位置' && x.text==='A11'));
assert(!ops.some(x=>x.label==='结合' || x.label==='印刷'));
assert(!p.body(p.atoms(p.groups([card])[0])).includes('无需结合'));
assert.equal(p.operations({...component,sheet_cutting_snapshot:{...component.sheet_cutting_snapshot,cutting_factor:1}},card).some(x=>x.label==='分切'),false);
assert(p.operations({...component,joining_method:'打钉'},card).some(x=>x.text==='打钉'));
assert(p.operations({...component,joining_method:'粘贴'},card).some(x=>x.text==='粘贴'));
assert(p.operations({...component,box_type_code:'a1_0201',crease_display:'100/200/100'},card).some(x=>x.label==='开槽' && x.text.includes('100/200/100')));
assert.deepEqual(p.atoms(p.groups([card,card])[0])[0].quantities,[200,16,184]);
const html=p.body(p.atoms(p.groups([card,other])[0]));
assert.equal((html.match(/<th>/g)||[]).length,5);
assert(html.includes('80011946') && html.includes('80011947'));
"""], cwd=ROOT, check=True)


def test_reservations_are_exact_and_exclude_new_output(production_print_app):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
    from app.services.production_paper_inventory import paper_inventory_sources, component_picks
    cutoff = datetime(2026,10,9,10,0)
    with production_print_app['session_factory']() as db:
        location=WarehouseLocation(location_code='RECOUNT-PENDING', source_version='RECOUNT_PENDING',
            location_name='一楼成品待归位',warehouse_type='shared',is_temporary=True,placement_status='unplaced')
        db.add(location); db.flush()
        lot=InventoryLot(lot_number='PAPER-STOCK',inventory_type='finished',warehouse_location_id=location.id,
            quantity_reserved=8,quantity_available=2,unit='boxes',source_type='manual',stock_date=date(2026,10,8),last_movement_at=cutoff)
        db.add(lot); db.flush()
        item=production_print_app['order_item_id']
        for name, status, reserved, consumed, released, created in [
            ('existing','partial',10,2,0,cutoff-timedelta(hours=1)),
            ('new-output','active',100,0,0,cutoff+timedelta(seconds=1)),
            ('released','released',100,0,100,cutoff-timedelta(hours=1))]:
            db.add(InventoryReservation(reservation_number=name,inventory_lot_id=lot.id,
                reservation_type='finished_order',order_item_id=item,reserved_stock_quantity=reserved,
                consumed_stock_quantity=consumed,released_stock_quantity=released,status=status,created_at=created))
        db.commit()
        rows=component_picks(paper_inventory_sources(db,[item],cutoff=cutoff),order_item_id=item)
        assert len(rows)==1
        assert rows[0]['quantity']==8 and rows[0]['consumed_quantity']==2
        assert rows[0]['location_id']==location.id and '待归位' in rows[0]['location_name']
        assert rows[0]['unit']=='只' and rows[0]['kind']=='finished'
        assert not component_picks(paper_inventory_sources(db,[item],cutoff=cutoff),order_item_id=item+999)
        assert not component_picks(paper_inventory_sources(db,[item],cutoff=cutoff),order_item_id=item,bom_id=999)
        assert 'cost' not in json.dumps(rows)
