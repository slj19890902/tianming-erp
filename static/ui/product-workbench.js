(function(global){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number=v=>v===null||v===undefined?'待核':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const list=v=>Array.isArray(v)?v:[];
  const cutLabel=v=>Number.isSafeInteger(v)&&v>0?'一开'+(['','一','二','三','四','五','六','七','八','九','十'][v]||v):'—';
  const pid=p=>Number(p?.product_id||p?.id);
  const fluteLayers={A:3,B:3,E:3,AB:5,BE:5,ABC:7,AAA:7,NONE:1};
  const stockNames={finished:'成品',semi_finished:'片料 / 半成品',processed:'已加工片料',processed_component:'已加工片料'};
  const validId=v=>Number.isSafeInteger(Number(v))&&Number(v)>0;
  const actions=new WeakMap(),navigationRounds=new WeakMap();
  function actionError(message,name='AbortError'){const e=Error(message);e.name=name;return e;}
  function navigationIntent(vm,context){
    const round=(navigationRounds.get(vm)||0)+1;navigationRounds.set(vm,round);
    const previous=actions.get(vm);
    if(previous&&previous!==context)previous.cancel();
    if(context)context.navigation=round;
  }
  function actionContext(vm,origin,kind){
    actions.get(vm)?.cancel();
    const actor=vm.user?.id,auth=vm.authGeneration,controller=new AbortController();
    let active=true,timer,rejectWait,form=kind==='requisition'?vm.stockReplenishmentForm:undefined,modal=vm.modal,arrived=false,bound=false;
    const wait=new Promise((_,reject)=>{rejectWait=reject;});wait.catch(()=>{});
    const context={navigation:navigationRounds.get(vm)||0,page:vm.activePage,
      current(){return active&&actions.get(vm)===context&&actor===vm.user?.id&&auth===vm.authGeneration&&context.navigation===(navigationRounds.get(vm)||0)&&context.page===vm.activePage&&(!origin||arrived||origin.current())&&(form===undefined||vm.stockReplenishmentForm===form)&&(modal===undefined||vm.modal===modal);},
      arrive(page){if(!context.current())return false;context.page=page;arrived=true;origin?.handoff();return true;},
      at(page){return arrived&&context.page===page&&context.current();},
      bind(formValue,modalValue){form=formValue;modal=modalValue;bound=true;},
      formReady(){return bound&&modal?.type==='stockReplenishment'&&context.current();},
      wait(promise){return Promise.race([promise,wait]);},
      cancel(error=actionError('操作已取消')){if(!active)return;active=false;controller.abort();clearTimeout(timer);rejectWait(error);},
      finish(){context.cancel();if(actions.get(vm)===context)actions.delete(vm);},
      async get(url,options={}){
        if(!context.current())throw actionError('操作已取消');
        // 每次读取继承当前axios配置；独立实例不继承全局响应副作用。
        const client=global.axios.create({withCredentials:true,timeout:15000});
        try{const response=await context.wait(client.get(url,{...options,signal:controller.signal}));if(!context.current())throw actionError('操作已取消');return response;}
        catch(e){if(!context.current())throw actionError('操作已取消');const status=e.response?.status;if(status===401&&!global.erpCheckingSession)global.erpAuthRequired?.();if(status===403&&global.erpForbidden){global.erpForbidden(e.response?.data?.detail);e._productActionNotified=true;}throw e;}
      }
    };
    actions.set(vm,context);origin?.onCancel(e=>context.cancel(e));
    timer=setTimeout(()=>{const current=context.current();context.cancel(actionError('读取超时，请关闭窗口后从产品重新打开','TimeoutError'));if(current)vm.showToast('读取超时，请关闭窗口后从产品重新打开',true);},15000);
    return context;
  }
  function drawingItems(p){return list(Array.isArray(p?.drawings)?p.drawings:p?.drawings?.items).filter(d=>
    d.preview_url===`/api/mobile/erp/products/${pid(p)}/drawings/${encodeURIComponent(d.id)}/preview`&&
    d.original_url===`/api/mobile/erp/products/${pid(p)}/drawings/${encodeURIComponent(d.id)}/original`);}
  function stockText(p){const f=p?.inventory?.finished;return f?`${number(f.actual)} ${f.unit||''}`:'无库存查看权限';}
  function positionText(p){return list(p?.inventory_positions).map(row=>`${row.inventory_type==='finished'?'成品':row.inventory_type==='processed_component'?'已加工子件':'片料'} · ${row.location_label||'位置待核对'}（实存 ${number(row.actual)} ${row.unit||''} / 可用 ${number(row.available)}）${row.map_issue?' · '+row.map_issue:''}`).join('；');}
  function criteria(form,mode){
    const d=new FormData(form),result={};
    for(const key of ['q','dimension_basis','length','width','height','material_code','flute_type','layer_count','processed_state','lot_id','min_score']){
      const value=String(d.get(key)||'').trim();if(value)result[key]=value;
    }
    if(mode==='reverse'||mode==='sheets'){
      if(!(Number(result.length)>0&&Number(result.width)>0))throw Error(mode==='sheets'?'请填写需要的片料长、宽':'请填写片料实测长、宽');
      delete result.q;delete result.dimension_basis;delete result.height;
      if(mode==='reverse'&&!result.lot_id&&!fluteLayers[result.flute_type])throw Error('请选择片料楞型');
      if(mode==='sheets')delete result.lot_id;
    }else if(!result.q&&!['length','width','height'].some(k=>Number(result[k])>0))throw Error('请输入客户、存货编码、名称或尺寸');
    return result;
  }
  function locationUrl(row){
    if(validId(row?.mold_id)){const url='/mobile/mold-lookup?mold_id='+Number(row.mold_id)+'&readonly=1';return row.map_url===url?url:null;}
    if(!row?.mapped||!String(row.url||'').startsWith('/warehouse.html?'))return null;
    const id=row?.location_id,raw=row?.floor??row?.warehouse_floor??row?.floor_code;
    const floor=String(raw||'').replace(/F$/i,'');
    if(!validId(id)||!['1','3','4'].includes(floor))return null;
    const q=new URLSearchParams({embedded:'1',tab:'map',view:'2d',mode:'lookup',readonly:'1',floor:floor+'F',location_id:String(id)});
    if(validId(row.lot_id))q.set('lot_id',String(row.lot_id));
    return '/warehouse.html?'+q;
  }
  function mount({container,request,onOpen=()=>{},onState=()=>{},onAction,initialState={},lazyDrawings=false,externalSearch=false}){
    let live=true,serial=0,operation=null,actionBusy=false,action=null,stockDialog=null;
    let mode=['reverse','sheets'].includes(initialState.mode)?initialState.mode:'search',params=initialState.params||{},page=initialState.page||1;
    let draft={...(initialState.draft||params)},resetDraft=false;
    let sheetSearch=initialState.sheetSearch||null;
    let result=null,detail=null,tab=initialState.tab||'production',busy=false,error='',mapRow=null,includeHistory=false,lastProductId=null,purpose=false,retryForm=false;
    function captureDraft(){const form=container.querySelector('form');if(form)form.querySelectorAll('[name]').forEach(e=>{draft[e.name||e.getAttribute('name')]=e.value;});}
    const store=()=>onState({mode,sheetSearch,params:{...params},draft:{...draft},page,tab,productId:detail?pid(detail.product):null});
    const cancelAction=preserve=>{if(!action||(preserve&&action.handedOff))return;action.cancel();};
    const stop=preserve=>{serial++;operation?.cancel();operation=null;busy=false;cancelAction(preserve);};
    const current=loaded=>live&&loaded&&loaded.token===serial&&detail&&pid(detail.product)===loaded.id;
    const disposeDrawings=()=>{if(lazyDrawings)global.TmProductDrawings?.disposeWithin(container);};
    const savedId=initialState.productId;
    function validateBody(body,id){
      const object=v=>!!v&&typeof v==='object'&&!Array.isArray(v);
      const inventoryComplete=v=>object(v.summary)&&object(v.groups)&&['finished','semi_finished','processed_component'].every(k=>{
        const summary=v.summary[k],group=v.groups[k];
        return object(summary)&&['actual','available','reserved'].every(n=>typeof summary[n]==='number'&&Number.isFinite(summary[n])&&summary[n]>=0)&&typeof summary.unit==='string'&&object(group)&&Array.isArray(group.positions);
      });
      if(!object(body))throw Error('产品资料回执不完整，请重试');
      if(id){if(!object(body.product)||pid(body.product)!==id||!object(body.production)||!object(body.actions)||!object(body.inventory)||!object(body.orders)||!Array.isArray(body.inventory.items)||!Array.isArray(body.orders.items)||!['molds','bom','process_steps'].every(k=>Array.isArray(body.production[k]))||(body.inventory.visibility!=='hidden_by_permission'&&!inventoryComplete(body.inventory)))throw Error('产品资料回执不完整，请重试');}
      else if(!Array.isArray(body.items)||!Number.isSafeInteger(body.total)||body.total<0||!Number.isInteger(body.page)||body.page<=0||!Number.isInteger(body.page_size)||body.page_size<=0||typeof body.has_more!=='boolean'||body.items.some(p=>!object(p)||!validId(mode==='sheets'?p.lot_id:pid(p))))throw Error('查询回执不完整，请重试');
    }
    const get=async(path,id=null)=>{stop();busy=true;error='';retryForm=false;const token=serial,controller=new AbortController();let timer,rejectWait,failed=false;
      const wait=new Promise((_,reject)=>{rejectWait=reject;timer=setTimeout(()=>{const e=Error('网络较慢，读取超时，请重试');e.name='TimeoutError';reject(e);},15000);});
      const op={cancel(){clearTimeout(timer);controller.abort();const e=Error('读取已取消');e.name='AbortError';rejectWait(e);}};operation=op;render();
      try{const body=await Promise.race([request(path,{signal:controller.signal}),wait]);if(!live||token!==serial)return null;validateBody(body,id);return {body,token};}
      catch(e){if(live&&token===serial&&e.name!=='AbortError'){failed=true;error=e.message||'查询失败，请重试';}return null;}
      finally{clearTimeout(timer);if(operation===op){operation=null;busy=false;controller.abort();if(failed)serial++;}}};
    async function search(next=1){page=next;detail=null;lastProductId=null;mapRow=null;result=null;onOpen(true);store();
      const q=new URLSearchParams({...params,page:String(page),page_size:'12'});
      const loaded=await get('/api/product-workbench/'+mode+'?'+q);
      if(loaded&&loaded.token===serial){result=loaded.body;store();}if(live)render();
    }
    async function show(id,activityPage=1){if(!validId(id))return;const sameProduct=lastProductId===Number(id),previousTab=tab;lastProductId=Number(id);detail=null;mapRow=null;purpose=false;onOpen(true);
      const query=new URLSearchParams();if(activityPage>1)query.set('activity_page',String(activityPage));if(includeHistory)query.set('include_history','true');
      const loaded=await get('/api/product-workbench/products/'+Number(id)+(query.size?'?'+query:''),Number(id));
      if(loaded&&loaded.token===serial){detail=loaded.body;tab=sameProduct&&(['production','inventory','orders'].includes(previousTab)||(previousTab==='activity'&&detail.activity))?previousTab:externalSearch?'production':detail.activity?'activity':'production';store();}if(live)render();
      return loaded?{token:loaded.token,id:Number(id)}:null;
    }
    function image(p,small=false){if(lazyDrawings)return `<div class="pw-lazy-drawing" data-drawing-product="${pid(p)}"></div>`;const d=drawingItems(p)[0];return d?`<a class="pw-drawing ${small?'pw-thumb':''}" href="${esc(d.original_url)}" target="_blank" rel="noopener noreferrer"><img src="${esc(d.preview_url)}" alt="${esc(d.name||'工程图纸')}" loading="lazy"><span>${small?'图纸':'查看原图 / PDF'}</span></a>`:`<span class="pw-muted">${p?.drawings?.status==='forbidden'?'无图纸权限':'未附图纸'}</span>`;}
    function facts(entries){return `<dl class="pw-facts">${entries.filter(([,v])=>v!==null&&v!==undefined&&v!=='').map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`;}
    function fieldMolds(production,product){
      const found=new Map();
      const add=(m,owner)=>{const id=Number(m.mold_id||m.id),key=validId(id)?`id:${id}`:`name:${m.name||m.mold_name||m.mold_code||'unknown'}:${m.short_label||m.location_label||''}`;
        if(!found.has(key))found.set(key,{...m,owners:[]});
        const row=found.get(key);if(owner&&!row.owners.includes(owner))row.owners.push(owner);};
      list(production.molds).forEach(m=>add(m,product.product_code||'当前产品'));
      list(production.bom).forEach(b=>list(b.molds).forEach(m=>add(m,b.product_code||b.product_name||'子件')));
      return [...found.values()];
    }
    function fieldSummary(){
      const p=detail.product||{},v=detail.production||{},molds=fieldMolds(v,p),rows=list(detail.inventory?.items);
      const places=detail.inventory?.visibility==='hidden_by_permission'?null:[...new Set(rows.map(r=>r.location_label||r.location_name).filter(Boolean))];
      return `<div class="pw-field-summary"><div><small>存货编码</small><strong>${esc(p.product_code||'待核')}</strong>${p.customer_material_code?`<small>客户编码 ${esc(p.customer_material_code)}</small>`:''}</div><div><small>模具 · 位置</small>${molds.length?molds.map(m=>`<span class="pw-field-mold"><b>${esc(moldPlace(m))}</b><small>${esc(m.owners.join('、'))} · ${esc(m.name||m.mold_name||m.mold_code||'模具待核')}${m.location_visibility==='hidden_by_permission'?'':m.location_label?` · ${esc(m.location_label)}`:''}</small></span>`).join(''):'<strong>无关联模具</strong>'}</div><div><small>实存货位</small>${places===null?'<strong>无库存查看权限</strong>':places.length?places.map(x=>`<strong>${esc(x)}</strong>`).join(''):'<strong>未查到实存货位</strong>'}</div></div>`;
    }
    function stockOverview(){
      const s=detail.inventory?.summary?.finished,groups=detail.inventory?.source_summary||{};
      if(!s)return '';
      return `<div class="pw-stock-first"><div>现存成品 <button type="button" class="pw-stock-number" data-stock-product="${pid(detail.product)}">${number(s.actual)} ${esc(s.unit)}</button></div><small>可用 ${number(s.available)} · 占用 ${number(s.reserved)}${s.restricted?` · 受限 ${number(s.restricted)}`:''}${s.shared_available?` · 另有共用可用 ${number(s.shared_available)} ${esc(s.unit)}`:''}</small></div><div class="pw-source-pills">${['stocktake','replenishment','order','other'].filter(k=>k!=='other'||groups[k]?.actual).map(k=>`<button type="button" class="so-source-${k}" data-stock-product="${pid(detail.product)}" data-stock-kind="${k}">${esc(groups[k]?.label||({stocktake:'盘点入库',replenishment:'补库生产入库',order:'订单生产入库',other:'其他／来源待核'})[k])} · ${number(groups[k]?.actual)} ${esc(s.unit)}</button>`).join('')}</div>`;
    }
    function moldPlace(m){return m.location_visibility==='hidden_by_permission'?'无位置查看权限':m.short_label||m.location_label||'位置待核';}
    function productionView(){
      const p=detail.product||{},v=detail.production||{},steps=list(v.process_steps),molds=list(v.molds),bom=list(v.bom),cut=v.cutting;
      return `<div class="pw-production"><div><h3>生产资料 <small>当前常用箱</small></h3>${facts([
        ['报料宽 × 长',v.report_width_mm&&v.report_length_mm?`${v.report_width_mm} × ${v.report_length_mm} mm`:v.report_specification||'未设置'],
        ['材质 / 楞型',[v.material_code,v.flute_type,v.layer_count?`${v.layer_count}层`:null].filter(Boolean).join(' / ')],
        ['箱型',v.box_style],['净片尺寸',v.net_specification],['开料方式',v.cutting_summary],
        ['模切开数',cut?.is_die_cut||molds.length?`${number(v.mold_count)} 模`:null],
        ['每张采购纸产出',cut&&Number(cut.yield_per_supplier_sheet)>1?`${number(cut.yield_per_supplier_sheet)} 片`:null],
        ['压线',list(v.crease_values_mm).some(x=>x!==null&&x!==undefined)||steps.some(s=>/开槽|压线/.test(s.label))?list(v.crease_values_mm).map(x=>x??'待核').join(' / ')+' mm':v.crease_text],
        ['印刷',v.print_content],['印刷颜色',Array.isArray(v.printing_colors)?v.printing_colors.join(' / '):v.printing_colors],
        ['结合方式',['无需结合','无','其他'].includes(v.joining_method)?null:v.joining_method]])}
        ${steps.length?`<ol class="pw-process">${steps.map(s=>`<li><b>${esc(s.label||s.name)}</b><span>${esc(s.detail||s.description)}</span></li>`).join('')}</ol>`:''}
        ${list(v.missing_fields).length?`<p class="pw-warning">待补：${esc(v.missing_fields.join('、'))}</p>`:''}
        ${molds.length?`<h3>模具</h3>${molds.map((m,i)=>`<div class="pw-mold"><b>${esc(m.name||m.mold_name||m.mold_code)}</b><span>${esc(moldPlace(m))}</span>${locationUrl(m)?`<button type="button" data-mold="${i}">查看位置</button>`:''}</div>`).join('')}`:''}
        ${list(v.printing_plates).length?`<h3>印版与位置</h3>${list(v.printing_plates).map(p=>`<div class="pw-mold"><b>${esc(p.code)} · ${esc(p.color)}</b><span>${esc(p.name)} · 版本 ${number(p.version)} · ${esc(p.location)}</span></div>`).join('')}`:''}
        ${bom.length?`<h3>每套用料</h3><div class="pw-bom">${bom.map(b=>`<button type="button" data-product="${Number(b.product_id)}"><b>${esc(b.product_code)}</b><span>${esc(b.product_name)}${list(b.molds).map(m=>`<small>${esc(m.name||m.mold_name||m.mold_code||'模具')} · ${esc(moldPlace(m))}</small>`).join('')}</span><strong>${number(b.quantity)} ${esc(b.unit||'片')}</strong></button>`).join('')}</div>`:''}
      </div><aside class="pw-engineering">${image(p)}${(lazyDrawings?[]:drawingItems(p).slice(1)).map(d=>`<a href="${esc(d.original_url)}" target="_blank" rel="noopener noreferrer">${esc(d.name||'更多工程图')}</a>`).join('')}</aside></div>`;
    }
    function inventoryView(){const inventory=detail.inventory;
      if(!inventory||inventory.visibility==='hidden_by_permission')return '<p class="pw-empty">无库存查看权限</p>';
      const summary=inventory.summary||{},rows=list(inventory.items);
      return `<div class="pw-stock-summary">${Object.entries(summary).filter(([,s])=>s&&typeof s==='object').map(([k,s])=>`<div><b>${esc(stockNames[k]||k)}</b><strong>${number(s.actual)} ${esc(s.unit||'')}</strong><span>可用 ${number(s.available)} · 占用 ${number(s.reserved)}</span></div>`).join('')}</div>
        ${rows.length?`<div class="pw-table-scroll"><table><thead><tr><th>存放位置</th><th>库存形态</th><th>实存</th><th>可用 / 占用</th><th>关联订单</th><th></th></tr></thead><tbody>${rows.map((r,i)=>`<tr><td><b>${esc(r.location_label||r.location_name||'位置待核')}</b></td><td>${esc(stockNames[r.inventory_type]||r.inventory_type||'')}</td><td><strong>${number(r.actual)} ${esc(r.unit||'')}</strong></td><td>${number(r.available)} / ${number(r.reserved)}</td><td>${list(r.orders).map(o=>`${esc(o.customer_po||o.order_number)} · ${number(o.quantity)}${esc(o.unit||'')}`).join('<br>')||'—'}</td><td>${locationUrl(r)?`<button type="button" data-location="${i}">地图定位</button>`:'位置待核'}</td></tr>`).join('')}</tbody></table></div>`:'<p class="pw-empty">当前没有实物库存</p>'}`;
    }
    function ordersView(){
      const rows=list(detail.orders?.items);
      if(detail.orders===null||detail.orders?.visibility==='hidden_by_permission')return '<p class="pw-empty">无订单查看权限</p>';
      return `<label class="pw-history"><input type="checkbox" data-history ${includeHistory?'checked':''}>包含历史订单</label><div class="pw-table-scroll"><table><thead><tr><th>客户单号 / 订单</th><th>交期</th><th>订单数量</th><th>成品抵扣</th><th>需生产</th><th>已交 / 待交</th><th>状态</th><th></th></tr></thead><tbody>${rows.map((o,i)=>`<tr><td><b>${esc(o.customer_po||o.order_number)}</b><small>${esc(o.customer_po?o.order_number:'')}</small></td><td>${esc(o.delivery_date||'未定')}</td><td>${number(o.quantity)} ${esc(o.unit||'')}</td><td>${number(o.deducted_quantity)}</td><td>${number(o.production_quantity)}</td><td>${number(o.delivered_quantity)} / ${number(o.remaining_quantity??o.undelivered_quantity)}</td><td>${esc(o.status_label||o.status)}</td><td><button type="button" data-order="${i}">查看订单</button></td></tr>`).join('')}</tbody></table></div>${!rows.length?'<p class="pw-empty">没有关联订单</p>':''}${detail.orders?.has_more?'<p class="pw-muted">更多历史记录请进入订单主链查看</p>':''}`;
    }
    function activityView(){
      const v=detail.activity||{},rows=list(v.items),deliveries=list(v.deliveries),related=list(v.related_products);
      return `<p class="pw-action-reason">按原始单据追溯；实际完工已进入对应库存，仅未完成余料显示理论待产。已报料批次沿用原冻结规格，新报料使用当前冻结资料。</p>
      ${related.length?`<h3>关联组合产品与零件</h3><div class="pw-bom">${related.map(r=>`<button type="button" data-product="${Number(r.product_id)}"><small>${esc(r.relation)}</small><b>${esc(r.code)}</b><span>${esc(r.name)}</span></button>`).join('')}</div>`:''}
      <h3>报料 · 收料 · 生产</h3>${rows.length?`<div class="pw-table-scroll"><table class="pw-activity-table"><thead><tr><th>环节 / 状态</th><th>原始单据 / 时间</th><th>登记数量</th><th>材料实存 / 理论待产</th><th>实际位置</th><th>本批冻结规格</th></tr></thead><tbody>${rows.map(r=>`<tr><td data-label="环节 / 状态"><b>${esc(r.status)}</b><small>${esc(r.source)}</small></td><td data-label="原始单据 / 时间">${esc(r.document)}<small>${esc(r.at?new Date(r.at).toLocaleString('zh-CN'):null)}</small></td><td data-label="登记数量">${number(r.quantity)} ${esc(r.unit)}</td><td data-label="材料实存 / 理论待产">${r.physical_quantity!==null&&r.physical_quantity!==undefined?`${number(r.physical_quantity)} 张`:'—'}${r.theoretical_output!==null&&r.theoretical_output!==undefined?`<strong class="pw-theoretical">理论待产 ${number(r.theoretical_output)} ${esc(r.output_unit)}</strong><small>每张 ${number(r.factor)} 片 / 每成品 ${number(r.pieces_per_box)} 片</small>`:''}</td><td data-label="实际位置">${esc(r.location||'位置未登记')}</td><td data-label="本批冻结规格">${esc(r.frozen_spec||'—')}</td></tr>`).join('')}</tbody></table></div>`:'<p class="pw-empty">本页没有关联报料、收料或生产记录</p>'}
      <h3>待送货与历史送货</h3>${deliveries.length?`<div class="pw-table-scroll"><table class="pw-activity-table"><thead><tr><th>送货单 / 日期</th><th>状态</th><th>客户单号</th><th>当时产品 / 规格</th><th>数量</th></tr></thead><tbody>${deliveries.map(r=>`<tr><td data-label="送货单 / 日期"><b>${esc(r.document)}</b><small>${esc(r.date)}</small></td><td data-label="状态">${esc(r.status)}</td><td data-label="客户单号">${esc(r.customer_po||'—')}</td><td data-label="当时产品 / 规格">${esc(r.code)} ${esc(r.name)}<small>${esc(r.specification)}</small></td><td data-label="登记数量">${number(r.quantity)} ${esc(r.unit)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="pw-empty">本页没有送货记录</p>'}
      ${list(v.hidden_sections).length?`<p class="pw-muted">当前账号无权查看：${esc(v.hidden_sections.join('、'))}</p>`:''}
      <footer class="pw-pager"><button type="button" data-activity-page="${Number(v.page||1)-1}" ${Number(v.page||1)<=1?'disabled':''}>上一页记录</button><span>第 ${number(v.page||1)} 页</span><button type="button" data-activity-page="${Number(v.page||1)+1}" ${v.has_more?'':'disabled'}>下一页记录</button></footer>`;
    }
    function pager(){return `<footer class="pw-pager"><button type="button" data-page="${page-1}" ${page<=1?'disabled':''}>上一页</button><span>第 ${page} 页</span><button type="button" data-page="${page+1}" ${!result.has_more?'disabled':''}>下一页</button></footer>`;}
    function sheetResults(){return `<section class="pw-results pw-sheet-results"><div class="pw-result-heading"><h2>库存片料 <small>${number(result.total)} 批</small></h2><span>按匹配度排序</span></div>${result.items.length?`<div class="pw-table-scroll"><table><thead><tr><th>片料名称</th><th>片料长宽</th><th>匹配度 / 开数</th><th>楞型 / 材质</th><th>数量</th><th>位置</th><th></th></tr></thead><tbody>${result.items.map((r,i)=>`<tr><td data-label="片料名称"><b>${esc(r.name)}</b></td><td data-label="片料长宽">${number(r.length)} × ${number(r.width)} mm</td><td data-label="匹配度 / 开数"><strong class="pw-match-score">${r.match_score==null?'—':number(r.match_score)+'%'}</strong><span>${cutLabel(r.cuts_per_sheet)}</span></td><td data-label="楞型 / 材质">${esc(r.flute_type==='NONE'?'卡纸':r.flute_type+' 瓦')} · ${number(r.layer_count)} 层<span>${esc(r.material_code||'材质未登记')}</span></td><td data-label="数量"><b>可用 ${number(r.available)} ${esc(r.unit)}</b><span>实存 ${number(r.actual)} · 占用 ${number(r.reserved)}</span></td><td data-label="位置">${esc(r.location_label)}</td><td><button class="pw-primary" type="button" data-sheet="${i}">选中查用途</button></td></tr>`).join('')}</tbody></table></div>`:'<p class="pw-empty">没有符合该长宽的库存片料</p>'}${pager()}</section>`;}
    function reverseResults(){return `<section class="pw-results pw-reverse-results"><div class="pw-result-heading"><h2>推荐常用箱 <small>${number(result.total)} 款</small></h2></div>${result.items.length?`<div class="pw-table-scroll"><table><thead><tr><th>存货编码 / 产品</th><th>成品尺寸</th><th>报料材质</th><th>报料尺寸</th><th>匹配度</th><th>图纸</th><th></th></tr></thead><tbody>${result.items.map(p=>`<tr><td data-label="存货编码 / 产品"><b class="pw-code">${esc(p.product_code||p.customer_material_code)}</b><span>${esc(p.product_name)}</span><small>${esc(p.customer_name)}</small></td><td data-label="成品尺寸">${esc(p.specification||'未登记')}</td><td data-label="报料材质">${esc(p.report_material||'未登记')}</td><td data-label="报料尺寸">${p.report_length_mm&&p.report_width_mm?`${number(p.report_length_mm)} × ${number(p.report_width_mm)} mm`:'未登记'}${p.report_component?`<span>${esc(p.report_component)}</span>`:''}</td><td data-label="匹配度"><b class="pw-match-score">${p.match_score!==null&&p.match_score!==undefined?number(p.match_score)+'%':'—'}</b></td><td data-label="图纸">${image(p,true)}</td><td><button type="button" data-product="${pid(p)}">查看资料</button></td></tr>`).join('')}</tbody></table></div>`:'<p class="pw-empty">没有符合条件的常用箱，请调整尺寸、楞型或匹配度</p>'}${pager()}</section>`;}
    function render(){if(!live)return;
      if(!resetDraft)captureDraft();resetDraft=false;disposeDrawings();
      container.classList.add('product-workbench');
      container.classList.toggle('pw-external-search',externalSearch);
      container.innerHTML=`<form class="pw-search" aria-label="统一产品搜索"><div class="pw-search-main"><div class="pw-modes">${[['search','找产品'],['sheets','按尺寸找片料'],['reverse','片料找用途']].map(([k,v])=>`<button type="button" data-mode="${k}" aria-pressed="${mode===k}" class="${mode===k?'active':''}">${v}</button>`).join('')}</div>${mode==='search'?`<input name="q" type="search" maxlength="100" aria-label="产品关键词" placeholder="客户、存货编码、名称、模具号…" value="${esc(draft.q||'')}">`:`<strong class="pw-reverse-title">${mode==='sheets'?'需要的片料尺寸':'实测片料'}</strong>`}<button class="pw-primary" type="submit" ${busy?'disabled':''}>${busy?'查询中…':'搜索'}</button>${result||detail||busy||error?'<button type="button" data-close>返回工作台</button>':''}</div>
      <details class="pw-conditions" ${mode!=='search'?'open':''}><summary>${mode==='reverse'?'尺寸与工艺条件':'按尺寸查找'}</summary><div class="pw-inputs">${mode==='search'?`<label>尺寸类型<select name="dimension_basis">${[['finished','成品尺寸'],['net','净片尺寸'],['report','报料尺寸']].map(([k,v])=>`<option value="${k}" ${draft.dimension_basis===k?'selected':''}>${v}</option>`).join('')}</select></label>`:''}${[['length','长'],['width','宽'],...(mode==='search'?[['height','高']]:[])].map(([k,v])=>`<label>${v}（mm）<input name="${k}" aria-label="${v}毫米" type="number" min="0.01" step="0.01" value="${esc(draft[k]||'')}"></label>`).join('')}${mode!=='search'?`<label>楞型<select name="flute_type"><option value="">${mode==='sheets'?'全部楞型':'请选择楞型'}</option>${Object.keys(fluteLayers).map(x=>`<option value="${x}" ${draft.flute_type===x?'selected':''}>${x==='NONE'?'卡纸':x+' 瓦'}</option>`).join('')}</select></label><output class="pw-auto-layers" aria-live="polite">${fluteLayers[draft.flute_type]?fluteLayers[draft.flute_type]+' 层':''}</output>`:''}${mode!=='search'?`<label>匹配度<select name="min_score">${[[0,'全部匹配度'],[70,'70% 及以上'],[80,'80% 及以上'],[90,'90% 及以上'],[100,'100%']].map(([k,v])=>`<option value="${k}" ${Number(draft.min_score||0)===k?'selected':''}>${v}</option>`).join('')}</select></label>`:''}</div></details></form>
      ${error?`<div class="pw-error" role="alert">${esc(error)} <button type="button" data-retry>重试</button></div>`:''}
      ${busy?'<p class="pw-loading" role="status">正在读取产品资料…</p>':''}
      ${detail?`<section class="pw-detail"><header><div><button type="button" data-back>← 搜索结果</button><span class="pw-customer">${esc(detail.product.customer_name||detail.product.label)}</span><h2>${esc(detail.product.product_code||detail.product.customer_material_code)} <small>${esc(detail.product.product_name)}</small></h2><span>${esc(detail.product.specification)} · ${esc(detail.product.status_label||'')}</span></div><div class="pw-actions">${detail.actions?.can_requisition||detail.actions?.can_request?`<button type="button" class="pw-primary" data-requisition ${actionBusy?'disabled':''}>${detail.actions.can_requisition?'报料':'申请报料'}</button>`:''}<button type="button" data-tab="inventory">库存与位置</button></div></header>${externalSearch?fieldSummary():''}<nav class="pw-tabs">${[...(detail.activity?[['activity','全程状态']]:[]),['production','生产资料'],['inventory','库存与位置'],['orders','订单与交付']].map(([k,v])=>`<button type="button" data-tab="${k}" class="${tab===k?'active':''}">${v}</button>`).join('')}</nav>${detail.actions?.stock_only?'<p class="pw-warning">仅现货交付</p>':''}${tab==='activity'?activityView():tab==='production'?productionView():tab==='inventory'?inventoryView():ordersView()}</section>`:''}
      ${!detail&&result&&mode==='search'?`<section class="pw-results"><div class="pw-result-heading"><h2>搜索结果 <small>${number(result.total)} 款</small></h2><span>包含零库存产品</span></div>${list(result.items).length?`<div class="pw-table-scroll"><table><thead><tr><th>客户 / 存货编码 / 产品</th><th>成品尺寸</th><th>成品实存 / 可用</th><th>图纸</th><th></th></tr></thead><tbody>${result.items.map(p=>`<tr><td><span class="pw-muted">${esc(p.customer_name||p.label)}</span><b class="pw-code">${esc(externalSearch?p.product_code||p.customer_material_code:p.customer_material_code||p.product_code)}</b><span>${esc(p.product_name)}</span>${p.status_label?`<small>${esc(p.status_label)}</small>`:''}</td><td>${esc(p.specification||'未登记')}</td><td>${`<strong>${esc(stockText(p))}</strong>${positionText(p)?`<small>${esc(positionText(p))}</small>`:''}<small>可用 ${p.inventory?.finished?number(p.inventory.finished.available):'无权限'}</small>${p.inventory?.semi_finished?.actual?`<small>材料实存 ${number(p.inventory.semi_finished.actual)} 张 · 查看全程状态</small>`:''}${p.dimension_match?`<small>尺寸差 ${esc(Object.values(p.dimension_match.delta_mm||{}).filter(x=>x!==null).map(x=>`${x}mm`).join(' / ')||'待核')}</small>`:''}`}</td><td>${image(p,true)}</td><td><button type="button" data-product="${pid(p)}">查看资料</button></td></tr>`).join('')}</tbody></table></div>`:'<p class="pw-empty">没有匹配产品，请核对编码或调整尺寸条件</p>'}<footer class="pw-pager"><button type="button" data-page="${page-1}" ${page<=1?'disabled':''}>上一页</button><span>第 ${page} 页</span><button type="button" data-page="${page+1}" ${!result.has_more?'disabled':''}>下一页</button></footer></section>`:''}
      ${!detail&&result&&mode==='sheets'?sheetResults():''}
      ${!detail&&result&&mode==='reverse'?reverseResults():''}
      ${mapRow?`<section class="pw-map" aria-label="产品位置"><header><strong>${esc(mapRow.location_label||mapRow.location_name||'地图位置')}</strong><button type="button" data-close-map>返回产品资料</button></header><iframe src="${esc(locationUrl(mapRow))}" title="产品当前库存位置"></iframe></section>`:''}`;
      if(mode==='search'){const dimensions=document.createElement('button');dimensions.type='button';dimensions.className='pw-dimensions-toggle';dimensions.textContent='尺寸';dimensions.setAttribute('aria-expanded','false');dimensions.addEventListener('click',()=>{const d=container.querySelector('.pw-conditions');d.open=!d.open;dimensions.setAttribute('aria-expanded',String(d.open));});container.querySelector('.pw-search-main .pw-primary').before(dimensions);}
      if(mode==='reverse'&&validId(params.lot_id)){
        const form=container.querySelector('form'),input=document.createElement('input');input.type='hidden';input.name='lot_id';input.value=params.lot_id;form.append(input);
        for(const name of ['length','width'])form.querySelector(`[name="${name}"]`).readOnly=true;
        form.querySelector('[name="flute_type"]').disabled=true;
        const selected=document.createElement('div');selected.className='pw-selected-sheet';selected.innerHTML=`<strong>已选库存片料 ${number(params.length)} × ${number(params.width)} mm · ${esc(params.flute_type||'楞型待核')}</strong><button type="button" data-reselect-sheet>重新选片料</button>`;form.append(selected);
      }
      if(detail?.actions?.action_reason&&!detail.actions.stock_only){const notice=document.createElement('p');notice.className='pw-action-reason';notice.textContent=detail.actions.action_reason;container.querySelector('.pw-detail>header')?.after(notice);}
      if(detail?.product?.drawings?.status==='unavailable_inactive')container.querySelectorAll('.pw-engineering .pw-muted').forEach(e=>e.textContent='停用产品图纸待核');
      if(result&&!detail&&mode==='search')list(result.items).forEach((p,i)=>{
        const row=container.querySelectorAll('.pw-results tbody tr')[i],first=row?.firstElementChild;
        if(first&&p.specification){const size=document.createElement('small');size.className='pw-phone-spec';size.textContent=p.specification;first.append(size);}
        if(first&&p.customer_material_code&&p.customer_material_code!==p.product_code){
          const code=document.createElement('small');code.textContent=`存货编码 ${p.product_code} · 客户编码 ${p.customer_material_code}`;first.append(code);
        }
        if(mode==='search'&&p.inventory?.finished&&row?.children[2]){
          const cell=row.children[2],old=cell.querySelector('strong');
          if(old){const b=document.createElement('button');b.type='button';b.className='pw-stock-number';b.dataset.stockProduct=String(pid(p));b.textContent=stockText(p);old.replaceWith(b);}
          const note=document.createElement('small');note.textContent=`已占用 ${number(p.inventory.finished.reserved)}${p.inventory.finished.restricted?` · 受限 ${number(p.inventory.finished.restricted)}`:''}`;cell.append(note);
          if(p.inventory.finished.shared_available){const shared=document.createElement('small');shared.textContent=`另有共用可用 ${number(p.inventory.finished.shared_available)} ${p.inventory.finished.unit||''}（未计入本客户现存）`;cell.append(shared);}
          const groups=p.stock_sources||{},chips=document.createElement('div');chips.className='pw-source-pills';
          for(const key of ['stocktake','replenishment','order','other']){if(key==='other'&&!groups[key]?.actual)continue;const b=document.createElement('button');b.type='button';b.dataset.stockProduct=String(pid(p));b.dataset.stockKind=key;b.className='so-source-'+key;b.textContent=({stocktake:'盘点',replenishment:'补库生产',order:'订单生产',other:'其他'})[key]+' '+number(groups[key]?.actual);chips.append(b);}cell.append(chips);
        }
        const pieces=Number(p.inventory?.processed_component?.available||0);
        if(mode==='search'&&pieces>0&&row?.children[2]){
          const note=document.createElement('small');note.textContent=`已加工半成品可用 ${number(pieces)} 片（尚非成品）`;
          row.children[2].append(note);
        }
      });
      if(detail&&purpose){const picker=document.createElement('div');picker.className='pw-purpose';picker.innerHTML='<strong>本次报料用途</strong><button type="button" data-purpose="order">对应现有订单</button><button type="button" data-purpose="stock">主动备库</button><button type="button" data-purpose="cancel">取消</button>';container.querySelector('.pw-detail>header').after(picker);}

      if(detail&&tab==='inventory')list(detail.inventory?.items).forEach((r,i)=>{if(!r.reverse_source)return;const cell=container.querySelectorAll('.pw-detail tbody tr')[i]?.lastElementChild;if(cell){const b=document.createElement('button');b.type='button';b.dataset.reverse=String(i);b.textContent='查用途';cell.append(b);}});
      if(detail&&tab==='inventory')list(detail.inventory?.items).forEach((r,i)=>{if(!r.shared_confirmed)return;const cell=container.querySelectorAll('.pw-detail tbody tr')[i]?.firstElementChild;if(cell){const note=document.createElement('small');note.textContent='已授权共用（不计入本客户自有成品）';cell.append(note);}});
      if(detail){const block=document.createElement('div');block.innerHTML=stockOverview();container.querySelector('.pw-detail>header')?.after(block);}
      if(lazyDrawings){const products=detail?[detail.product]:list(result?.items);container.querySelectorAll('[data-drawing-product]').forEach(host=>{const p=products.find(p=>pid(p)===Number(host.dataset.drawingProduct));if(!p)return;if(p.drawings?.status==='unavailable_inactive')host.textContent='停用产品图纸待核';else if(global.TmProductDrawings)global.TmProductDrawings.append(host,p,{label:'工程图纸'});else host.textContent='图纸组件未加载，请刷新页面';});}
      bind();
    }
    function bind(){
      container.querySelector('form').addEventListener('input',()=>{captureDraft();store();});
      container.querySelector('form').addEventListener('change',()=>{captureDraft();store();});
      const all=(s,f)=>container.querySelectorAll(s).forEach(e=>e.addEventListener('click',()=>f(e)));
      const submit=form=>{stop();lastProductId=null;try{captureDraft();const next=criteria(form,mode);if(mode==='reverse'&&validId(next.lot_id)){next.flute_type=params.flute_type;if(params.processed_state)next.processed_state=params.processed_state;}params=next;search(1);}catch(ex){retryForm=true;error=ex.message;render();}};
      container.querySelector('form').addEventListener('submit',e=>{e.preventDefault();submit(e.target);});
      container.querySelector('[name="flute_type"]')?.addEventListener('change',e=>{const layer=fluteLayers[e.target.value];container.querySelector('.pw-auto-layers').textContent=layer?layer+' 层':'';});
      container.querySelector('[name="min_score"]')?.addEventListener('change',()=>submit(container.querySelector('form')));
      all('[data-sheet]',e=>{const row=result.items[Number(e.dataset.sheet)];sheetSearch={params:{...params},page};mode='reverse';params={...row.reverse_source};draft={...params};resetDraft=true;search(1);});
      all('[data-reselect-sheet]',()=>{const previous=sheetSearch||{params:{length:params.length,width:params.width},page:1};mode='sheets';params={...previous.params};draft={...params};resetDraft=true;search(previous.page);});
      all('[data-mode]',e=>{stop();mode=e.dataset.mode;sheetSearch=null;params={};draft={};resetDraft=true;result=detail=null;lastProductId=null;retryForm=false;mapRow=null;error='';onOpen(false);store();render();});
      all('[data-close]',()=>{stop();sheetSearch=null;result=detail=null;lastProductId=null;retryForm=false;error='';mapRow=null;params={};draft={};resetDraft=true;onOpen(false);store();render();});
      all('[data-retry]',()=>retryForm?submit(container.querySelector('form')):lastProductId?show(lastProductId):search(page));
      all('[data-product]',e=>show(e.dataset.product));all('[data-page]',e=>search(Number(e.dataset.page)));
      all('[data-stock-product]',e=>{const id=Number(e.dataset.stockProduct),p=detail?.product||list(result?.items).find(p=>pid(p)===id);if(global.ERPStockOrigins){stockDialog=global.ERPStockOrigins.open({productId:id,sourceKind:e.dataset.stockKind||'all',label:[p?.customer_name,p?.product_code,p?.product_name].filter(Boolean).join(' · '),request,isCurrent:()=>live});}else show(id);});
      all('[data-back]',()=>{stop();detail=null;lastProductId=null;mapRow=null;store();if(result)render();else if(Object.keys(params).length)search(page);else{onOpen(false);render();}});
      all('[data-tab]',e=>{tab=e.dataset.tab;store();render();});
      all('[data-activity-page]',e=>show(pid(detail.product),Number(e.dataset.activityPage)));
      all('[data-location]',e=>{mapRow=detail.inventory.items[Number(e.dataset.location)];render();});
      all('[data-mold]',e=>{mapRow=detail.production.molds[Number(e.dataset.mold)];render();});
      all('[data-close-map]',()=>{mapRow=null;render();});
      all('[data-order]',e=>act('order',detail.orders.items[Number(e.dataset.order)]));
      all('[data-requisition]',()=>{purpose=!purpose;render();});
      all('[data-purpose]',e=>{purpose=false;if(e.dataset.purpose==='stock')act('requisition',detail);else if(e.dataset.purpose==='order'){tab='orders';store();render();}else render();});
      all('[data-reverse]',e=>{sheetSearch=null;const source=detail.inventory.items[Number(e.dataset.reverse)].reverse_source;mode='reverse';params={...source};draft={...source};resetDraft=true;search(1);});
      const history=container.querySelector('[data-history]');
      if(history)history.addEventListener('change',async()=>{includeHistory=history.checked;const id=pid(detail.product);const loaded=await show(id);if(current(loaded)){tab='orders';store();render();}});
      container.querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{const retry=document.createElement('button');retry.type='button';retry.className='pw-warning';retry.textContent='图纸加载失败 · 重试';retry.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();render();});img.replaceWith(retry);}));
    }
    async function act(kind,data){
      if(actionBusy||!onAction)return;
      let cancelled=false,rejectWait,timer;const listeners=[];
      const wait=new Promise((_,reject)=>{rejectWait=reject;});wait.catch(()=>{});
      const origin={handedOff:false,current:()=>live&&action===origin&&!cancelled,
        handoff(){origin.handedOff=true;clearTimeout(timer);},onCancel(fn){listeners.push(fn);},
        cancel(e=actionError('操作已取消')){if(cancelled)return;cancelled=true;clearTimeout(timer);listeners.forEach(fn=>fn(e));rejectWait(e);}};
      action=origin;actionBusy=true;store();render();
      timer=setTimeout(()=>origin.cancel(actionError('读取超时，请重试','TimeoutError')),15000);
      try{await Promise.race([onAction(kind,data,origin),wait]);}
      catch(e){if(live&&action===origin&&(e.name!=='AbortError'))error=e.message||'无法打开，请重试';}
      finally{clearTimeout(timer);if(action===origin){action=null;actionBusy=false;if(live)render();}}
    }
    render();if(savedId)show(savedId).then(loaded=>{if(current(loaded)){tab=initialState.tab||'production';store();render();}});else if(Object.keys(params).length)search(page);
    return {search,show,query(nextParams,nextPage=1,nextMode='search'){mode=['reverse','sheets'].includes(nextMode)?nextMode:'search';params={...nextParams};draft={...nextParams};resetDraft=true;return search(nextPage);},destroy(){stockDialog?.close();live=false;stop(true);disposeDrawings();container.replaceChildren();onOpen(false);},snapshot:()=>({mode,sheetSearch,params:{...params},draft:{...draft},page,tab,productId:detail?pid(detail.product):null})};
  }
  async function read(url,{signal}={}){
    const response=await fetch(url,{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'},signal});
    let body;
    try{body=await response.json();}
    catch(error){
      if(error.name==='AbortError')throw error;
      if(response.ok)throw Error('产品资料响应格式异常，请重试');
    }
    if(!response.ok)throw Error(typeof body?.detail==='string'?body.detail:
      response.status===401?'登录已失效，请重新登录':
      response.status===403?'无权查看该产品资料':`产品资料读取失败（HTTP ${response.status}），请重试`);
    return body;
  }
  let mobile=null;
  function destroyMobile(){mobile?.instance.destroy();mobile=null;}
  function mountMobile({container,userId,onOrder,externalSearch=container?.dataset?.externalSearch==='1'}){
    if(mobile?.userId===userId&&mobile.container===container)return mobile.instance;
    destroyMobile();
    if(!container||!validId(userId))return;
    const instance=mount({container,request:read,lazyDrawings:true,externalSearch,onAction:async(kind,data,origin)=>{
      const productId=kind==='order'?data.product_id:pid(data.product);
      if(kind==='order'&&onOrder){await onOrder(data,origin.current);return;}
      if(kind==='requisition'&&data.actions?.can_request&&!data.actions?.can_requisition&&validId(data.actions.policy_id)){
        global.location.href=`/static/business-approvals.html?customer=${Number(data.product.customer_id)}&policy=${Number(data.actions.policy_id)}`;return;
      }
      if(!validId(productId))throw Error('请在订单主链查看对应订单');
      global.location.href='/?page=dashboard&workbench_product='+Number(productId);
    }});
    mobile={userId,instance,container};return instance;
  }
  function install(app){
    app.mixin({
      data(){return this.$parent?{}:{productWorkbenchOpen:false,productWorkbenchState:null,productWorkbenchSequence:0};},
      watch:{authGeneration(){if(!this.$parent){this.productWorkbenchState=null;this.productWorkbenchOpen=false;}}},
      methods:{async openProductWorkbench(id,tab='inventory'){
        this.productWorkbenchState={mode:'search',params:{},page:1,tab,productId:Number(id)};this.productWorkbenchSequence++;await this.go('dashboard');
      },async productWorkbenchAction(kind,data,origin){
        const context=actionContext(this,origin,kind);
        const p=kind==='order'?data:data.product;
        try{
          if(kind==='order'){
            this.homeEntry={target:'orders',product_code:this.productWorkbenchState?.productId?'产品资料':'',customer_label:''};
            await context.wait(this.go('orders',{productAction:context}));if(!context.at('orders'))return;
            await context.wait(this.openOrderDetail({id:Number(data.order_id)},{productAction:context}));return;
          }
          if(!p||data.actions?.stock_only)throw Error('此产品仅供现货交付');
          this.homeEntry={target:'requisition',customer_label:p.customer_name,product_code:p.product_code};
          if(!data.actions?.can_requisition){
            if(data.actions?.can_request&&validId(data.actions.policy_id))return this.openBusinessRequests({customer_id:p.customer_id,policy_id:data.actions.policy_id});
            throw Error('请由管理员补充报料资料或设置库存预警');
          }
          await context.wait(this.go('requisition',{productAction:context}));if(!context.at('requisition'))return;
          await context.wait(this.openStockReplenishment({warningOnly:!!data.actions.policy_id,productAction:context}));if(!context.formReady())return;
          if(data.actions.policy_id){await context.wait(this.addStockPolicyDraft({id:data.actions.policy_id},{productAction:context}));return;}
          await context.wait(this.loadStockProducts(p.customer_id,p.customer_material_code||p.product_code,{productAction:context}));if(!context.current())return;
          if(!this.stockReplenishmentProducts.some(r=>Number(r.id)===pid(p)))throw Error('此产品当前不能生成报料草稿，请核对常用箱');
          this.addBlankStockReplenishmentLine();const line=this.stockReplenishmentForm.items.at(-1);
          line.customer_id=p.customer_id;line.reference_product_id=pid(p);this.applyStockProduct(line);
        }catch(e){if(e.name!=='AbortError'&&context.current()){
          if(!origin||origin.handedOff){if(!e._productActionNotified)this.showToast((e.message||'读取失败')+(this.modal?'，请关闭窗口后从产品重新打开':'，请返回产品后重新打开'),true);}
          else throw e;
        }}
        finally{context.finish();}
      }}
    });
    app.component('product-workbench',{
      props:['vm'],template:'<div ref="workbench"></div>',
      mounted(){this.startProductWorkbench();},beforeUnmount(){this._productSearch?.destroy();},
      watch:{'vm.authGeneration'(){this._productSearch?.destroy();this.vm.productWorkbenchState=null;this.startProductWorkbench();},
        'vm.user.id'(){this._productSearch?.destroy();this.vm.productWorkbenchState=null;this.startProductWorkbench();},
        'vm.productWorkbenchSequence'(){this._productSearch?.destroy();this.startProductWorkbench();}},
      methods:{startProductWorkbench(){
        if(!this.vm.user?.id)return;
        const query=new URLSearchParams(global.location.search),initial=this.vm.productWorkbenchState||{};
        if(!initial.productId&&validId(query.get('workbench_product')))initial.productId=Number(query.get('workbench_product'));
        this._productSearch=mount({container:this.$refs.workbench,request:read,initialState:initial,
          onOpen:value=>{this.vm.productWorkbenchOpen=value;},onState:value=>{this.vm.productWorkbenchState=value;},onAction:(kind,data,origin)=>this.vm.productWorkbenchAction(kind,data,origin)});
      }}
    });
    const original=app._component.methods.openLowStockLocations;
    app._component.methods.openLowStockLocations=function(row){if(validId(row?.product_id))return this.openProductWorkbench(row.product_id,'inventory');return original.call(this,row);};
  }
  const snapshotMobile=()=>mobile?.instance.snapshot()||null;
  const exported={mount,install,navigationIntent,read,mountMobile,destroyMobile,snapshotMobile,criteria,locationUrl,drawingItems,esc};
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
  global.ERPProductWorkbench=exported;
})(typeof window==='undefined'?globalThis:window);
