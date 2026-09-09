const $ = id => document.getElementById(id), params = new URLSearchParams(location.search);
const shortPath = location.pathname.match(/^\/q\/([1-9]\d*)(?:\/([a-f0-9]{24}))?$/);
const id = shortPath ? shortPath[1] : params.get('location_id'), product = shortPath ? shortPath[2] : params.get('product');
const h = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const unit = v => ({pcs:'只',piece:'片',pieces:'片',set:'套',sets:'套',sheet:'张',sheets:'张'}[v] || v);
let generation = 0;
async function request(url, options={}) {
  const r = await fetch(url, {credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(12000),...options});
  const data = await r.json();
  if (!r.ok) { const e = Error(typeof data.detail==='string'?data.detail:'读取失败'); e.status=r.status; throw e; }
  return data;
}
async function load() {
  const current=++generation; $('refresh').disabled=true; $('message').textContent='读取实时库存…'; $('items').innerHTML=''; $('updated').textContent='';
  try {
    if (!/^[1-9]\d*$/.test(id||'') || (product && !/^[a-f0-9]{24}$/.test(product))) throw Error('二维码无效');
    const data=await request(`/api/warehouse/locations/${id}/scan${product?'?product='+product:''}`);
    if(current!==generation)return;
    $('login').hidden=true; $('address').textContent=data.address;
    $('message').textContent=data.items.length?'':product?'本格已无此产品或无查看权限':'本格暂无可见库存';
    $('updated').textContent='更新于 '+new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(data.refreshed_at));
    $('items').innerHTML=data.items.map(item=>`<article><b>${h(item.customer)}</b><div class="code">${h(item.code)}</div><div>${h(item.name)} · ${h(item.specification)}</div><div class="quantity">${h(item.quantity)} ${h(unit(item.unit))}</div><small>可用 ${h(item.available)} · 预占 ${h(item.reserved)}${item.damaged?' · 异常 '+h(item.damaged):''}</small>${item.lots.map(lot=>`<details data-lot="${lot.id}"><summary>批次 · ${h(lot.quantity)} ${h(unit(item.unit))}${lot.status==='frozen'?' · 已冻结':''} · 订单</summary><div class="orders"></div></details>`).join('')}</article>`).join('');
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
    const source=data.shelf_related_inventory?.source_order?.order_number;
    box.innerHTML=(source?'<p>来源订单：'+h(source)+'</p>':'')+orders.map(x=>'<p>'+h(x.order_number||'关联订单')+' · 预占 '+h(x.remaining_reserved_stock_quantity)+'</p>').join('');
    if(!box.innerHTML)box.textContent='暂无可见关联订单';
    node.dataset.loaded='1';
  }catch(e){box.textContent=e.status===401?'登录已失效，请刷新登录':e.message;const retry=document.createElement('button');retry.textContent='重试';retry.onclick=()=>loadOrders(node,current);box.append(retry);}
  finally{delete node.dataset.busy;}
}
$('refresh').onclick=load;
$('login').onsubmit=async e=>{e.preventDefault();$('loginButton').disabled=true;try{await request('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('username').value.trim(),password:$('password').value})});$('password').value='';await load();}catch(e){$('message').textContent=e.message;}finally{$('loginButton').disabled=false;}};
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&$('login').hidden)load();});
setInterval(()=>{if(!document.hidden&&$('login').hidden&&!document.querySelector('details[open]'))load();},30000);
load();
