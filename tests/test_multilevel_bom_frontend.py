import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")


def test_bom_set_fixed_source_and_legacy_upgrade_are_explained():
    assert '零件组装 · 父件不报料' in HTML
    assert '旧规则 · 请选实际形成方式' in HTML
    assert '先选产品形成方式，再选组装消耗 / 随货配套' in HTML


def method(name):
    pattern = re.compile(r"^          (?:async )?" + re.escape(name) + r"\(", re.M)
    start = pattern.search(HTML).start()
    end = re.compile(r"^          (?:async )?[A-Za-z_$][\w$]*\(", re.M).search(HTML, start + 12).start()
    return HTML[start:end]


def run_js(body):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    names = ["bomComponentUnit", "bomComponentOption", "normalizeBomComponent", "applyBomResponse", "mergeBomComponentOptions",
        "onBomInventoryModeChange", "_productBomSaveFields", "_productBomDirty", "validateProductBom",
        "bomPayload", "openBomChildEditor", "returnFromCommonBoxEditor", "bindBomYieldToCurrentProduct", "selectBomComponent",
        "applyBomChildProduct", "bomComponentProductionText", "bomComponentMoldText"]
    constants = HTML[HTML.index("      let bomComponentKeyCounter"):HTML.index("      const blankMaterial =")]
    script = "const assert=require('node:assert/strict');\n" + constants
    script += "\nconst methods={" + "\n".join(method(name) for name in names) + "};\n"
    script += "const axios={get:async()=>({data:{id:2,product_name:'新版内衬',product_code:'SHARED'}})};\n"
    script += "(async()=>{" + body + "})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node], input=script, text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_external_purchase_can_edit_and_round_trip_real_accompany_bom():
    opening = re.search(r'<details[^>]*>\s*<summary>组合 BOM</summary>', HTML).group()
    assert 'v-if=' not in opening
    run_js("""
      const ctx={...methods,bomEditor:blankBomEditor(),
        productForm:{id:1,supply_mode:'external_purchase',composite_fulfillment_mode:'parent_delivery'},
        spec:()=>'',modal:{type:'product'}};
      ctx.bomEditor.enabled=true;ctx.bomEditor.inventory_mode='purchased';
      ctx.bomEditor.components=[{...blankBomComponent(),component_product_id:2,quantity_per_set:3}];
      ctx.onBomInventoryModeChange();
      assert.equal(ctx.bomEditor.components[0].inventory_relation,'accompany');
      assert.equal(ctx.validateProductBom(),'');
      const payload=ctx.bomPayload(7);
      assert.equal(payload.inventory_mode,'purchased');
      assert.equal(payload.components[0].quantity_per_set,3);
      ctx.applyBomResponse({...payload,version:8,is_composite:true});
      assert.equal(ctx.bomEditor.inventory_mode,'purchased');
      assert.equal(ctx._productBomDirty(),false);
      ctx.bomEditor.components[0].inventory_relation='assembly';
      assert.equal(ctx.validateProductBom(),'');
    """)


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


def test_body_assembly_is_editable_and_invalid_sources_still_rejected():
    run_js("""
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1}};
      ctx.bomEditor.enabled=true;ctx.bomEditor.inventory_mode='manufactured';
      ctx.bomEditor.components=[{...blankBomComponent(),component_product_id:2,inventory_relation:'assembly'}];
      assert.equal(ctx.validateProductBom(),'');
      ctx.bomEditor.inventory_mode='purchased';
      assert.equal(ctx.validateProductBom(),'');
      ctx.bomEditor.inventory_mode='assembled';ctx.productForm.composite_fulfillment_mode='parent_delivery';ctx.bomEditor.components=[];
      assert.match(ctx.validateProductBom(),/至少需要一个组装子件/);
    """)


