import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const code=readFileSync(new URL('../../../static/assets/mold-label-preview.js',import.meta.url),'utf8');
function fixture(insecure=false){
  const rows=[1,2].map(id=>({mold_id:id,mold_version:4,label_text_editable:true,label_product_choices:[{id:id+10,version:7}],label_text_overrides:{}}));
  const nodes=rows.map(row=>({dataset:{layoutId:'inventory_code_side'},textContent:'21301834',setAttribute(){},closest(){return {dataset:{moldId:String(row.mold_id)}}}}));
  const panel={},labels={},status={},calls=[];let uuid=0,failSave=false,failFreeze=false;
  const window={},document={querySelectorAll:()=>nodes,querySelector:()=>status,getElementById:id=>id==='labels'?labels:panel};
  vm.runInNewContext(code,{window,document,crypto:insecure?{getRandomValues:bytes=>{bytes.fill(++uuid);return bytes}}:{randomUUID:()=>String(++uuid)}});
  const editor=window.TmMoldPreview.create({enabled:()=>true,rows:()=>rows,moldIds:()=>[1,2],batch:true,onChange:async()=>{},request:async(url,init)=>{
    const payload=JSON.parse(init.body);calls.push({url,payload});
    if(url.endsWith('label-texts')){if(failSave){failSave=false;throw Error('network uncertain');}return {items:payload.items.map(item=>({...item,version:item.expected_version+1,effective_texts:item.texts}))};}
    if(failFreeze){failFreeze=false;throw Error('network uncertain');}return {print_job_id:23};
  }});editor.bind();
  function edit(index,value){nodes[index].onfocus();nodes[index].textContent=value;nodes[index].onblur();}
  return {editor,rows,nodes,calls,edit,failSave:()=>{failSave=true},failFreeze:()=>{failFreeze=true}};
}
test('批量预览逐张修改，单张保存不覆盖另一张，打印冻结采用最新版本',async()=>{
  const f=fixture();f.edit(0,'1834');f.edit(1,'');await f.editor.save(1);
  assert.equal(f.calls[0].payload.items.length,1);assert.equal(f.rows[0].mold_version,5);assert.equal(f.rows[1].mold_version,4);
  await f.editor.freeze();assert.equal(f.calls[1].payload.items[0].mold_id,2);assert.equal(f.calls[1].payload.items[0].texts.inventory_code_side,'');
  const last=f.calls[2].payload;assert.deepEqual(last.content_options.map(i=>i.expected_mold_version),[5,5]);assert.deepEqual(last.content_options.map(i=>i.product_id),[11,12]);
});
test('未知保存与冻结结果重试复用原凭证，不生成重复任务',async()=>{
  const f=fixture();f.edit(0,'短码');f.failSave();await assert.rejects(f.editor.save(),/uncertain/);await f.editor.save();
  assert.deepEqual(f.calls[0].payload,f.calls[1].payload);
  f.failFreeze();await assert.rejects(f.editor.freeze(),/uncertain/);await f.editor.freeze();assert.deepEqual(f.calls[2].payload,f.calls[3].payload);
});
test('ESC取消文字修改，客户范围只读标签不绑定编辑',async()=>{
  const f=fixture();f.nodes[0].onfocus();f.nodes[0].textContent='取消此修改';f.nodes[0].blur=()=>f.nodes[0].onblur();
  f.nodes[0].onkeydown({key:'Escape',preventDefault(){}});await f.editor.save();assert.equal(f.calls.length,0);
  f.rows[1].label_text_editable=false;delete f.nodes[1].onfocus;f.editor.bind();assert.equal(f.nodes[1].onfocus,undefined);
});

test('局域网HTTP没有randomUUID仍能保存与冻结，重试保留同一凭证',async()=>{
  const f=fixture(true);f.edit(0,'局域网短码');f.failSave();await assert.rejects(f.editor.save());await f.editor.save();await f.editor.freeze();
  assert.deepEqual(f.calls[0].payload,f.calls[1].payload);assert.match(f.calls[0].payload.idempotency_key,/^mold-text-[0-9a-f]{32}$/);
  assert.match(f.calls[2].payload.idempotency_key,/^mold-preview-[0-9a-f]{32}$/);
});
