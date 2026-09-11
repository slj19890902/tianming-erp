// Admin-only cost maintenance. Existing stock changes require a separate exact preview.
export async function editCostRule(host, row, refresh) {
  const el=(tag,text)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;};
  const dialog=el('dialog'); dialog.className='cost-editor';
  const style=el('style',`.cost-editor{font:14px/1.5 system-ui;color:#24433b;border:1px solid #b6d2ca;border-radius:10px;width:580px;max-width:95vw;padding:16px;max-height:90vh;overflow:auto}.cost-editor::backdrop{background:#19362c66}.cost-editor h3{margin:0 0 8px}.cost-editor form{display:grid;grid-template-columns:1fr 1fr;gap:10px}.cost-editor label{display:block}.cost-editor input,.cost-editor select,.cost-editor textarea{width:100%;font:inherit;padding:7px;margin:2px 0;border:1px solid #b5c9c3;border-radius:5px}.cost-editor .wide{grid-column:1/-1}.cost-editor .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.cost-editor button{padding:8px 12px;border:1px solid #8daf9f;border-radius:5px;cursor:pointer;background:#f1f8f3;color:#174732}.cost-editor button.primary{background:#126e51;color:white}.cost-editor button:disabled{opacity:.5;cursor:default}.cost-editor .notice{white-space:pre-wrap;margin-top:10px}.cost-editor .error{color:#b42318}`);
  style.textContent += '.cost-editor [hidden]{display:none!important}';
  const title=el('h3',`成本依据 · ${row.customer_name||''} ${row.product_code} ${row.product_name}`);
  const note=el('p','只维护盘点成本，不修改生产尺寸、数量或已出货成本。');
  const status=el('div');status.className='notice';status.setAttribute('role','status');
  const form=el('form'); const actions=el('div');actions.className='actions';
  const close=el('button','关闭');close.type='button';close.onclick=()=>dialog.close();
  dialog.append(style,title,note,form,actions,status);host.append(dialog);
  dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
  const api=async(path,method='GET',body)=>{const r=await fetch('/api/warehouse/cost-rules/'+path,{method,credentials:'same-origin',cache:'no-store',headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'请核对填写内容');return d;};
  let data, materials, saved=false, plan=null, requestId=null;
  const showError=e=>{status.textContent=e.message;status.className='notice error';};
  try {[data,materials]=await Promise.all([api(row.product_id),api('materials')]);} catch(e){showError(e);actions.append(close);return;}
  const cfg=data.config;
  function field(label,name,type='text',wide=false){const wrap=el('label',label);if(wide)wrap.className='wide';const n=el(type==='select'?'select':'input');n.name=name;if(type!=='select')n.type=type;n.value=cfg[name]??'';wrap.append(n);form.append(wrap);return n;}
  const mode=field('计价方式','mode','select');
  for(const [value,label] of [['auto','自动：材料优先，缺价用售价'],['material','按纸板平方价'],['fixed','外购 / 分摊单价（含税）'],['sale','按当前售价参考']]){const o=el('option',label);o.value=value;mode.append(o);}mode.value=cfg.mode;
  field('采购渠道','purchase_channel');
  const mat=field('供应商 · 材质','material_id','select',true);const empty=el('option','选择材质');empty.value='';mat.append(empty);
  for(const m of materials){const o=el('option',`${m.supplier_name} · ${m.code}`);o.value=m.id;mat.append(o);}mat.value=cfg.material_id||'';
  const flute=field('楞型','flute_type','select');for(const v of ['','A','B','C','E','F','AB','BC','BE','AE','AC']){const o=el('option',v||'沿用产品楞型');o.value=v;flute.append(o);}flute.value=cfg.flute_type||'';
  const price=field('每件含税单价（元）','unit_cost','number');price.step='.0001';price.min='.0001';
  const length=field('报料长（毫米）','length_mm','number'),width=field('报料宽（毫米）','width_mm','number');
  const sheets=field('每件用纸张数','sheets_per_product','number'),yieldCount=field('每张出几个','products_per_sheet','number');
  for(const n of [length,width,sheets,yieldCount])n.min='1';
  const basis=field('本次成本依据','basis','text',true);basis.maxLength=1000;basis.required=true;
  const save=el('button','保存后续入库规则');save.type='submit';save.className='primary';form.append(save);
  const preview=el('button','预览本批次新成本');preview.type='button';preview.disabled=true;
  const apply=el('button','确认调整本批次');apply.type='button';apply.disabled=true;
  if(row.can_revalue)actions.append(preview,apply);actions.append(close);
  const visibility=()=>{const material=mode.value==='material';for(const n of [mat,flute,length,width,sheets,yieldCount]){n.closest('label').hidden=!material;n.disabled=!material;}price.closest('label').hidden=mode.value!=='fixed';price.disabled=mode.value!=='fixed';};visibility();
  form.addEventListener('input',()=>{saved=false;plan=null;preview.disabled=true;apply.disabled=true;visibility();});
  const config=()=>({mode:mode.value,purchase_channel:form.elements.purchase_channel.value.trim(),basis:basis.value.trim(),
    material_id:mode.value==='material'?Number(mat.value)||null:null,flute_type:mode.value==='material'?(flute.value||null):null,
    length_mm:mode.value==='material'?(length.value||null):null,width_mm:mode.value==='material'?(width.value||null):null,
    sheets_per_product:mode.value==='material'?Number(sheets.value):1,products_per_sheet:mode.value==='material'?Number(yieldCount.value):1,
    unit_cost:mode.value==='fixed'?(price.value||null):null,temporary:mode.value==='sale'||cfg.temporary,
    evidence:{maintenance_source:'管理员成本页面维护',previous_rule_version:data.version}});
  form.onsubmit=async e=>{e.preventDefault();save.disabled=true;status.className='notice';try{data=await api(row.product_id,'PUT',{config:config(),expected_version:data.version,expected_product_version:data.product_version});saved=true;preview.disabled=false;status.textContent='已保存，后续盘点入库使用新规则。现有库存尚未改价。';}catch(e){showError(e);}finally{save.disabled=false;}};
  preview.onclick=async()=>{if(!saved)return;preview.disabled=true;try{plan=await api(row.product_id+'/preview','POST',{lot_ids:[row.lot_id]});requestId=globalThis.crypto.randomUUID?.()||`cost-${Date.now()}-${Math.random().toString(16).slice(2)}`;const p=plan.rows[0];status.className='notice';status.textContent=`仅本批次 ${row.lot_number}，${p.quantity}件\n原单价：${p.before.estimated_unit_cost_snapshot||'未定价'} → 新单价：${p.unit_cost} 元\n数量、位置和已冻结的出库成本不变。`;apply.disabled=false;}catch(e){showError(e);}finally{preview.disabled=false;}};
  apply.onclick=async()=>{if(!plan)return;apply.disabled=true;save.disabled=true;preview.disabled=true;try{await api(row.product_id+'/apply','POST',{lot_ids:[row.lot_id],expected:plan.fingerprint,batch_id:requestId});dialog.close();refresh();}catch(e){showError(e);apply.disabled=false;}finally{save.disabled=false;preview.disabled=false;}};
}
