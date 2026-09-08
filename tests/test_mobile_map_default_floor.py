from pathlib import Path
import re
import subprocess


def test_mobile_map_defaults_and_explicit_floor_switches():
    html = (Path(__file__).resolve().parents[1] / 'static/mobile_erp.html').read_text(encoding='utf-8')
    expression = re.search(r'const floor = (state.warehouseMapFloors.find\(item => item.floor_code === resolvedPreferred.floorCode\).*?);', html, re.S).group(1)
    switch = re.search(r'byId\("warehouseMapFloor"\).addEventListener\("change", \(\) => \{(.*?)\n      \}\);', html, re.S).group(1)
    script = '''const assert = require('node:assert/strict');
const state = {warehouseMapFloors: ['1F','3F','4F'].map(floor_code => ({floor_code})), warehouseMapFloor: '1F'};
const choose = (resolvedPreferred = {}) => EXPRESSION;
assert.equal(choose().floor_code, '3F');
for (const code of ['1F','4F']) assert.equal(choose({floorCode:code}).floor_code, code);
let selected = ''; let rendered = 0; let loaded = 0;
const byId = () => ({value:selected});
const renderWarehouseMapSelectors = () => rendered++;
const loadWarehouseFloorOverview = () => loaded++;
const change = () => { SWITCH };
for (selected of ['1F','4F']) {change(); assert.equal(state.warehouseMapFloor, selected); assert.equal(state.warehouseMapArea, '');}
assert.equal(rendered,2); assert.equal(loaded,2);
assert.equal(choose().floor_code,'3F');
state.warehouseMapFloors = [{floor_code:'1F'}];
assert.equal(choose().floor_code,'1F');
state.warehouseMapFloors = [];
assert.equal(choose(),undefined);
'''.replace('EXPRESSION', expression).replace('SWITCH', switch)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
