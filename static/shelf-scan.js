const $ = id => document.getElementById(id), params = new URLSearchParams(location.search);
const shortPath = location.pathname.match(/^\/q\/([1-9]\d*)(?:\/([a-f0-9]{24}))?$/);
let id = shortPath ? shortPath[1] : params.get('location_id'), product = shortPath ? shortPath[2] : params.get('product');
const h = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const unit = v => ({pcs:'只',pc:'只',piece:'片',pieces:'片',set:'套',sets:'套',sheet:'张',sheets:'张',box:'箱',boxes:'箱',carton:'箱',cartons:'箱',bundle:'捆',bundles:'捆',kg:'千克',roll:'卷',rolls:'卷',pallet:'托',pallets:'托'}[String(v||'').trim().toLowerCase()] || v || '');
function productCard(item) {
  return `<article><div class="product-grid"><div class="product-main"><small>存货编码</small><div class="code">${h(item.code)}</div><div class="product-name">${h(item.name)}</div><div class="spec">${h(item.specification)}</div></div><aside class="product-side"><small>客户</small><b class="customer">${h(item.customer)}</b>${item.customer_name&&item.customer_name!==item.customer?`<small class="customer-full">${h(item.customer_name)}</small>`:''}<small class="quantity-label">实时数量</small><div class="quantity">${h(item.quantity)} <span>${h(unit(item.unit))}</span></div></aside></div><div class="balances"><span>可用 <b>${h(item.available)}</b></span><span>预占 <b>${h(item.reserved)}</b></span>${item.damaged?'<span>异常 <b>'+h(item.damaged)+'</b></span>':''}</div>${item.lots.map(lot=>`<details data-lot="${lot.id}"><summary>批次 · ${h(lot.quantity)} ${h(unit(item.unit))}${lot.status==='frozen'?' · 已冻结':''} · 订单</summary><div class="orders"></div></details>`).join('')}</article>`;
}
function bomRelations(rows) {
  if (!(rows || []).length) return '';
  return '<details><summary>BOM 配套关系</summary><small>当前常用箱关系；位置不代表本批已预占。</small>'+rows.map(row=>
    '<p><b>'+h(row.direction==='parent'?'父件':'子件')+' '+h(row.product_code)+'</b> · '+h(row.product_name)+' · 每套 '+h(row.quantity_per_set)+' · '+h(row.relation==='accompany'?'随货配套':'组装消耗')+'</p>'+
    ((row.locations||[]).map(loc=>'<p>'+h(loc.location_name)+' · '+h(loc.quantity)+' '+h(unit(loc.unit))+(loc.status==='frozen'?' · 已冻结':'')+'</p>'+
      (loc.reservations||[]).map(r=>'<small>'+TMOrderReference.html(r)+' · 已占 '+h(r.quantity)+' '+h(unit(loc.unit))+'</small>').join('')).join('')||'<small>暂无可见在库位置</small>')
  ).join('')+'</details>';
}
let generation = 0;
async function request(url, options={}) {
  const r = await fetch(url, {credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(12000),...options});
  const data = await r.json();
  if (!r.ok) { const e = Error(typeof data.detail==='string'?data.detail:'读取失败'); e.status=r.status; throw e; }
  return data;
}
async function load() {
  const current=++generation; $('refresh').disabled=true; $('stocktake').hidden=true; $('message').textContent='读取当前库存…'; $('items').innerHTML=''; $('updated').textContent='';
  try {
    if (!/^[1-9]\d*$/.test(id||'') || (product && !/^[a-f0-9]{24}$/.test(product))) throw Error('二维码无效');
    const data=await request(`/api/warehouse/locations/${id}/scan${product?'?product='+product:''}`);
    if(current!==generation)return;
    $('login').hidden=true; $('address').textContent=data.address;
    const returnParams=new URLSearchParams({location_id:id,return_scan:'1'});if(product)returnParams.set('return_product',product);
    $('stocktake').href=`/mobile/stocktake.html?${returnParams}`;
    $('stocktake').hidden=false;
    $('message').textContent=data.items.length?'':product?'本格已无此产品或无查看权限':'本格暂无可见库存';
    $('updated').textContent='读取于 '+new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(data.refreshed_at));
    $('items').innerHTML=data.items.map(productCard).join('');
    for(const node of document.querySelectorAll('.quantity')) {
      let size=parseFloat(getComputedStyle(node).fontSize);
      while(node.scrollWidth>node.clientWidth+1&&size>12){size-=.5;node.style.fontSize=size+'px';}
    }
    for(const node of document.querySelectorAll('details[data-lot]'))node.addEventListener('toggle',()=>{if(node.open&&!node.dataset.loaded)loadOrders(node,current)});
  } catch(e) {if(current!==generation)return; $('message').textContent=e.status===401?'登录后查看当前货位':e.message; if(e.status===401)$('login').hidden=false;}
  finally {if(current===generation)$('refresh').disabled=false;}
}
async function loadOrders(node,current){
  if(node.dataset.busy)return;node.dataset.busy='1';const box=node.querySelector('.orders');box.textContent='读取订单…';
  try{
    const data=await request('/api/warehouse/lots/'+node.dataset.lot);
    if(current!==generation)return;
    if(Number(data.location?.id)!==Number(id)){box.textContent='批次位置已变更，请刷新';return;}
    const orders=(data.reservations||[]).filter(x=>x.remaining_reserved_stock_quantity>0);
    const source=data.shelf_related_inventory?.source_order;
    box.innerHTML=(source?'<p>来源订单：'+TMOrderReference.html(source)+'</p>':'')+orders.map(x=>'<p>'+TMOrderReference.html(x)+' · 预占 '+h(x.remaining_reserved_stock_quantity)+'</p>').join('');
    if(!box.innerHTML)box.textContent='暂无可见关联订单';
    box.insertAdjacentHTML('beforeend',bomRelations(data.shelf_related_inventory?.bom_relations));
    node.dataset.loaded='1';
  }catch(e){box.textContent=e.status===401?'登录已失效，请刷新登录':e.message;const retry=document.createElement('button');retry.textContent='重试';retry.onclick=()=>loadOrders(node,current);box.append(retry);}
  finally{delete node.dataset.busy;}
}
$('refresh').onclick=load;
$('login').onsubmit=async e=>{e.preventDefault();$('loginButton').disabled=true;try{await request('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('username').value.trim(),password:$('password').value,remember_me:$('remember').checked})});$('password').value='';await load();}catch(e){$('message').textContent=e.message;}finally{$('loginButton').disabled=false;}};
// Browser back from a stocktake may restore this page from bfcache.
// Refresh once on that actual navigation, never on a timer or window focus.
window.addEventListener('pageshow',event=>{if(event.persisted)load();});
if (location.pathname !== '/mobile/scan') load();
