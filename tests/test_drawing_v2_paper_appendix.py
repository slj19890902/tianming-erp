"""Exercise the paper appendix data and measured capacity guards without a browser."""
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE = Path('static/requisition-production-print.html').read_text(encoding='utf-8')


def run_node(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node unavailable; installation is not permitted')
    start = SOURCE.index('      function drawingAppendixHtml(')
    end = SOURCE.index('      function orderFactsHtml(', start)
    helpers = SOURCE[start:end]
    script = r'''
const assert=require('node:assert/strict');
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const drawingSvg=(drawing,print)=>`<img data-snapshot="${print?'print':'structure'}:${drawing.release_id}">`;
let pages=[];
const pagesHost={querySelectorAll:()=>pages},printButton={disabled:false},message={};
''' + helpers + body
    result = subprocess.run([node], input=script, capture_output=True, text=True,
                            encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_many_long_objects_keep_exact_values_and_traceable_repeating_header():
    run_node(r'''
const objects=Array.from({length:30},(_,index)=>({kind:'text',text:`对象${index}:`+'字'.repeat(190),
 width_mm:'60.25',height_mm:'11.75',panel_id:'center',x_mm:'0.25',y_mm:'2.50',rotation_deg:'90'}));
objects[0].text='<Logo & 标记>\n保留原文';
const drawing={release_id:71,number:'客户<&>-001',revision:' A/02 ',geometry:{width_mm:'752.25',height_mm:'996.5',dimensions:{左侧翼:'40.25'}},print_objects:objects};
const card={product_code:'21301204',source_identity:'supplier:9',components:[
 {production_task_id:5,managed_drawing:{release_id:71}},
 {production_task_id:6,managed_drawing:{release_id:72}}]};
const html=drawingAppendixHtml(card,drawing);
const header=html.slice(html.indexOf('<thead>'),html.indexOf('</thead>'));
assert.ok(header.includes('客户&lt;&amp;&gt;-001  A/02 '));
assert.ok(header.includes('任务 5｜产品 21301204｜来源 supplier:9'));
assert.ok(!header.includes('任务 6'));
assert.equal((html.match(/<tr>/g)||[]).length,34); // header, image, overall, one dimension, 30 objects
for (const object of objects) assert.ok(html.includes(escapeHtml(object.text)));
assert.ok(html.includes('60.25 × 11.75 mm｜面板 center｜距边 0.25, 2.50 mm｜方向 90°'));
assert.ok(html.includes('data-snapshot="print:71"'));
assert.ok(html.includes('总展开 752.25 × 996.5 mm'));
assert.ok(html.includes('左侧翼：40.25 mm'));
const plain=drawingAppendixHtml(card,{...drawing,release_id:72,print_objects:[]});
assert.ok(plain.includes('无印刷内容；本页以结构尺寸为主。'));
assert.ok(plain.includes('任务 6｜产品 21301204'));
assert.ok(plain.includes('data-snapshot="print:72"'));
''')
    # The native print engine can split the table and repeats its identity header.
    assert '.drawing-appendix thead { display:table-header-group; }' in SOURCE
    assert '.drawing-appendix { padding:12mm; height:auto; min-height:297mm; overflow:visible; }' in SOURCE


def test_measured_appendix_overflow_or_missing_image_stops_printing():
    run_node(r'''
function page({rows=Array(30).fill(70),header=100,width=794,scrollWidth=794,image=true,visible=true,cellOverflow=false}={}) {
 return {clientWidth:width,scrollWidth,getClientRects:()=>visible?[{}]:[],
  querySelector:selector=>selector==='img'?(image?{complete:true,naturalWidth:100}:null):{getBoundingClientRect:()=>({height:header})},
  querySelectorAll:selector=>selector==='th,td'?[{clientWidth:600,scrollWidth:cellOverflow?900:600}]:rows.map(height=>({getBoundingClientRect:()=>({height})}))};
}
pages=[page()]; // Total content spans several pages; individual rows fit.
assert.equal(checkDrawingPrintReadiness(),true);
assert.equal(printButton.disabled,false);
for (const bad of [page({rows:[1000]}),page({scrollWidth:900}),page({cellOverflow:true}),page({image:false})]) {
 pages=[bad]; printButton.disabled=false;
 assert.equal(checkDrawingPrintReadiness(),false);
 assert.equal(printButton.disabled,true);
 assert.equal(message.hidden,false);
 assert.ok(message.textContent.includes('已停止打印'));
}
pages=[page({image:false,visible:false})];
assert.equal(checkDrawingPrintReadiness(),true); // Internal drawings stay hidden in customer mode.
''')


def test_complete_paper_script_parses_and_checks_images_before_enabling_print():
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node unavailable; installation is not permitted')
    script = SOURCE.split('<script>', 1)[1].split('</script>', 1)[0]
    result = subprocess.run([node, '--check'], input=script, capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    render = SOURCE.split('      async function render(packageData) {', 1)[1].split('      async function loadPackage()', 1)[0]
    assert render.index('await waitForDrawingImages()') < render.index('assertDrawingAppendicesFit()') < render.rindex('printButton.disabled =')
    assert "window.addEventListener('beforeprint'" in SOURCE
    assert 'body.print-blocked .pages { display:none !important; }' in SOURCE
