import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")


def method(name):
    pattern = re.compile(r"^          (?:async )?" + re.escape(name) + r"\(", re.M)
    start = pattern.search(HTML).start()
    end = re.compile(r"^          (?:async )?[A-Za-z_$][\w$]*\(", re.M).search(HTML, start + 12).start()
    return HTML[start:end]


def run_js(body):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    names = ["bomComponentOption", "normalizeBomComponent", "applyBomResponse", "mergeBomComponentOptions",
        "onBomInventoryModeChange", "_productBomSaveFields", "_productBomDirty", "validateProductBom",
        "bomPayload", "openBomChildEditor", "returnFromCommonBoxEditor"]
    constants = HTML[HTML.index("      let bomComponentKeyCounter"):HTML.index("      const blankMaterial =")]
    script = "const assert=require('node:assert/strict');\n" + constants
    script += "\nconst methods={" + "\n".join(method(name) for name in names) + "};\n"
    script += "const axios={get:async()=>({data:{id:2,product_name:'新版内衬',product_code:'SHARED'}})};\n"
    script += "(async()=>{" + body + "})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node], input=script, text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_fields_round_trip_and_no_free_text_recipe_in_new_mode():
    run_js("""
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1,composite_fulfillment_mode:'parent_delivery'},
        spec:()=>'',modal:{type:'product'}};
      ctx.bomEditor.enabled=true;
      ctx.bomEditor.inventory_mode='assembled';
      ctx.bomEditor.components=[{...blankBomComponent(),component_product_id:2,quantity_per_set:2},
        {...blankBomComponent(),component_product_id:3,quantity_per_set:6}];
      ctx.bomEditor.subkit={name:'不应再靠手写名称',enabled:false};
      ctx.onBomInventoryModeChange();
      assert.equal(ctx.validateProductBom(),'');
      const payload=ctx.bomPayload(7);
      assert.equal(payload.inventory_mode,'assembled');
      assert.equal(payload.expected_version,7);
      assert.equal(payload.subkit,null);
      assert.deepEqual(payload.components.map(r=>r.inventory_relation),['assembly','assembly']);
      ctx.applyBomResponse({...payload,version:8,is_composite:true});
      assert.equal(ctx.bomEditor.inventory_mode,'assembled');
      assert.equal(ctx.bomEditor.persisted_inventory_mode,'assembled');
      assert.equal(ctx._productBomDirty(),false);
      ctx.bomEditor.components[1].inventory_relation='accompany';
      assert.equal(ctx._productBomDirty(),true);
    """)


def test_invalid_assembly_cannot_be_saved_as_ordinary_manufacture():
    run_js("""
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1}};
      ctx.bomEditor.enabled=true;ctx.bomEditor.inventory_mode='manufactured';
      ctx.bomEditor.components=[{...blankBomComponent(),component_product_id:2,inventory_relation:'assembly'}];
      assert.match(ctx.validateProductBom(),/只能独立配套/);
      ctx.bomEditor.inventory_mode='assembled';ctx.bomEditor.components=[];
      assert.match(ctx.validateProductBom(),/至少需要一个组装子件/);
    """)


def test_real_product_use_is_integer_without_changing_legacy_decimal_contract():
    run_js("""
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1}};
      ctx.bomEditor.enabled=true;ctx.bomEditor.inventory_mode='assembled';
      ctx.bomEditor.components=[{...blankBomComponent(),component_product_id:2,
        inventory_relation:'assembly',quantity_per_set:1.5}];
      assert.match(ctx.validateProductBom(),/正整数/);
      ctx.bomEditor.components[0].quantity_per_set=2;
      assert.equal(ctx.validateProductBom(),'');
      ctx.bomEditor.inventory_mode='legacy';ctx.bomEditor.components[0].quantity_per_set=1.5;
      assert.equal(ctx.validateProductBom(),'');
    """)


