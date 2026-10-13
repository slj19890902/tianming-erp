import json
import subprocess
from pathlib import Path


def test_notes_only_edit_and_clear_trigger_product_save():
    source = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text()
    fields = source.split('_productFormSaveFields() {', 1)[1].split('\n          },', 1)[0]
    dirty = source.split('_productFormDirty() {', 1)[1].split('\n          },', 1)[0]
    script = '''
const assert = require('node:assert/strict');
const ctx = {
  modal: {type:'product'}, drawingFile:null,
  productForm: {production_notes:'', product_code:'82021188'},
  productPrintingWriteFields: () => ({}),
  _productFormSaveFields: new Function(FIELDS),
  _productFormDirty: new Function(DIRTY),
};
ctx.productFormSnapshot = JSON.stringify(ctx._productFormSaveFields());
assert.equal(ctx._productFormDirty(), false);
ctx.productForm.production_notes = '纸塑；每模件数待核对';
assert.equal(ctx._productFormDirty(), true, 'notes-only edit must reach save API');
ctx.productFormSnapshot = JSON.stringify(ctx._productFormSaveFields());
assert.equal(ctx._productFormDirty(), false);
ctx.productForm.production_notes = '';
assert.equal(ctx._productFormDirty(), true, 'explicit clearing must also save');
'''.replace('FIELDS', json.dumps(fields)).replace('DIRTY', json.dumps(dirty))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
