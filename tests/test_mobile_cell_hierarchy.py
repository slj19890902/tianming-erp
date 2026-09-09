from pathlib import Path
import os
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def source(path):
    if os.environ.get('ERP_UI_TEST_BASELINE'):
        return subprocess.check_output(['git', 'show', f'origin/factory-current-baseline:{path}'], cwd=ROOT).decode('utf-8')
    return (ROOT / path).read_text(encoding='utf-8')


def test_mobile_identity_description_quantity_and_grouping():
    html = source('static/mobile_erp.html')
    functions = '\n'.join(re.search(r'      function ' + name + r'\([^\n]*\) \{.*?\n      \}', html, re.S)[0]
                          for name in ['groupWarehouseRackGoods', 'warehouseGoodsIdentity', 'warehouseGoodsDescription'])
    script = r'''
const assert=require('node:assert/strict');
function node(tag,cls,text=''){return {tag,cls,text,children:[],append(...xs){this.children.push(...xs)}}}
''' + functions + r'''
const good={customer_id:1,customer_short_name:'客户简称',customer_name:'客户全称',product_id:2,lot_id:3,product_code:'SKU-123',product_name:'纸箱',specification:'400x300x200',unit:'只',quantity_total:12};
const identity=warehouseGoodsIdentity(good), description=warehouseGoodsDescription(good);
assert.deepEqual(identity.children.map(x=>x.text),['客户简称','SKU-123']);
assert.equal(identity.children[1].tag,'strong');
assert.deepEqual(description.children.map(x=>x.text),['纸箱','400x300x200']);
assert.equal(warehouseGoodsIdentity({}).children.length,2);
const groups=groupWarehouseRackGoods([good,{...good,lot_id:4,quantity_total:8},{...good,lot_id:5,product_id:9},{...good,unit:'张'}]);
assert.equal(groups.length,3);assert.equal(groups[0].quantity_total,20);assert.equal(good.quantity_total,12);
assert.equal(groupWarehouseRackGoods([good,{...good,quantity_total:null}])[0].quantity_total,null);
assert.equal(groupWarehouseRackGoods([{...good,product_id:null},{...good,product_id:null,lot_id:4}]).length,2);
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True)
    card = html[html.index('goods.forEach(good => {', html.index('function renderWarehouseLocationGoods')):]
    assert card.index('warehouseGoodsIdentity(good)') < card.index('warehouseGoodsDescription(good)') < card.index('warehouse-stocktake-quantity')
    assert 'if (productGroups.length === 1) preview.append' in html


def test_desktop_mixed_product_details_are_collapsed():
    tsx = source('factory_twin/frontend/src/WarehouseTwinApp.tsx')
    assert 'groupShelfProducts(cellItems).length === 1 && <><span>{group.item.product_name' in tsx
    assert re.search(r'<details><summary>.*?</summary>\s*\{groupShelfProducts\(cellItems\).length > 1 && <div className="shelf-expanded-description"', tsx)
