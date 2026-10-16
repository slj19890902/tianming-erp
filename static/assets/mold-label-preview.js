(function(global){
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function create(options){
    let dirty=false, attempt=null, saving=false;
    const saves=new Map();
    const rows=()=>options.rows();
    const changed=()=>{dirty=true;attempt=null;void options.onChange();};
    function render(){
      const panel=document.getElementById('moldContentEditor');
      if(!panel)return;
      panel.hidden=!options.enabled();
      if(panel.hidden)return;
      panel.innerHTML=rows().map((row,index)=>{
        const choices=row.label_product_choices||[], selected=Number(row.selected_product_id)|| (choices.length===1?Number(choices[0].id):0);
        const editable=row.label_title_kind==='name' && selected>0;
        return `<div class="mold-content-row" data-content-row="${index}"><label>绑定产品<select data-product>${choices.length>1?'<option value="">全部绑定产品（选择一款可单独打印）</option>':''}${choices.map(p=>`<option value="${Number(p.id)}" ${selected===Number(p.id)?'selected':''}>${escape(p.customer_short_name)} · ${escape(p.product_code)}${p.drawing_number?' · 图号 '+escape(p.drawing_number):''}</option>`).join('')}</select></label><label>${row.label_title_kind==='drawing'?'图号':'产品标签简称'}<input data-title maxlength="250" value="${escape(row.label_title_text)}" ${editable?'':'disabled'}></label><button class="button secondary" data-save ${editable?'':'disabled'}>保存该产品标签简称</button><span data-save-status></span></div>`;
      }).join('');
      panel.querySelectorAll('[data-content-row]').forEach(node=>{
        const row=rows()[Number(node.dataset.contentRow)], choices=row.label_product_choices||[];
        node.querySelector('[data-product]').onchange=event=>{
          const choice=choices.find(p=>Number(p.id)===Number(event.target.value));
          if(!choice){ options.onReload(); return; }
          Object.assign(row,{selected_product_id:Number(choice.id),label_inventory_codes:[choice.product_code],label_inventory_code:choice.product_code,label_customer_name:choice.customer_short_name,label_report_specification:choice.report_specification,label_mold_count:choice.mold_count,label_title_kind:choice.label_title_kind,label_title_text:choice.label_title_text});
          changed();render();
        };
        node.querySelector('[data-title]').oninput=event=>{row.label_title_text=event.target.value;changed();};
        node.querySelector('[data-save]').onclick=async event=>{
          if(saving)return;
          const choice=choices.find(p=>Number(p.id)===Number(row.selected_product_id)) || (choices.length===1?choices[0]:null);
          if(!choice || row.label_title_kind!=='name')return;
          const title=String(row.label_title_text||'').trim();
          if(!title){node.querySelector('[data-save-status]').textContent='请先填写标签简称';return;}
          const signature=JSON.stringify([row.mold_id,choice.id,row.mold_version,title]);
          if(!saves.has(signature))saves.set(signature,{product_id:Number(choice.id),expected_version:Number(row.mold_version),title_text:title,idempotency_key:'mold-title-'+crypto.randomUUID()});
          saving=true;event.target.disabled=true;
          try{
            const result=await options.request(`/api/warehouse/molds/${row.mold_id}/label-title`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(saves.get(signature))});
            row.mold_version=result.version; choice.label_title_text=result.title_text;
            node.querySelector('[data-save-status]').textContent='已保存';
          }catch(error){node.querySelector('[data-save-status]').textContent=error.message;}
          finally{saving=false;event.target.disabled=false;}
        };
      });
    }
    async function freeze(){
      if(saving)throw new Error('请等待标签简称保存完成');
      if(!dirty)return null;
      const content_options=rows().map(row=>{
        const choice=(row.label_product_choices||[]).find(p=>Number(p.id)===Number(row.selected_product_id)) || ((row.label_product_choices||[]).length===1?row.label_product_choices[0]:null);
        return {mold_id:Number(row.mold_id),product_id:choice?Number(choice.id):null,title_text:row.label_title_kind==='name'?String(row.label_title_text||'').trim():null,expected_mold_version:Number(row.mold_version),expected_product_version:choice?Number(choice.version):null};
      });
      if(content_options.some(x=>x.title_text===''))throw new Error('请先填写标签简称，再打印');
      const body={mold_ids:options.moldIds(),source:options.batch?'batch':'single',template_version:'mold_80x40_v1',content_options};
      if(!attempt)attempt={...body,idempotency_key:'mold-preview-'+crypto.randomUUID()};
      const registered=await options.request('/api/warehouse/molds/label-prints',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(attempt)});
      dirty=false;attempt=null;return registered;
    }
    return {render,freeze};
  }
  global.TmMoldPreview={create};
})(window);