def test_child_editor_cancel_restores_unsaved_parent_and_original_return_context():
    run_js("""
      const form={id:1,product_name:'父产品未保存备注'};
      const bom=blankBomEditor();bom.components=[{component_product_id:2,quantity_per_set:9}];
      const previous={source:'order',productId:1};
      const ctx={...methods,productForm:form,bomEditor:bom,productEditReturnContext:previous,
        modal:{type:'product',title:'父产品'},masterEditBaseline:{product:{version:7}},
        productFormSnapshot:'original-form',productBomSnapshot:'original-bom',drawingFile:{realFile:true},
        canEditProducts:true,masterSavePending:false,spec:()=>'',showToast:()=>{},
        openProduct:async function(){this.productForm={id:2};this.bomEditor=blankBomEditor();this.drawingFile=null;return true;}};
      assert.equal(await ctx.openBomChildEditor({component_product_id:2}),true);
      assert.equal(ctx.productForm.id,2);
      await ctx.returnFromCommonBoxEditor(2,{saved:false});
      assert.equal(ctx.productForm,form);assert.equal(ctx.bomEditor,bom);
      assert.equal(ctx.bomEditor.components[0].quantity_per_set,9);
      assert.equal(ctx.productEditReturnContext,previous);
      assert.equal(ctx.productFormSnapshot,'original-form');
      assert.equal(ctx.drawingFile.realFile,true);
      assert.equal(ctx.modal.title,'父产品');
    """)


def test_child_save_refreshes_name_not_parent_unsaved_quantity():
    run_js("""
      const bom=blankBomEditor();bom.components=[{component_product_id:2,quantity_per_set:9}];
      const ctx={...methods,productForm:{id:1},bomEditor:bom,productEditReturnContext:null,
        modal:{type:'product'},masterEditBaseline:{product:{version:7}},canEditProducts:true,
        masterSavePending:false,spec:()=>'',showToast:()=>{},openProduct:async function(){
          this.productForm={id:2};this.bomEditor=blankBomEditor();return true;}};
      await ctx.openBomChildEditor({component_product_id:2});
      await ctx.returnFromCommonBoxEditor(2,{saved:true});
      assert.equal(ctx.bomEditor.components[0].product_name,'新版内衬');
      assert.equal(ctx.bomEditor.components[0].quantity_per_set,9);
      assert.equal(ctx.productForm.id,1);
    """)


def test_new_controls_are_inline_and_no_new_free_text_subkit_button():
    panel = HTML.split('<summary>组合 BOM</summary>', 1)[1].split('</fieldset>', 1)[0]
    assert 'aria-label="BOM库存来源"' in panel
    assert 'aria-label="子件库存关系"' in panel
    assert 'class="bom-component-actions"' in panel
    assert 'openBomChildEditor(component)' in panel
    assert '>组成子套件</button>' not in panel


def test_disable_bom_sends_empty_recipe_without_discarding_local_draft():
    run_js("""
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1}};
      ctx.bomEditor.components=[{...blankBomComponent(),component_product_id:2}];
      ctx.bomEditor.enabled=false;
      assert.deepEqual(ctx.bomPayload().components,[]);
      assert.equal(ctx.bomEditor.components.length,1);
      ctx.bomEditor.enabled=true;
      assert.equal(ctx.bomPayload().components.length,1);
    """)


def test_failed_child_load_restores_parent_and_readonly_never_opens_child():
    run_js("""
      const form={id:1}; const bom=blankBomEditor();
      let opened=0;
      const ctx={...methods,productForm:form,bomEditor:bom,productEditReturnContext:null,
        modal:{type:'product',title:'父产品'},masterEditBaseline:{product:{version:7}},
        canEditProducts:false,masterSavePending:false,openProduct:async function(){
          opened++;this.productForm={id:2};this.bomEditor=blankBomEditor();return false;}};
      assert.equal(await ctx.openBomChildEditor({component_product_id:2}),false);
      assert.equal(opened,0);
      ctx.canEditProducts=true;
      assert.equal(await ctx.openBomChildEditor({component_product_id:2}),false);
      assert.equal(opened,1);assert.equal(ctx.productForm,form);assert.equal(ctx.bomEditor,bom);
      assert.equal(ctx.modal.title,'父产品');assert.equal(ctx.productEditReturnContext,null);
    """)
