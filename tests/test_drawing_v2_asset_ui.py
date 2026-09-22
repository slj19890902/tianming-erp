"""Print artwork remains selectable without becoming a legacy default drawing."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_print_artwork_is_excluded_from_legacy_defaults_but_retained_in_gallery():
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node unavailable; no installation permitted')
    source = Path('static/index.html').read_text(encoding='utf-8')
    def section(start, end):
        return source[source.index(start):source.index(end, source.index(start))]
    methods = '\n'.join([
        section('          engineeringDrawings(', '          plainProductText('),
        section('          async previewProductDrawing(', '          async loadProductMaterialContext('),
        section('          drawingDisplayName(', '          _productFormSaveFields('),
        section('previewOrderItemDrawing(item) {', 'previewDrawingFile(path) {'),
    ])
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),sandbox={axios:{}};
vm.runInNewContext('methods=({'+METHODS+'});',sandbox);
const artwork={id:9,purpose:'print_artwork',file_name:'logo.png',image_path:'/files/logo.png'};
const engineering={id:8,purpose:'engineering',image_path:'/files/box.pdf'};
const legacy={id:7,image_path:'/files/legacy.pdf'};
const gallery=[artwork,engineering,legacy],seen=[],warnings=[];
const c={...sandbox.methods,previewDrawingFile(path){seen.push(path);},showToast(text){warnings.push(text);},errorMessage(e){return e.message;}};
(async()=>{
 assert.equal(c.engineeringDrawings(gallery).map(x=>x.id).join(','),'8,7');
 assert.equal(gallery.length,3);assert.equal(gallery[0],artwork);
 assert.equal(c.productDrawingText({drawings:[artwork]}),'无图纸');
 assert.equal(c.productDrawingText({drawings:[legacy]}),'有图纸');
 assert.equal(c.drawingDisplayName(artwork),'印刷原件｜logo.png');
 sandbox.axios.get=async()=>({data:{drawings:gallery}});
 await c.previewProductDrawing({id:1});assert.equal(seen.pop(),'/files/box.pdf');
 c.previewOrderItemDrawing({_product_drawings:gallery});assert.equal(seen.pop(),'/files/box.pdf');
 c.previewOrderItemDrawing({_product_drawings:gallery,drawing_file:'/files/order.pdf'});assert.equal(seen.pop(),'/files/order.pdf');
 c.previewOrderItemDrawing({_product_drawings:[artwork]});assert.equal(seen.pop(),'');
 sandbox.axios.get=async()=>({data:{drawings:[artwork]}});
 await c.previewProductDrawing({id:1});assert.equal(seen.length,0);assert.equal(warnings.length,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('METHODS', json.dumps(methods))
    result = subprocess.run([node], input=script, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