def test_real_product_use_is_integer_without_changing_legacy_decimal_contract():
    run_js("""
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1,composite_fulfillment_mode:'parent_delivery'}};
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


def test_child_mold_change_refreshes_parent_and_discards_old_mold_capacity():
    run_js("""
      const bom=blankBomEditor();const row={component_product_id:2,quantity_per_set:9,
        is_die_cut:true,mold_tool_id:40,mold_max_yield_per_sheet:4,spare_sheet_quantity:3};
      bom.components=[row];
      const ctx={...methods,productForm:{id:1,customer_id:9},bomEditor:bom,productEditReturnContext:null,
        modal:{type:'product'},masterEditBaseline:{product:{version:7}},canEditProducts:true,
        masterSavePending:false,spec:()=>'',showToast:()=>{},errorMessage:e=>e.message,
        openProduct:async function(){this.productForm={id:2};this.bomEditor=blankBomEditor();return true;}};
      axios.get=async()=>({data:{id:2,customer_id:9,box_category:'die_cut',mold_tool_id:41,is_active:true}});
      await ctx.openBomChildEditor(row);await ctx.returnFromCommonBoxEditor(2,{saved:true});
      assert.equal(row.mold_tool_id,41);assert.equal(row.is_die_cut,true);
      assert.equal(row.mold_max_yield_per_sheet,null);
      assert.equal(row.quantity_per_set,9);assert.equal(row.spare_sheet_quantity,3);
    """)


def test_new_child_cancel_and_save_preserve_parent_then_add_real_child_once():
    run_js("""
      const form={id:1,customer_id:9,product_name:'父件未保存'};
      const bom=blankBomEditor();bom.inventory_mode='assembled';
      const drawing={draft:'未保存图纸'};
      const ctx={...methods,productForm:form,bomEditor:bom,drawingV2:drawing,
        productEditReturnContext:null,modal:{type:'product'},masterEditBaseline:{product:{version:7}},
        canEditProducts:true,masterSavePending:false,spec:()=>'',showToast:()=>{},errorMessage:e=>e.message,
        openProduct:async function(seed){assert.equal(seed.customer_id,9);assert.equal(seed.id,undefined);
          this.productForm={customer_id:9};this.bomEditor=blankBomEditor();this.drawingV2={};return true;}};
      await ctx.openBomChildEditor();await ctx.returnFromCommonBoxEditor(null,{saved:false});
      assert.equal(ctx.productForm,form);assert.equal(ctx.drawingV2,drawing);assert.equal(bom.components.length,0);
      axios.get=async()=>({data:{id:3,customer_id:9,box_category:'die_cut',mold_tool_id:42,is_active:true,unit:'片'}});
      await ctx.openBomChildEditor();await ctx.returnFromCommonBoxEditor(3,{saved:true});
      assert.equal(ctx.productForm,form);assert.equal(bom.components.length,1);
      assert.equal(bom.components[0].component_product_id,3);assert.equal(bom.components[0].mold_tool_id,42);
      assert.equal(bom.components[0].inventory_relation,'assembly');assert.equal(bom.components[0].quantity_per_set,1);
      assert.equal(await ctx.returnFromCommonBoxEditor(3,{saved:true}),false);
      ctx.canEditProducts=false;assert.equal(await ctx.openBomChildEditor(),false);
    """)


def test_child_readback_failure_blocks_parent_and_late_result_cannot_touch_other_editor():
    run_js("""
      const bom=blankBomEditor();bom.enabled=true;const row={component_product_id:2,quantity_per_set:1};bom.components=[row];
      const ctx={...methods,productForm:{id:1},bomEditor:bom,productEditReturnContext:null,
        modal:{type:'product'},masterEditBaseline:{},canEditProducts:true,masterSavePending:false,
        spec:()=>'',showToast:()=>{},errorMessage:e=>e.message,
        openProduct:async function(){this.productForm={id:2};this.bomEditor=blankBomEditor();return true;}};
      await ctx.openBomChildEditor(row);axios.get=async()=>{throw new Error('断网')};
      await ctx.returnFromCommonBoxEditor(2,{saved:true});assert.match(ctx.validateProductBom(),/未成功回读/);
      await ctx.openBomChildEditor(row);let resolve;axios.get=()=>new Promise(r=>{resolve=r});
      const pending=ctx.returnFromCommonBoxEditor(2,{saved:true});assert.match(ctx.validateProductBom(),/正在回读/);
      const other=blankBomEditor();ctx.bomEditor=other;ctx.productForm={id:99};
      resolve({data:{id:2,box_category:'die_cut',mold_tool_id:50}});await pending;
      assert.equal(ctx.bomEditor,other);assert.equal(other.components.length,0);assert.equal(row.mold_tool_id,undefined);
    """)


def test_ab_production_summary_uses_explicit_yields_and_scoped_readable_location():
    run_js("""
      const ctx={...methods,moldTools:[{id:41,display_name:'测试客户 A模',mold_code:'INTERNAL-HIDDEN',
        label_overrides:{product_name:'A模'},location_guide:{prompt:'一楼模具左架第二层'}}]};
      const row={mold_tool_id:41,component:{report_length_mm:800,report_width_mm:600,production_process:'模切',
        sheet_cutting_settings:{schema_version:2,whole:{length_parts:2,width_parts:1,mold_count:3,is_die_cut:true}}}};
      assert.match(ctx.bomComponentProductionText(row),/800×600mm.*模切.*每张出 6 片/);
      assert.match(ctx.bomComponentMoldText(row),/用途：A模.*一楼模具左架第二层/);
      assert.doesNotMatch(ctx.bomComponentMoldText(row),/INTERNAL/);
      row.component.sheet_cutting_settings=null;
      assert.match(ctx.bomComponentProductionText(row),/出数待完善/);
    """)


def test_new_controls_are_inline_and_no_new_free_text_subkit_button():
    panel = HTML.split('<summary>组合 BOM</summary>', 1)[1].split('</fieldset>', 1)[0]
    assert 'aria-label="BOM库存来源"' in panel
    assert 'aria-label="子件库存关系"' in panel
    assert 'class="bom-component-actions"' in panel
    assert 'openBomChildEditor(component)' in panel
    assert '>组成子套件</button>' not in panel
    assert 'aria-label="子件备用纸张"' in panel
    assert 'aria-label="子件最大模切出数"' in panel
    assert 'v-show="component._planningOpen"' in panel


def test_yield_edit_reads_current_real_mold_and_round_trips_limits():
    run_js("""
      const row={...blankBomComponent(),component_product_id:2,inventory_relation:'assembly',
        mold_max_yield_per_sheet:4,spare_sheet_quantity:5};
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1,customer_id:9,composite_fulfillment_mode:'parent_delivery'},errorMessage:e=>e.message,spec:()=>''};
      ctx.bomEditor.enabled=true;ctx.bomEditor.inventory_mode='assembled';ctx.bomEditor.components=[row];
      axios.get=async()=>({data:{id:2,customer_id:9,is_active:true,box_category:'die_cut',mold_tool_id:40}});
      await ctx.bindBomYieldToCurrentProduct(row);
      assert.equal(row.mold_tool_id,40);assert.equal(row.is_die_cut,true);
      assert.equal(ctx.validateProductBom(),'');
      const sent=ctx.bomPayload().components[0];
      assert.equal(sent.mold_max_yield_per_sheet,4);assert.equal(sent.spare_sheet_quantity,5);
      ctx.applyBomResponse({is_composite:true,inventory_mode:'assembled',components:[sent],version:8});
      assert.equal(ctx.bomEditor.components[0].mold_max_yield_per_sheet,4);
      assert.equal(ctx.bomEditor.components[0].spare_sheet_quantity,5);
      ctx.bomEditor.components[0].spare_sheet_quantity='';
      assert.equal(ctx.bomPayload().components[0].spare_sheet_quantity,0);
    """)


def test_yield_lookup_failure_and_stale_response_do_not_overwrite_another_product():
    run_js("""
      const row={...blankBomComponent(),component_product_id:2,inventory_relation:'assembly',mold_max_yield_per_sheet:4};
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1,customer_id:9,composite_fulfillment_mode:'parent_delivery'},errorMessage:e=>e.message};
      ctx.bomEditor.enabled=true;ctx.bomEditor.inventory_mode='assembled';ctx.bomEditor.components=[row];
      let resolve;axios.get=()=>new Promise(r=>{resolve=r});
      const pending=ctx.bindBomYieldToCurrentProduct(row);
      assert.match(ctx.validateProductBom(),/正在核对/);
      row.component_product_id=3;
      resolve({data:{id:2,customer_id:9,box_category:'die_cut',mold_tool_id:40}});await pending;
      assert.equal(row.mold_tool_id,null);
      axios.get=async()=>{throw new Error('读取失败')};
      await ctx.bindBomYieldToCurrentProduct(row);
      assert.equal(ctx.validateProductBom(),'读取失败');
      row.mold_max_yield_per_sheet='';await ctx.bindBomYieldToCurrentProduct(row);
      assert.equal(row._yieldError,'');assert.equal(row._yieldLoading,false);
    """)


def test_invalid_bom_is_checked_before_product_or_drawing_write():
    block = HTML[HTML.index('                const productDirty = this._productFormDirty();'):]
    assert block.index('this.validateProductBom()') < block.index('this.buildProductWritePayload(masterOptions)')


def test_product_reselection_does_not_reuse_previous_mold_or_yield():
    run_js("""
      const row={...blankBomComponent(),component_product_id:2,is_die_cut:true,mold_tool_id:40,mold_max_yield_per_sheet:4,quantity_per_set:6};
      const ctx={...methods,bomEditor:blankBomEditor(),productForm:{id:1},spec:()=>''};
      ctx.bomEditor.components=[row];ctx.bomEditor.componentOptions=[{id:3,box_category:'normal',product_name:'普通子件'}];
      await ctx.selectBomComponent(row,3);
      assert.equal(row.component_product_id,3);assert.equal(row.is_die_cut,false);
      assert.equal(row.mold_tool_id,null);assert.equal(row.mold_max_yield_per_sheet,null);
      assert.equal(row.quantity_per_set,6);
    """)


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


def test_new_parent_cannot_drop_unsaved_bom_on_first_save():
    run_js("""
      const ctx={...methods,modal:{type:'product'},productForm:{customer_id:9},
        productBomSnapshot:null,bomEditor:blankBomEditor()};
      assert.equal(ctx._productBomDirty(),false);
      ctx.bomEditor.enabled=true;assert.equal(ctx._productBomDirty(),true);
      assert.match(ctx.validateProductBom(),/至少添加/);
      ctx.bomEditor.components=[{...blankBomComponent(),component_product_id:3}];
      assert.equal(ctx.validateProductBom(),'');assert.equal(ctx.bomPayload(1).components.length,1);
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
