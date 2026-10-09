(function(global){
 'use strict';
 const PREFIX='erp.pdf-save.v2:',LEGACY='erp.pdf-save.v1:';
 const positive=v=>Number.isSafeInteger(v)&&v>0;
 const copy=v=>JSON.parse(JSON.stringify(v));
 const fail=message=>{throw Error(message)};
 const TOKEN_FIELDS=new Set(['expected_actor_id','mold_repair_confirmation_token','preview_safety_token','temp_drawing_token']);
 const PRICE_FIELDS=new Set(['unit_price','requested_unit_price','sale_unit_price','sale_unit_price_no_tax','cost_unit_price']);
 function projection(value,pricing,a01,path=[]){
  if(Array.isArray(value))return value.map(v=>projection(v,pricing,a01,path));
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).filter(([key])=>!TOKEN_FIELDS.has(key)&&(pricing||!PRICE_FIELDS.has(key))&&(a01||key!=='product_expected_version'||path.length!==1||path[0]!=='items')).map(([key,v])=>[key,projection(v,pricing,a01,[...path,key])]));
  return value;
 }
 function decimal(v){
  if(typeof v!=='number'&&typeof v!=='string')return null;
  if(typeof v==='number'&&!Number.isFinite(v))return null;
  const m=String(v).match(/^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);if(!m)return null;
  const exponent=Number(m[4]||0);if(Math.abs(exponent)>100)return null;
  let digits=m[2]+(m[3]||''),point=m[2].length+exponent;
  if(point<=0){digits='0'.repeat(1-point)+digits;point=1}if(point>=digits.length)digits+='0'.repeat(point-digits.length);
  let whole=digits.slice(0,point).replace(/^0+(?=\d)/,''),fraction=digits.slice(point).replace(/0+$/,'');
  return (m[1]==='-'&&(whole!=='0'||fraction)?'-':'')+whole+(fraction?'.'+fraction:'');
 }
 function equal(expected,actual,key=''){
  if(expected===actual)return true;
  if(typeof expected==='number'||PRICE_FIELDS.has(key)||key==='quantity'){const a=decimal(expected),b=decimal(actual);if(a!==null&&b!==null)return a===b}
  if(Array.isArray(expected))return Array.isArray(actual)&&expected.length===actual.length&&expected.every((v,i)=>equal(v,actual[i],key));
  if(expected&&actual&&typeof expected==='object'&&typeof actual==='object'&&!Array.isArray(actual)){const a=Object.keys(expected).sort(),b=Object.keys(actual).sort();return a.length===b.length&&a.every((k,i)=>k===b[i]&&equal(expected[k],actual[k],k))}
  return false;
 }
 function priceMatches(requested,actual){
  const a=decimal(requested),b=decimal(actual);if(a===null||b===null)return false;
  const actualPlaces=(b.split('.')[1]||'').length;if(actualPlaces>6)return false;
  const places=Math.max(7,(a.split('.')[1]||'').length,actualPlaces);
  const integer=v=>{const sign=v.startsWith('-')?-1n:1n,[whole,fraction='']=v.replace(/^-/,'').split('.');return sign*BigInt(whole+fraction.padEnd(places,'0'))};
  const delta=integer(a)-integer(b),magnitude=delta<0n?-delta:delta;
  return magnitude<=5n*10n**BigInt(places-7);
 }
 function linePriceMatches(item,line){
  const hasMeta=['requested_unit_price','unit_price_scale','price_normalized'].some(k=>Object.hasOwn(line,k));
  if(!hasMeta)return equal(item.unit_price,line.unit_price,'unit_price')&&priceMatches(item.unit_price,line.unit_price);
  return line.unit_price_scale===6&&typeof line.price_normalized==='boolean'&&equal(item.unit_price,line.requested_unit_price,'unit_price')&&priceMatches(line.requested_unit_price,line.unit_price)&&line.price_normalized===!equal(line.requested_unit_price,line.unit_price,'unit_price');
 }
 function validateComplete(data,record){
  const bad=()=>fail('保存证明不完整或与原请求不一致，请查询原结果；原内容已保留。');
  if(!data||data.current_actor_id!==record.actorId)bad();
  const proof=data.save_receipt;if(!proof||proof.schema!=='order-import-save-v1'||proof.proof_status!=='complete'||proof.request_match!==true||typeof proof.pricing_visible!=='boolean'||typeof proof.source_replay!=='boolean'||proof.actor_id!==record.actorId||proof.request_key!==record.key||!/^[a-f0-9]{64}$/.test(proof.payload_digest||''))bad();
  const body=JSON.parse(record.bodyJson),request=projection(body,proof.pricing_visible,body.readback_contract==='a01-v1');
  if(new Set(body.items.map(i=>i.client_line_id)).size!==body.items.length)bad();
  if(!equal(request,proof.request))bad();
  const order=proof.order,rows=proof.lines,po=(body.customer_po||'').trim()||null;if(!order||!positive(order.id)||typeof order.order_number!=='string'||!order.order_number.trim()||order.customer_id!==body.customer_id||order.customer_po!==po||!Array.isArray(order.items)||!Array.isArray(rows)||proof.line_count!==body.items.length||rows.length!==body.items.length||order.items.length!==rows.length)bad();
  const actualOrder=data.order||data;if(actualOrder.id!==order.id)bad();
  const byRequest=new Map(),byItem=new Map(),originalIds=new Set();
  for(const line of rows){
   if(!line||typeof line.request_client_line_id!=='string'||!line.request_client_line_id||byRequest.has(line.request_client_line_id)||typeof line.original_client_line_id!=='string'||!line.original_client_line_id||(!proof.source_replay&&line.original_client_line_id!==line.request_client_line_id)||originalIds.has(line.original_client_line_id)||!positive(line.order_item_id)||byItem.has(line.order_item_id)||!positive(line.product_id)||typeof line.is_new_product!=='boolean')bad();
   byRequest.set(line.request_client_line_id,line);byItem.set(line.order_item_id,line);originalIds.add(line.original_client_line_id);
  }
  for(const item of body.items){const line=byRequest.get(item.client_line_id);if(!line||line.request_product_id!==(item.product_id??null)||line.is_new_product!==!!item.is_new_product||(!item.is_new_product&&line.product_id!==item.product_id)||!equal(item.quantity,line.quantity,'quantity')||(proof.pricing_visible&&!linePriceMatches(item,line)))bad()}
  const seenItems=new Set(),sequences=new Set();for(const item of order.items){const line=byItem.get(item.id);if(!line||seenItems.has(item.id)||!positive(item.item_sequence)||item.item_sequence>rows.length||sequences.has(item.item_sequence)||item.client_line_id!==line.original_client_line_id||item.product_id!==line.product_id||!equal(item.quantity,line.quantity,'quantity')||(proof.pricing_visible&&!equal(item.unit_price,line.unit_price,'unit_price'))||(!proof.pricing_visible&&(Object.hasOwn(item,'unit_price')||Object.hasOwn(line,'unit_price')||Object.hasOwn(line,'requested_unit_price'))))bad();seenItems.add(item.id);sequences.add(item.item_sequence)}
  if(proof.source!==null&&(!proof.source||!positive(proof.source.id)||typeof proof.source.kind!=='string'||!proof.source.kind||typeof proof.source.hash!=='string'||!proof.source.hash||typeof proof.source.name!=='string'))bad();
  if(body.import_draft===true&&(!proof.source||proof.source.hash!==record.fileHash))bad();
  return {id:order.id,orderNumber:order.order_number,priceNormalized:rows.some(r=>r.price_normalized===true),proof};
 }
 function validateTrace(data,record){
  const bad=()=>fail('历史查询证明不完整，请再次查询；旧记录保留。');
  const order=data?.order;if(data?.current_actor_id!==record.actorId||data.request_key!==record.key||!order||!positive(order.id)||!positive(order.customer_id)||typeof order.order_number!=='string'||!order.order_number.trim()||!['legacy','restricted'].includes(data.proof_status))bad();
  if(data.status==='source_located'){
   if(data.save_receipt!==null||!data.source||!positive(data.source.id)||data.source.hash!==record.fileHash||typeof data.source.kind!=='string'||!data.source.kind||typeof data.source.name!=='string')bad();
  }else if(data.status==='completed'){
   const p=data.save_receipt;if(!p||p.schema!=='order-import-save-v1'||p.actor_id!==record.actorId||p.request_key!==record.key||p.proof_status!==data.proof_status||p.request_match!==false||p.request!==null||typeof p.pricing_visible!=='boolean'||!/^[a-f0-9]{64}$/.test(p.payload_digest||'')||p.order?.id!==order.id||!Array.isArray(p.order?.items)||!Array.isArray(p.lines)||!Number.isSafeInteger(p.line_count)||p.line_count<0||p.line_count!==p.order.items.length||(p.lines.length!==0&&p.line_count!==p.lines.length))bad();
   if(p.source!==null&&(!p.source||p.source.hash!==record.fileHash||!positive(p.source.id)))bad();
  }else bad();
  return {id:order.id,orderNumber:order.order_number};
 }
 function createStore(storage,actor){
  if(!positive(actor))fail('请重新登录后查询原保存结果。');
  const prefix=PREFIX+actor+':',legacyPrefix=LEGACY+actor+':';
  function read(key){
   const raw=storage.getItem(key);if(raw===null)return null;
   let r;try{r=JSON.parse(raw)}catch(_){fail('原保存记录无法读取，请保留此浏览器并联系管理员核对。')}
   if(!r||r.schema!==2||r.actorId!==actor||typeof r.key!=='string'||!r.key||key!==prefix+encodeURIComponent(r.key)||typeof r.fileHash!=='string'||!r.fileHash||typeof r.bodyJson!=='string'||!['unknown','confirmed'].includes(r.phase))fail('原保存记录不完整，请保留此浏览器并联系管理员核对。');
   let body;try{body=JSON.parse(r.bodyJson)}catch(_){fail('原请求内容无法读取，请保留记录并联系管理员核对。')}
   if(!body||body.idempotency_key!==r.key||!Array.isArray(body.items)||!body.items.length)fail('原请求内容不完整，请联系管理员核对，不能重新生成请求。');
   return {...r,storageKey:key};
  }
  function all(){
   const keys=[],old=[];for(let i=0;i<storage.length;i++){const k=storage.key(i);if(k?.startsWith(prefix))keys.push(k);else if(k?.startsWith(legacyPrefix))old.push(k)}
   const rows=keys.map(read).filter(Boolean);
   for(const key of old){const requestKey=storage.getItem(key),fileHash=key.slice(legacyPrefix.length);if(requestKey&&!rows.some(r=>r.key===requestKey&&r.fileHash===fileHash))rows.push({schema:1,actorId:actor,key:requestKey,fileHash,bodyJson:null,phase:'unknown',storageKey:key,legacy:true});}
   return rows;
  }
  function find(fileHash){
   return all().filter(r=>r.fileHash===fileHash);
  }
  function prepare(fileHash,bodyJson,summary){
   if(typeof fileHash!=='string'||!fileHash||typeof bodyJson!=='string')fail('文件身份或原请求缺失，请重新核对。');
   const body=JSON.parse(bodyJson);if(typeof body.idempotency_key!=='string'||!body.idempotency_key||!positive(body.customer_id)||!Array.isArray(body.items)||!body.items.length||body.expected_actor_id!==actor)fail('原保存请求或账号不完整，不能发送。');
   const rows=find(fileHash);if(rows.length)fail('此文件已有待核对保存，请先查询原结果，不能创建新请求。');
   const key=prefix+encodeURIComponent(body.idempotency_key);
   if(storage.getItem(key)!==null)fail('此请求键已有保存记录，请先核对。');
   const r={schema:2,actorId:actor,key:body.idempotency_key,fileHash,bodyJson,summary:copy(summary||{}),phase:'unknown',createdAt:new Date().toISOString()};
   const raw=JSON.stringify(r);storage.setItem(key,raw);if(storage.getItem(key)!==raw)fail('浏览器未能保留完整原请求，保存尚未发出，请检查存储设置。');
   return {...r,storageKey:key};
  }
  function current(record){const r=read(record.storageKey);if(!r||r.key!==record.key||r.fileHash!==record.fileHash||r.bodyJson!==record.bodyJson)fail('原保存缓存已变化，请重新查询，不能覆盖其它请求。');return r}
  function complete(record,orderId){
   if(record.legacy)return {retained:true,legacy:true};
   if(!positive(orderId))fail('保存回执缺少订单身份。');
   try{
    const r=current(record);const saved={...r,phase:'confirmed',orderId};delete saved.storageKey;
    storage.setItem(record.storageKey,JSON.stringify(saved));
    const after=current(record);if(after.phase!=='confirmed'||after.orderId!==orderId)fail('完成记录未能保存');
    storage.removeItem(record.storageKey);if(storage.getItem(record.storageKey)!==null)fail('完成记录未能清理');
    return {retained:false};
   }catch(error){return {retained:true,message:'订单已保存；恢复缓存未清理，请查询原结果，不要再次保存。',error:String(error?.message||error)}}
  }
  function releaseRejected(record){
   if(record.legacy||record.phase==='confirmed')fail('此记录不能按本次拒绝结束，请查询原结果。');
   current(record);storage.removeItem(record.storageKey);
   if(storage.getItem(record.storageKey)!==null)fail('原拒绝记录无法清理，请保留并查询原结果，暂不能更正发送。');
  }
  return {all,find,prepare,current,complete,releaseRejected};
 }
 global.ERPOrderImportSaveRecovery={createStore,positive,copy,projection,decimal,equal,priceMatches,validateComplete,validateTrace};
})(typeof window==='undefined'?globalThis:window);
