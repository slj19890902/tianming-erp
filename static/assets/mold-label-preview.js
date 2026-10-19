(function(global){
  'use strict';
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function nonce(){
    if(typeof crypto!=='undefined'&&typeof crypto.randomUUID==='function')return crypto.randomUUID();
    if(typeof crypto!=='undefined'&&typeof crypto.getRandomValues==='function')return Array.from(crypto.getRandomValues(new Uint8Array(16)),value=>value.toString(16).padStart(2,'0')).join('');
    return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  }
  function create(options){
    const dirty=new Set(), saves=new Map();
    let saving=false, needsFreeze=false, attempt=null;
    const rows=()=>options.rows();
    const selected=row=>Number(row.selected_product_id)||((row.label_product_choices||[]).length===1?Number(row.label_product_choices[0].id):null);
    const mark=row=>{dirty.add(Number(row.mold_id));needsFreeze=true;attempt=null;};
    const status=text=>{const n=document.querySelector('[data-text-status]');if(n)n.textContent=text;};
    function bind(){
      if(!options.enabled())return;
      document.querySelectorAll('#labels [data-mold-id] .mold-layout-text[data-layout-id]').forEach(node=>{
        const row=rows().find(r=>Number(r.mold_id)===Number(node.closest('[data-mold-id]').dataset.moldId));
        if(!row||row.label_text_editable===false)return;
        node.contentEditable='plaintext-only';node.tabIndex=0;node.setAttribute('role','textbox');
        node.setAttribute('aria-label','编辑标签文字：'+(node.dataset.layoutLabel||node.dataset.layoutId));
        let original='';
        node.onfocus=()=>{original=node.innerText??node.textContent;node.textContent=original;};
        node.onkeydown=event=>{
          if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();node.blur();}
          if(event.key==='Escape'){event.preventDefault();node.textContent=original;node.blur();}
        };
        node.onblur=()=>{
          const value=(node.innerText??node.textContent).replace(/\r\n/g,'\n').trim();
          if(value===original.trim())return;
          if(value.length>500){status('每项文字最多500字，请缩短');return;}
          row.label_text_overrides={...(row.label_text_overrides||{}),[node.dataset.layoutId]:value};
          mark(row);status('有修改尚未保存');void options.onChange();
        };
      });
    }
    async function save(onlyId){
      if(saving)throw new Error('正在保存标签文字，请稍候');
      const changed=rows().filter(r=>dirty.has(Number(r.mold_id))&&(!onlyId||Number(r.mold_id)===Number(onlyId)));
      if(!changed.length)return;
      const items=changed.map(row=>{
        const pid=selected(row), choice=(row.label_product_choices||[]).find(p=>Number(p.id)===pid);
        return {mold_id:Number(row.mold_id),product_id:pid,expected_version:Number(row.mold_version),
          expected_product_version:choice?Number(choice.version):null,texts:{...(row.label_text_overrides||{})}};
      });
      const signature=JSON.stringify(items);
      if(!saves.has(signature))saves.set(signature,{items,idempotency_key:'mold-text-'+nonce()});
      saving=true;
      const panel=document.getElementById('moldContentEditor'),labels=document.getElementById('labels');
      panel.inert=true;labels.inert=true;
      try{
        const result=await options.request('/api/warehouse/molds/label-texts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(saves.get(signature))});
        result.items.forEach(saved=>{
          const row=rows().find(r=>Number(r.mold_id)===Number(saved.mold_id));
          row.mold_version=saved.version;row.label_text_overrides={...(saved.effective_texts||saved.texts)};dirty.delete(Number(row.mold_id));
          const choice=(row.label_product_choices||[]).find(p=>Number(p.id)===Number(saved.product_id));
          if(choice)choice.label_text_overrides={...(saved.effective_texts||saved.texts)};
        });
        needsFreeze=true;attempt=null;status('已保存；以后新打印采用这些文字');
      }finally{saving=false;panel.inert=false;labels.inert=false;}
      await options.onChange();
    }
    function render(){
      const panel=document.getElementById('moldContentEditor');if(!panel)return;
      panel.hidden=!options.enabled();if(panel.hidden)return;
      panel.innerHTML='<div>直接点击预览标签上的文字编辑；回车完成，Shift+回车换行，清空可隐藏该项。修改只保存标签显示内容。</div>'+rows().map(row=>{
        const choices=row.label_product_choices||[],pid=selected(row);
        return `<div class="mold-content-row" data-content-mold="${Number(row.mold_id)}"><label>绑定产品<select data-product>${choices.length!==1?'<option value="">全部绑定产品</option>':''}${choices.map(p=>`<option value="${Number(p.id)}" ${pid===Number(p.id)?'selected':''}>${escape(p.customer_short_name)} · ${escape(p.product_code)}</option>`).join('')}</select></label><button class="button secondary" data-save>保存这款标签</button><button class="button secondary" data-reset>恢复默认文字</button></div>`;
      }).join('')+'<button class="button" data-save-print>保存全部修改并打印</button><span data-text-status role="status"></span>';
      panel.querySelectorAll('[data-content-mold]').forEach(node=>{
        const row=rows().find(r=>Number(r.mold_id)===Number(node.dataset.contentMold));
        node.querySelector('[data-product]').onchange=async event=>{
          const next=Number(event.target.value)||null, previous=selected(row);
          try{
            if(dirty.has(Number(row.mold_id)))await save(Number(row.mold_id));
            const choice=(row.label_product_choices||[]).find(p=>Number(p.id)===next);
            if(!choice){
              const loaded=await options.request(`/api/warehouse/molds/${row.mold_id}/label-preview`,{method:'GET'});
              Object.assign(row,loaded,{selected_product_id:null});needsFreeze=true;attempt=null;
              await options.onChange();render();return;
            }
            Object.assign(row,{selected_product_id:next,label_inventory_codes:[choice.product_code],label_inventory_code:choice.product_code,
              label_customer_name:choice.customer_short_name,label_report_specification:choice.report_specification,
              label_mold_count:choice.mold_count,label_title_kind:choice.label_title_kind,label_title_text:choice.label_title_text,
              label_text_overrides:{...(choice.label_text_overrides||{})}});
            needsFreeze=true;attempt=null;await options.onChange();render();
          }catch(error){event.target.value=previous||'';status(error.message);}
        };
        node.querySelector('[data-save]').onclick=()=>void save(Number(row.mold_id)).catch(error=>status(error.message));
        node.querySelector('[data-reset]').onclick=()=>{row.label_text_overrides={};mark(row);status('已恢复预览默认文字；保存后生效');void options.onChange();};
      });
      panel.querySelector('[data-save-print]').onclick=async()=>{
        try{await save();await options.onChange();const button=document.getElementById('printButton');
          if(button.disabled)throw new Error('仍有文字超长，请直接点击对应文字缩短后再打印');button.click();
        }catch(error){status(error.message);}
      };
      bind();
    }
    async function freeze(){
      if(saving)throw new Error('请等待标签文字保存完成');
      await save();if(!needsFreeze)return null;
      const content_options=rows().map(row=>{
        const pid=selected(row),choice=(row.label_product_choices||[]).find(p=>Number(p.id)===pid);
        return {mold_id:Number(row.mold_id),product_id:pid,title_text:null,expected_mold_version:Number(row.mold_version),expected_product_version:choice?Number(choice.version):null};
      });
      if(!attempt)attempt={mold_ids:options.moldIds(),source:options.batch?'batch':'single',template_version:'mold_80x40_v1',content_options,idempotency_key:'mold-preview-'+nonce()};
      const registered=await options.request('/api/warehouse/molds/label-prints',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(attempt)});
      needsFreeze=false;attempt=null;return registered;
    }
    return {render,freeze,bind,save};
  }
  global.TmMoldPreview={create};
})(window);
