/* Shared desktop/mobile editor. All writes use the existing master-data gates. */
(() => {
  'use strict';
  const fields = {
    length_mm:'成品长（毫米）', width_mm:'成品宽（毫米）', height_mm:'成品高（毫米）',
    report_length_mm:'展开纸板长（毫米）', report_width_mm:'展开纸板宽（毫米）',
    base_report_length_mm:'底片长（天地盖，毫米）', base_report_width_mm:'底片宽（毫米）',
    crease_left_mm:'压线左（毫米）', crease_middle_mm:'压线中（毫米）', crease_right_mm:'压线右（毫米）',
    base_crease_left_mm:'底片压线左', base_crease_middle_mm:'底片压线中', base_crease_right_mm:'底片压线右',
  };
  const el = (tag, text) => { const n=document.createElement(tag); if(text != null)n.textContent=text; return n; };
  async function api(path, method='GET', body) {
    const r=await fetch(path,{method,credentials:'same-origin',cache:'no-store',
      headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});
    const data=await r.json();
    if(!r.ok){ const e=new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||'请求失败')); e.status=r.status; throw e; }
    return data;
  }
  const costPath=(id,stage)=>`/api/warehouse/cost-rules/${id}/entry-preview?stock_stage=${encodeURIComponent(stage||'complete')}`;
  function costText(data) {
    if(data.unit_cost==null)return '成本资料待补：'+(data.missing||[]).join('；');
    const e=data.evidence||{};
    return `${e.cost_label||'材料参考成本'}：¥${data.unit_cost} / 件。${data.note}`;
  }
  let opened=false;
  async function open({productId, stockStage='complete'}) {
    if(opened)return null;
    if(!Number.isSafeInteger(Number(productId))||Number(productId)<=0)throw new Error('请先选择常用箱产品');
    opened=true;
    const dialog=el('dialog'), form=el('form'), title=el('h3','正在读取常用箱…'), note=el('p','保存到常用箱，供后续入仓复用；已入库、订单及送货冻结资料保持原记录。'), status=el('p');
    dialog.style.cssText='width:min(700px,94vw);max-height:90vh;overflow:auto;border:1px solid #ccd5e0;border-radius:12px;padding:18px;color:#172033';
    const grid=el('div');grid.style.cssText='display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px';
    const save=el('button','核对并保存'), close=el('button','关闭'), actions=el('div');
    save.type='submit';save.className='btn primary';close.type='button';close.className='btn';
    actions.style.cssText='display:flex;gap:12px;margin-top:16px';actions.append(save,close);
    form.append(title,note,grid,status,actions);dialog.append(form);document.body.append(dialog);dialog.showModal();
    let busy=true, saved=null;
    const done=new Promise(resolve=>{
      const finish=()=>{if(busy)return;dialog.close();dialog.remove();opened=false;resolve(saved);};
      close.onclick=finish;dialog.oncancel=e=>{e.preventDefault();finish();};
    });
    function field(label,input){const wrap=el('label',label);input.style.cssText='display:block;width:100%;padding:8px;box-sizing:border-box';wrap.append(input);grid.append(wrap);return input;}
    save.disabled=true;
    try {
      const [product,materials]=await Promise.all([api(`/api/master/products/${productId}`),api('/api/warehouse/cost-rules/materials')]);
      title.textContent=`编辑常用箱 · ${product.product_code} · ${product.product_name}`;
      const material=field('供应商材质（含报价关联）',el('select'));
      material.append(new Option('请选择有效供应商材质',''));
      materials.forEach(m=>material.append(new Option(`${m.supplier_name} · ${m.code}`,m.id)));
      if(product.material_id&&!materials.some(m=>m.id===product.material_id))material.append(new Option('当前材质已停用或非人民币报价',product.material_id));
      material.value=product.material_id||'';
      const inputs={};Object.entries(fields).forEach(([key,label])=>{
        const input=field(label,el('input'));input.type='number';input.min='1';input.step='1';input.value=product[key]??'';inputs[key]=input;
      });
      const flute=field('楞型',el('select'));
      ['', 'NONE','A','B','C','E','F','AB','BC','BE','AE','AC','AAA','ABC'].forEach(v=>flute.append(new Option(v==='NONE'?'卡纸':v||'待完善',v)));
      flute.value=product.flute_type||'';
      const layers=field('层数',el('select'));['',1,3,5,7].forEach(v=>layers.append(new Option(v||'待完善',v)));layers.value=product.layer_count||'';
      material.onchange=()=>{const m=materials.find(m=>m.id===Number(material.value));if(m){flute.value=m.flute_type||'';layers.value=m.layer_count||'';}};
      for(const [key,label,values] of [['splice_mode','拼片方式',['single','double']],['default_cutting_mode','每张开料方式',['一开一','一开二','一开三','一开四']],['crease_type','主片压线',['','毛片','净料','压线','其他']],['base_crease_type','底片压线',['','毛片','净料','压线','其他']]]){
        const input=field(label,el('select'));const options=[...new Set([...values,product[key]??''])];options.forEach(v=>input.append(new Option(v==='single'?'单片':v==='double'?'双片':v||'未设置',v)));input.value=product[key]??'';inputs[key]=input;
      }
      const reason=field('修改说明',el('input'));reason.value='入仓前核对常用箱材质及规格';reason.required=true;
      try{status.textContent=costText(await api(costPath(productId,stockStage)));}catch(e){status.textContent='成本预览读取失败：'+e.message;}
      busy=false;save.disabled=false;
      form.onsubmit=async event=>{
        event.preventDefault();if(busy)return;busy=true;save.disabled=true;
        let writing=false;
        try{
          const payload={...product,expected_version:product.version,material_id:Number(material.value)||null,
            flute_type:flute.value||null,layer_count:Number(layers.value)||null,change_reason:reason.value.trim()};
          Object.entries(inputs).forEach(([key,input])=>payload[key]=input.tagName==='SELECT'?(input.value||null):(input.value?Number(input.value):null));
          // Derived by the same production relation shown in the main editor.
          if(payload.splice_mode!==product.splice_mode)payload.pieces_per_box=payload.splice_mode==='double'?2:1;
          const preview=await api(`/api/master/products/${productId}/update-preview`,'POST',payload);
          if(!preview.can_update){status.textContent='资料没有变化，无需重复保存。';return;}
          const changes=Object.entries(preview.changes||{}).map(([key,v])=>`${fields[key]||({material_id:'材质',flute_type:'楞型',layer_count:'层数'}[key])||key}：${v.before??'空'} → ${v.after??'空'}`).join('\n');
          if(!window.confirm(`保存以下常用箱变更？\n${changes}\n${(preview.warnings||[]).map(w=>typeof w==='string'?w:JSON.stringify(w)).join('\n')}`))return;
          payload.confirmation_token=preview.confirmation_token;writing=true;
          saved=await api(`/api/master/products/${productId}`,'PUT',payload);
          save.hidden=true;grid.querySelectorAll('input,select').forEach(n=>n.disabled=true);
          status.textContent='常用箱已保存。';
          try{status.textContent+=' '+costText(await api(costPath(productId,stockStage)));}
          catch(e){status.textContent+=' 成本预览刷新失败，关闭后重新读取即可，无需再次保存。';}
          close.textContent='返回入仓';
        }catch(e){
          status.textContent=writing&&(!e.status||e.status>=500)?'保存结果未确认，请关闭后重新打开核对当前资料，避免重复提交。':e.message;
          if(writing&&(!e.status||e.status>=500))save.hidden=true;
        }finally{busy=false;save.disabled=false;}
      };
    }catch(e){status.textContent=e.message;busy=false;save.hidden=true;}
    return done;
  }
  window.TMEntryProduct={open,preview:async(id,stage)=>costText(await api(costPath(id,stage)))};
})();
