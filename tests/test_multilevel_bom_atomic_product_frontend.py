import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_product_editor_submits_one_atomic_request(existing, failure):
    html = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
    block = html.split('                const productDirty = this._productFormDirty();', 1)[1]
    block = 'const productDirty = this._productFormDirty();' + block.split(
        '              if (this.modal.type === "material") {', 1)[0].rsplit('}', 1)[0]
    node = shutil.which("node")
    assert node, "Node.js is required"
    script = """
const assert=require('node:assert/strict');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const calls=[];
const write=async(url,body)=>{
  calls.push({url,body});
  if(failure)throw Error('rollback');
  return {data:{product:{id:9,version:4},bom:{version:4,components:[]}}};
};
global.axios={post:write,put:write};
const ctx={productForm:{id:existing?9:null},drawingFile:null,productEditReturnContext:null,
  _productFormDirty:()=>true,_productBomDirty:()=>true,validateProductBom:()=>'',
  buildProductWritePayload:()=>({expected_version:3,production_notes:'new'}),
  bomPayload:(version)=>({expected_version:version??3,components:[{component_product_id:2}]}),
  hydrateProductForm:x=>x,beginMasterEdit:()=>{},
  applyBomResponse(x){this.bom=x},_productFormSaveFields(){return this.productForm},
  loadProducts:async()=>true};
(async()=>{
  const before=JSON.stringify(ctx.productForm);
  try {await new AsyncFunction('masterOptions',block).call(ctx,{});assert.equal(failure,false)}
  catch(e){if(!failure)throw e;assert.equal(e.message,'rollback')}
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,existing?'/api/master/products/9/with-bom':'/api/master/products/with-bom');
  assert.equal(calls[0].body.product.production_notes,'new');
  assert.equal(calls[0].body.bom.expected_version,existing?3:1);
  if(failure)assert.equal(JSON.stringify(ctx.productForm),before);
  else {assert.equal(ctx.productForm.version,4);assert.equal(ctx.bom.version,4)}
})().catch(e=>{console.error(e);process.exit(1)});
"""
    prefix = f"const existing={json.dumps(existing)},failure={json.dumps(failure)},block={json.dumps(block)};\n"
    result = subprocess.run([node], input=prefix + script, text=True, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr
