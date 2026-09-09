from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_information_label_omits_all_quantity_fields_and_requires_binding():
    html = (ROOT / 'static/location-label.html').read_text(encoding='utf-8')
    functions = '\n'.join(re.search(r'    function ' + name + r'\(.*', html)[0]
                          for name in ('labelHtml', 'fieldKey', 'shelfHtml'))
    script = "const assert=require('node:assert/strict');let informationOnly=true;const h=x=>String(x??'');\n" + functions + r'''
const row={area_code:'F',rack_code:'1',level_no:2,slot_no:3,qr_data_url:'QR',shelf_content:{customer_short_name:'天明',inventory_code:'SKU',product_name:'纸箱',specification:'400x300x200',units_per_bundle:50,warning_quantity:100}};
let result=labelHtml(row);
for(const text of ['天明','SKU','纸箱','400x300x200','QR']) assert.ok(result.includes(text));
for(const text of ['只/捆','预警参考','每捆数量']) assert.ok(!result.includes(text));
assert.ok(labelHtml({}).includes('扫码看位置'));
assert.throws(()=>labelHtml({...row,shelf_content:{restricted:true}}),/客户/);
informationOnly=false;
assert.ok(labelHtml(row).includes('50只/捆'));
assert.ok(labelHtml(row).includes('预警参考100只'));
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True, encoding='utf-8')
    assert '/static/shelf-label.html?' in (ROOT / 'factory_twin/frontend/src/WarehouseTwinApp.tsx').read_text(encoding='utf-8')


def test_mixed_cell_expands_to_one_label_per_product():
    html = (ROOT / 'static/location-label.html').read_text(encoding='utf-8')
    fn = re.search(r'    function renderLocationLabels\(.*', html)[0]
    script = "const assert=require('node:assert/strict');let sourceRows=[];const informationOnly=true,batchMode=true;function renderRows(){}\n" + fn + r'''
renderLocationLabels({items:[{location_id:1,shelf_contents:[{product_id:2},{product_id:3}]},{location_id:4,shelf_contents:[]}]});
assert.equal(sourceRows.length,3);
assert.equal(sourceRows[0].shelf_content.product_id,2);
assert.equal(sourceRows[1].shelf_content.product_id,3);
assert.equal(sourceRows[2].location_id,4);
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True)
