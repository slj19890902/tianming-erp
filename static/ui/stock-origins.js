(function(global){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=v=>Number(v||0).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const time=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'时间未记录';
  const names={all:'全部来源',stocktake:'盘点入库',replenishment:'补库生产入库',order:'订单生产入库',other:'其他／来源待核'};
  let active=null;
  function open({productId,lotId=null,sourceKind='all',label='',request,isCurrent=()=>true}){
    if(!Number.isSafeInteger(Number(productId))||Number(productId)<=0)return null;
    active?.close();
    const focus=global.document.activeElement;
    const host=global.document.createElement('div');host.className='stock-origins';
    host.innerHTML=`<section class="so-dialog" role="dialog" aria-modal="true" aria-label="产品库存来源详情"><header><div><strong>库存来源与批次</strong><small>${esc(label)}</small></div><button type="button" data-so-close>关闭 ×</button></header><div class="so-content" aria-live="polite"></div></section>`;
    global.document.body.append(host);
    const body=host.querySelector('.so-content');
    let live=true,sequence=0,controller=null,page=1,history=false,category=Object.hasOwn(names,sourceKind)?sourceKind:'all',listing=null,mode=lotId?'batch':'list',lastLot=lotId;
    let guard=null;
    const current=()=>live&&isCurrent();
    const handle={close(){if(!live)return;live=false;sequence++;controller?.abort();if(guard!==null)global.clearInterval(guard);host.remove();global.document.removeEventListener('keydown',keyboard,true);if(active===handle)active=null;focus?.focus?.({preventScroll:true});}};
    active=handle;
    if(typeof global.setInterval==='function')guard=global.setInterval(()=>{if(!isCurrent())handle.close();},500);
    async function get(path){
      controller?.abort();controller=new AbortController();const ownedController=controller,serial=++sequence,signal=ownedController.signal;
      const timer=setTimeout(()=>ownedController.abort(),15000);
      try{
        let data;
        if(request)data=await request(path,{signal});
        else{const response=await global.fetch(path,{credentials:'same-origin',cache:'no-store',signal});let payload;try{payload=await response.json();}catch{throw Error('库存详情读取失败，请重试');}if(!response.ok){if(response.status===401)global.erpAuthRequired?.();throw Error(typeof payload.detail==='string'?payload.detail:'无权读取或库存详情读取失败');}data=payload;}
        return current()&&serial===sequence?data:null;
      }catch(e){if(!current()||serial!==sequence)return null;throw e;}finally{clearTimeout(timer);}
    }
    const base='/api/product-workbench/products/'+Number(productId)+'/stock-sources';
    function error(e){if(!current())return;body.innerHTML=`<div class="so-error" role="alert">${esc(e.name==='AbortError'?'读取超时或已取消，请重试':e.message||'读取失败')} <button type="button" data-so-retry>重试</button></div>`;}
    async function list(){mode='list';body.innerHTML='<p role="status">正在读取库存来源…</p>';try{const data=await get(base+'?'+new URLSearchParams({source_kind:category,include_history:String(history),page:String(page),page_size:'20'}));if(!data)return;if(!Array.isArray(data.items)||!data.summary||!data.sources)throw Error('库存来源回执不完整，请重试');listing=data;renderList();}catch(e){error(e);}}
    function renderList(){const d=listing;if(!d)return;body.innerHTML=`<div class="so-summary"><div>现存成品 <b>${num(d.summary.actual)}</b> ${esc(d.unit)}</div><span>可用 ${num(d.summary.available)} · 占用 ${num(d.summary.reserved)}${d.summary.restricted?` · 受限 ${num(d.summary.restricted)}`:''}</span></div><nav class="so-categories" aria-label="库存来源">${Object.entries(names).filter(([k])=>k!=='other'||d.sources.other?.actual||category==='other').map(([key,text])=>`<button type="button" data-so-category="${key}" class="so-source-${key}" aria-pressed="${category===key}">${text}<b>${num(key==='all'?d.summary.actual:d.sources[key]?.actual)}</b></button>`).join('')}</nav><p class="so-muted">分类数量为当前剩余；移库、占用与释放不新增入库数量。</p><label class="so-history"><input type="checkbox" data-so-history ${history?'checked':''}> 包含已送完及历史批次</label><div class="so-table-wrap"><table><thead><tr><th>来源／批次</th><th>现存／可用／占用</th><th>当前位置</th><th>原入库时间</th><th></th></tr></thead><tbody>${d.items.map(r=>`<tr class="so-source-${esc(r.source_kind)}"><td><b>${esc(r.source_label)}</b><small>${esc(r.lot_number||'已授权共用批次')}</small></td><td><b>${num(r.actual)} ${esc(r.unit)}</b><small>可用 ${num(r.available)} · 占用 ${num(r.reserved)}${r.restricted?` · 受限 ${num(r.restricted)}`:''}</small></td><td>${esc(r.location_label||'位置待核')}</td><td>${esc(time(r.entered_at))}<small>库存日期 ${esc(r.stock_date||'未登记')}</small></td><td>${r.detail_allowed?`<button type="button" data-so-batch="${Number(r.lot_id)}">详情</button>`:'<small>原批次详情受归属权限限制</small>'}</td></tr>`).join('')}</tbody></table></div>${!d.items.length?'<p class="so-empty">此分类下没有匹配批次</p>':''}${d.shared_total?`<section class="so-shared"><b>另有已授权共用库存，可用 ${num(d.shared_available)} ${esc(d.unit)}</b><small>不计入以上本客户库存；原批次仍只保留一份。</small>${d.shared_items.map(r=>`<p>${esc(r.source_label)} · ${num(r.actual)} ${esc(r.unit)} · ${esc(r.location_label)}${r.detail_allowed?` <button type="button" data-so-batch="${Number(r.lot_id)}">详情</button>`:' · 原批次详情受归属权限限制'}</p>`).join('')}</section>`:''}<footer><button type="button" data-so-page="${page-1}" ${page<=1?'disabled':''}>上一页</button><span>第 ${page} 页 · ${num(d.total)} 批</span><button type="button" data-so-page="${page+1}" ${d.has_more?'':'disabled'}>下一页</button></footer>`;}
    async function batch(id){mode='batch';lastLot=id;body.innerHTML='<p role="status">正在读取批次时间线…</p>';try{const d=await get(base+'/'+Number(id));if(!d)return;if(!d.batch||!Array.isArray(d.events))throw Error('批次详情回执不完整，请重试');const r=d.batch;body.innerHTML=`<button type="button" data-so-back>← 返回来源列表</button><div class="so-batch-title so-source-${esc(r.source_kind)}"><b>${esc(r.source_label)}</b> · ${esc(r.lot_number||'批次详情')}<small>现存 ${num(r.actual)} ${esc(r.unit)} · 可用 ${num(r.available)} · 占用 ${num(r.reserved)}${r.restricted?` · 受限 ${num(r.restricted)}`:''}</small><small>当前位置：${esc(r.location_label)}</small></div><p class="so-muted">${esc(d.source_note)}</p><ol class="so-timeline">${d.events.map(e=>`<li><time>${esc(time(e.at))}</time><div><b>${esc(e.label)}${e.quantity!==null&&e.quantity!==undefined?' · '+num(e.quantity)+' '+esc(e.unit):''}</b>${e.document?`<small>${esc(e.document)}</small>`:''}${e.material||e.specification?`<p>材质：${esc(e.material||'未记录')}　规格：${esc(e.specification||'未记录')}</p>`:''}${e.location?`<p>位置：${esc(e.location)}</p>`:''}${e.note?`<p>${esc(e.note)}</p>`:''}</div></li>`).join('')}</ol>${d.hidden_sections?.length?'<p class="so-muted">部分单据节点受当前账号权限限制。</p>':''}`;}catch(e){error(e);}}
    function keyboard(e){if(!live)return;if(!isCurrent()){handle.close();return;}if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();handle.close();}if(e.key==='Tab'){const fields=[...host.querySelectorAll('button,input')].filter(el=>!el.disabled),first=fields[0],last=fields[fields.length-1];if(e.shiftKey&&global.document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&global.document.activeElement===last){e.preventDefault();first?.focus();}}}
    global.document.addEventListener('keydown',keyboard,true);
    host.addEventListener('click',e=>{if(e.target===host){handle.close();return;}const b=e.target.closest('button');if(!b)return;if(b.hasAttribute('data-so-close'))handle.close();else if(b.dataset.soCategory){category=b.dataset.soCategory;page=1;list();}else if(b.dataset.soBatch)batch(b.dataset.soBatch);else if(b.dataset.soPage){page=Number(b.dataset.soPage);list();}else if(b.hasAttribute('data-so-back')){mode='list';if(listing)renderList();else list();}else if(b.hasAttribute('data-so-retry'))mode==='batch'?batch(lastLot):list();});
    host.addEventListener('change',e=>{if(e.target.hasAttribute('data-so-history')){history=e.target.checked;page=1;list();}});
    host.querySelector('[data-so-close]').focus();
    if(lotId)batch(lotId);else list();return handle;
  }
  global.ERPStockOrigins={open,close(){active?.close();}};
})(window);
