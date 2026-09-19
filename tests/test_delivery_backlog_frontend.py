from pathlib import Path
import re
import subprocess


def test_backlog_allocation_preserves_total_and_rejects_stale_form():
    html=Path('static/index.html').read_text(encoding='utf-8')
    methods='backlogStatusLabel('+html.split('          backlogStatusLabel(',1)[1].split('          openTianhuaPreimport(',1)[0]
    script=r'''
const assert=require('node:assert/strict');const ctx=({METHODS});
ctx.user={role:'admin'};ctx.modal={type:'delivery'};ctx.deliveryForm={customer_id:1,lines:[{key:'new',order_item_id:3,delivered_quantity:100,remarks:'keep'}]};
ctx.deliveryFormSignature=()=>JSON.stringify(ctx.deliveryForm);ctx.showToast=()=>{};ctx.errorMessage=e=>e.message;
let seq=0;ctx.createDeliveryLine=r=>({...r,key:'line-'+(++seq)});
const preview=()=>({customer_id:1,originKey:'new',signature:ctx.deliveryFormSignature(),requested_quantity:100,items:[{take:40,ready_quantity:60,stock_code:'P',candidate:{order_item_id:1}},{take:30,ready_quantity:50,stock_code:'P',candidate:{order_item_id:2}}]});
(async()=>{
 ctx.deliveryBacklogs={loading:false,preview:preview()};
 ctx.deliveryForm.lines[0].delivered_quantity=120;
 await ctx.applyDeliveryBacklogs();assert.equal(ctx.deliveryForm.lines.length,1);assert.equal(ctx.deliveryForm.lines[0].delivered_quantity,120);
 ctx.deliveryForm.lines[0].delivered_quantity=100;ctx.deliveryBacklogs.preview=preview();
 await ctx.applyDeliveryBacklogs();assert.deepEqual(ctx.deliveryForm.lines.map(r=>[r.order_item_id,r.delivered_quantity]),[[1,40],[2,30],[3,30]]);
 assert.equal(ctx.deliveryForm.lines[2].remarks,'keep');assert.equal(ctx.deliveryBacklogs.preview,null);
 const before=JSON.stringify(ctx.deliveryForm);await ctx.applyDeliveryBacklogs();assert.equal(JSON.stringify(ctx.deliveryForm),before);
 // Repeated query responses cannot replace a newer page.
 const pending=[];global.axios={get:()=>new Promise(resolve=>pending.push(resolve))};
 ctx.deliveryBacklogs={sequence:0,items:[],loading:false,error:'',history:false};
 const first=ctx.loadDeliveryBacklogs(1);const second=ctx.loadDeliveryBacklogs(2);
 pending[1]({data:{items:[{id:2}],page:2,has_more:false}});await second;
 pending[0]({data:{items:[{id:1}],page:1,has_more:true}});await first;
 assert.equal(ctx.deliveryBacklogs.page,2);assert.equal(ctx.deliveryBacklogs.items[0].id,2);
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('METHODS',methods)
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stdout+result.stderr


def test_modified_inline_javascript_parses(tmp_path):
    for file in ('index.html','mobile_tianhua_pick.html'):
        html=Path('static',file).read_text(encoding='utf-8')
        for i,source in enumerate(re.findall(r'<script\b[^>]*>([\s\S]*?)</script>',html)):
            if not source.strip():continue
            script=tmp_path/f'{file}-{i}.js';script.write_text(source,encoding='utf-8')
            result=subprocess.run(['node','--check',str(script)],capture_output=True,text=True,encoding='utf-8')
            assert result.returncode==0,result.stdout+result.stderr
