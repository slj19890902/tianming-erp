"""Exercise the shipped page methods in Node; this is not browser validation."""
from pathlib import Path
from html.parser import HTMLParser
import json
import shutil
import subprocess

import pytest


def test_editor_http_retry_late_read_and_template_switch():
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node runtime unavailable; no installation permitted')
    source = Path('static/index.html').read_text(encoding='utf-8')
    helper = source[source.index('// BEGIN IDEMPOTENCY_KEY_HELPER'):source.index('// END IDEMPOTENCY_KEY_HELPER')]
    methods = source[source.index('          async changeDrawingV2Template()'):source.index('          async openProduct(row=null)')]
    program = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const sandbox = {axios:{}}; // Deliberately no crypto.randomUUID (ordinary LAN HTTP).
vm.runInNewContext(HELPER + '\nmethods = ({' + METHODS + '});', sandbox);
function context() {
  const c={...sandbox.methods,canEditProducts:true,productForm:{id:42,version:1},
    drawingV2:{template_key:'liner_v1',parameters:{},print_objects:[],paper_color:'white',thickness_mm:3,
      thickness_source:'UAT',thickness_approximate:false,customer_number:'',customer_revision:'',version:null},
    _productFormSaveFields(){return {id:42};},productFormSnapshot:'{"id":42}',
    errorMessage(e){return e.message;},showToast(){}};
  return c;
}
(async()=>{
  const c=context(), keys=[]; let fail=true, posts=0;
  sandbox.axios.put=async(url,payload)=>{keys.push(payload.idempotency_key);if(fail){fail=false;throw Error('lost response');}return {};};
  sandbox.axios.get=async()=>({data:{draft:{...c.drawingV2,version:1},geometry_ready:true,panels:[{id:'face',width:'50.5',height:'100.25'}],releases:[]}});
  sandbox.axios.post=async(url,payload)=>{posts++;assert.ok(payload.idempotency_key.length>=12);return {};};
  await c.saveDrawingV2(); assert.ok(c.drawingV2.pendingSave);
  await c.saveDrawingV2(); assert.equal(keys.length,2);assert.equal(keys[0],keys[1]);
  assert.equal(c.drawingV2.version,1);assert.equal(c.drawingV2.error,'');
  await c.publishDrawingV2();assert.equal(posts,1);

  const slow=context();slow.drawingV2.parameters={left_wing_mm:'40'};
  let resolve; sandbox.axios.get=()=>new Promise(r=>{resolve=r;});
  const request=slow.loadDrawingV2();assert.equal(slow.drawingV2.reading,true);
  slow.drawingV2.parameters.left_wing_mm='42';
  resolve({data:{draft:{...slow.drawingV2,parameters:{left_wing_mm:'40'},version:1},geometry_ready:true}});
  assert.equal(await request,false);assert.equal(slow.drawingV2.parameters.left_wing_mm,'42');
  assert.equal(slow.drawingV2.reading,false);

  const changed=context();changed.drawingV2.version=1;
  changed.drawingV2.print_objects=[{kind:'image',panel_id:'face',width_mm:10,height_mm:15}];
  changed.drawingV2.template_key='custom_21301634_v1';
  sandbox.axios.get=async()=>({data:{product_version:1,parameters:{top_cover_mm:'235',left_fold_mm:'26'}}});
  await changed.changeDrawingV2Template();
  assert.equal(changed.drawingV2.geometry_ready,false);
  assert.equal(changed.drawingV2.print_objects[0].panel_id,'face');
  assert.equal(changed.drawingV2.print_objects[0].width_mm,10);
  assert.equal(changed.drawingV2PanelOptions().map(p=>p.id).join(','),'center,top,bottom');
  changed.drawingV2.print_objects[0].panel_id='center'; // Explicit reassignment, no guessing/stretch.
  const signature=changed.drawingV2Signature();
  changed.drawingV2.print_objects[0].display_unit='cm';
  assert.equal(changed.drawingV2Signature(),signature); // Display unit is not a new design version.
  changed.drawingV2Input(changed.drawingV2.print_objects[0],'width_mm','60');
  assert.equal(changed.drawingV2.print_objects[0].width_mm,600);

  const reader=context();reader.canEditProducts=false;
  let writes=0;
  sandbox.axios.get=async()=>({data:{draft:{...reader.drawingV2,version:3},editing_enabled:true,
    geometry_ready:true,releases:[{id:77,number:'CUSTOM-77',revision:' A/2 '}]}});
  sandbox.axios.put=async()=>{writes++;return {};};sandbox.axios.post=async()=>{writes++;return {};};
  assert.equal(await reader.loadDrawingV2(),true);
  assert.equal(reader.drawingV2.releases[0].id,77);
  assert.equal(reader.drawingV2.releases[0].revision,' A/2 ');
  await reader.saveDrawingV2();await reader.publishDrawingV2();
  assert.equal(writes,0,'Read-only users must never issue drawing writes');
  reader.canEditProducts=true;
  sandbox.axios.get=async()=>({data:{draft:{...reader.drawingV2,version:3},editing_enabled:false,
    geometry_ready:true,releases:[{id:77,number:'CUSTOM-77',revision:' A/2 '}]}});
  assert.equal(await reader.loadDrawingV2(),true);
  assert.equal(reader.drawingV2.releases[0].id,77);
  await reader.saveDrawingV2();await reader.publishDrawingV2();
  assert.equal(writes,0,'Disabled V2 still reads releases but cannot write');
  console.log('HTTP retry, stale read and template-switch checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('HELPER', json.dumps(helper)).replace('METHODS', json.dumps(methods))
    result = subprocess.run([node], input=program, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_readonly_drawing_controls_are_outside_disabled_product_fieldset():
    class Fieldsets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.controls = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'fieldset':
                self.stack.append(attrs.get(':disabled', ''))
            action = attrs.get('@click', '')
            if action in ('loadDrawingV2', 'saveDrawingV2', 'publishDrawingV2'):
                self.controls.setdefault(action, []).append(tuple(self.stack))
            if tag == 'a' and 'managed-drawing/releases/' in attrs.get(':href', ''):
                self.controls.setdefault('pdf', []).append(tuple(self.stack))

        def handle_endtag(self, tag):
            if tag == 'fieldset':
                assert self.stack, 'Unexpected fieldset closing tag'
                self.stack.pop()

    parsed = Fieldsets()
    parsed.feed(Path('static/index.html').read_text(encoding='utf-8'))
    assert not parsed.stack
    for control in ('loadDrawingV2', 'pdf'):
        assert parsed.controls.get(control)
        assert all(not any('!canEditProducts' in condition for condition in parents)
                   for parents in parsed.controls[control]), f'{control} is disabled for read-only users'
    for control in ('saveDrawingV2', 'publishDrawingV2'):
        assert parsed.controls.get(control)
        assert all(any('!canEditProducts' in condition for condition in parents)
                   for parents in parsed.controls[control]), f'{control} must remain permission-disabled'


def test_product_open_and_save_keep_decimal_dimensions_and_integer_reporting():
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node runtime unavailable; no installation permitted')
    source = Path('static/index.html').read_text(encoding='utf-8')
    def section(start, end):
        index = source.index(start)
        return source[index:source.index(end, index)]
    methods = '\n'.join([
        section('          normalizeMmInteger(value)', '          boxTypeLookupKey(value)'),
        section('          normalizeProductDimensions(form)', '          parseProductionProcesses(value)'),
        section('          hydrateProductForm(detail)', '          drawingDisplayName(drawing)'),
        section('          buildProductWritePayload(options = null)', '          async prepareProductOneClickSave()'),
    ])
    program = r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),sandbox={blankProduct:()=>({})};
vm.runInNewContext('methods=({'+METHODS+'});',sandbox);
const c={...sandbox.methods,
 mergePrintingPlateOptions(){},normalizeBoxTypeDisplay:v=>v,parseProductPrintingColors:()=>[],
 hydrateProductPrintingPlateRows(){},productBoxTypeRule:()=>null,usesProductSplice:()=>false,
 usesProductTongue:()=>true,normalizeCuttingMode:v=>v,usesProductDefaultCuttingMode:()=>false,
 parseProductionProcesses:()=>['无需结合'],unmanagedProductionProcesses:()=>[],
 validateProductCreaseAndReport:()=>null,productPrintingWriteFields:()=>({print_content:'无印刷'}),
 serializeProductionProcesses:()=> '无需结合',productPrintingConfigurationChanged:()=>false,
 attachMasterUpdateMetadata:(kind,payload)=>payload};
const source={product_code:'DECIMAL-UAT',length_mm:'620.25',width_mm:'470.50',height_mm:'26.25',
 report_length_mm:'1450.75',report_width_mm:'997.40',crease_left_mm:'100.49',crease_middle_mm:'200.50',
 base_report_width_mm:'700.50',base_crease_right_mm:'35.75',flap_mm:'30.4'};
const opened=c.hydrateProductForm(source);
const dimensions=object=>[object.length_mm,object.width_mm,object.height_mm];
assert.deepEqual(dimensions(opened),[620.25,470.50,26.25]);
assert.equal(source.length_mm,'620.25');
for (const [key,value] of Object.entries({report_length_mm:1451,report_width_mm:997,crease_left_mm:100,
 crease_middle_mm:201,base_report_width_mm:701,base_crease_right_mm:36,flap_mm:30})) assert.equal(opened[key],value);
assert.equal(opened._report_dims_manual,true);assert.equal(opened._crease_dims_manual,true);
c.productForm=opened;
const saved=c.buildProductWritePayload();
assert.deepEqual(dimensions(saved),[620.25,470.50,26.25]);
assert.equal(saved.report_length_mm,1451);assert.equal(saved.crease_middle_mm,201);assert.equal(saved.flap_mm,30);
c.productForm={...opened,length_mm:'620.25',width_mm:'470.50',height_mm:'26.25',report_length_mm:'1450.75',crease_left_mm:'100.49'};
const direct=c.buildProductWritePayload();
assert.deepEqual(dimensions(direct),[620.25,470.50,26.25]);
assert.equal(direct.report_length_mm,1451);assert.equal(direct.crease_left_mm,100);
'''.replace('METHODS', json.dumps(methods))
    result = subprocess.run([node], input=program, capture_output=True, text=True,
                            encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
