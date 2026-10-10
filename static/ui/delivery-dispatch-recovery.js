(function (global) {
  'use strict';
  const PENDING = 'erp-delivery-dispatch:v1:', UNKNOWN = 'erp-delivery-dispatch-unknown:v1:', CLOSING = 'erp-delivery-dispatch-closing:v1:', CONFIRMED = 'erp-delivery-dispatch-confirmed:v1:';
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const nonnegative = value => Number.isSafeInteger(value) && value >= 0;
  const object = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const clone = value => JSON.parse(JSON.stringify(value));
  const stable = value => Array.isArray(value) ? '[' + value.map(stable).join(',') + ']' : object(value) ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + stable(value[key])).join(',') + '}' : JSON.stringify(value);
  const nullableId = value => value === null || positive(value);
  const nullableText = value => value === null || typeof value === 'string';
  const confirmedMemory = new Map();
  function requireValue(valid, message = '发货核对回执不完整，请重新核对') { if (!valid) throw new Error(message); }
  const fraction = (n, d = 1) => [BigInt(n),BigInt(d)];
  const add = (a,b) => [a[0]*b[1]+b[0]*a[1],a[1]*b[1]];
  const equal = (a,b) => a[0]*b[1] === b[0]*a[1];
  const less = (a,b) => a[0]*b[1] < b[0]*a[1];
  const bucket = (item,component,product) => JSON.stringify([item,component,product]);
  function snapshot(value, actor, id, version, validateDisplay = true) {
    requireValue(object(value) && value.schema_version === 1 && value.current_actor_id === actor && /^[a-f0-9]{64}$/.test(value.snapshot_hash));
    const s = value.snapshot, d = s?.delivery;
    requireValue(object(s) && s.schema_version === 1 && positive(d?.id) && d.id === id && positive(d.customer_id) && positive(d.version) && d.status === 'pending');
    if (positive(version) && d.version !== version) throw new Error('送货内容已变化，请刷新原单后重新核对；没有发货');
    requireValue(['order', 'unordered_finished', 'mixed'].includes(d.source_mode) && /^\d{4}-\d{2}-\d{2}$/.test(d.delivery_date) && nullableText(d.vehicle_number));
    requireValue(Array.isArray(s.items) && s.items.length > 0 && Array.isArray(s.expected_final_items) && s.expected_final_items.length === s.items.length && ['none', 'apply'].includes(s.pick_action));
    const ids = new Set();
    for (const item of s.items) {
      requireValue(positive(item.delivery_item_id) && !ids.has(item.delivery_item_id) && positive(item.revision_number) && positive(item.customer_id) && nullableId(item.order_item_id) && nullableId(item.product_id) && nullableId(item.source_product_id));
      ids.add(item.delivery_item_id);
      requireValue(['order', 'unordered_finished'].includes(item.source_type) && positive(item.customer_quantity) && positive(item.physical_quantity) && nullableText(item.customer_unit) && nullableText(item.physical_unit) && (item.quantity_contract === null || object(item.quantity_contract)));
      requireValue(Array.isArray(item.goods) && item.goods.length > 0 && item.goods.every(g => nullableId(g.component_snapshot_id) && nullableId(g.source_product_id) && positive(g.customer_id) && positive(g.physical_quantity) && nullableText(g.physical_unit)));
      requireValue(Array.isArray(item.semi_requirements) && new Set(item.semi_requirements.map(r=>r.requirement_id)).size===item.semi_requirements.length && item.semi_requirements.every(r=>positive(r.requirement_id)&&positive(r.pieces_per_box)) && Array.isArray(item.direct_completion_sources) && new Set(item.direct_completion_sources.map(r=>r.completion_id)).size===item.direct_completion_sources.length && item.direct_completion_sources.every(r=>positive(r.completion_id)&&positive(r.direct_quantity)) && (item.order_delivered_quantity===null || nonnegative(item.order_delivered_quantity)));
      requireValue(Array.isArray(item.allocations) && (item.allocation_mode === 'on_dispatch' ? item.source_type === 'order' && item.allocations.length === 0 : item.allocation_mode === 'explicit_unordered' && item.source_type === 'unordered_finished' && item.allocations.length > 0 && item.allocations.every(a => positive(a.allocation_id) && positive(a.inventory_lot_id) && positive(a.planned_physical_quantity) && typeof a.status === 'string')));
    }
    const finals = new Set();
    for (const item of s.expected_final_items) {
      requireValue(ids.has(item.delivery_item_id) && !finals.has(item.delivery_item_id) && nonnegative(item.customer_quantity) && nonnegative(item.physical_quantity) && nullableText(item.customer_unit) && nullableText(item.physical_unit));
      requireValue(Array.isArray(item.goods) && item.goods.length>0 && item.goods.every(g=>nullableId(g.component_snapshot_id)&&nullableId(g.source_product_id)&&positive(g.customer_id)&&nonnegative(g.physical_quantity)&&nullableText(g.physical_unit)));
      finals.add(item.delivery_item_id);
    }
    const task = s.pick_task;
    requireValue(s.pick_action !== 'apply' || object(task));
    requireValue(task === null || object(task) && positive(task.id) && task.delivery_id === d.id && positive(task.snapshot_version) && nullableText(task.route_snapshot_version) && nullableText(task.print_version) && typeof task.status === 'string' && Array.isArray(task.items) && task.items.length > 0);
    if (task) {
      const taskIds = new Set();
      for (const item of task.items) {
        requireValue(positive(item.task_item_id) && !taskIds.has(item.task_item_id) && ids.has(item.delivery_item_id) && nullableId(item.order_item_id) && nonnegative(item.original_physical_quantity) && nonnegative(item.picked_physical_quantity) && typeof item.pick_status === 'string' && typeof item.location_plan_complete === 'boolean' && Array.isArray(item.location_lines));
        taskIds.add(item.task_item_id);
      }
    }
    requireValue(object(value.display));
    if (validateDisplay) {
      const display = value.display;
      requireValue(typeof display.delivery_number === 'string' && display.delivery_number.length > 0 && nullableText(display.customer_name) && Array.isArray(display.items) && display.items.length === s.items.length);
      const shown = new Set();
      for (const line of display.items) {
        const item = s.items.find(i => i.delivery_item_id === line.delivery_item_id), final = s.expected_final_items.find(i => i.delivery_item_id === line.delivery_item_id);
        requireValue(item && final && !shown.has(line.delivery_item_id) && nullableText(line.product_code) && nullableText(line.product_name) && line.original_customer_quantity === item.customer_quantity && line.original_physical_quantity === item.physical_quantity && line.final_customer_quantity === final.customer_quantity && line.final_physical_quantity === final.physical_quantity && line.customer_unit === final.customer_unit && line.physical_unit === final.physical_unit && line.allocation_mode === item.allocation_mode);
        shown.add(line.delivery_item_id);
      }
    }
    return clone(value);
  }
  function command(body, actor) {
    requireValue(object(body) && body.expected_actor_id === actor && typeof body.idempotency_key === 'string' && body.idempotency_key.length >= 8 && body.idempotency_key.length <= 120 && positive(body.expected_version) && typeof body.confirm_pick_exception === 'boolean');
    snapshot({ schema_version: 1, current_actor_id: actor, snapshot: body.snapshot, snapshot_hash: body.snapshot_hash, display: {} }, actor, body.snapshot?.delivery?.id, body.expected_version, false);
    return clone(body);
  }
  function completed(value, record, actor, resolving = false) {
    requireValue(object(value) && value.current_actor_id === actor && (resolving ? value.status === 'completed' : value.schema_version === 1 && value.result === 'completed'));
    const body = record.body, receipt = value.dispatch_receipt;
    command(body, actor);
    const request = clone(body); delete request.expected_actor_id; delete request.idempotency_key;
    requireValue(object(receipt) && receipt.schema_version === 1 && receipt.idempotency_key === record.key && receipt.actor_id === record.actor && /^[a-f0-9]{64}$/.test(receipt.request_hash) && stable(receipt.request) === stable(request));
    requireValue(receipt.delivery_id === record.deliveryId && receipt.customer_id === body.snapshot.delivery.customer_id && typeof receipt.delivery_number === 'string' && receipt.delivery_number.length > 0 && receipt.original_version === body.expected_version && positive(receipt.completed_version) && receipt.completed_version > receipt.original_version && typeof receipt.dispatched_at === 'string' && receipt.dispatched_at.length > 0);
    requireValue(object(receipt.stages) && receipt.stages.prepared === true && receipt.stages.dispatched === true && receipt.stages.pick_applied === (body.snapshot.pick_action === 'apply') && receipt.stages.pick_task_id === (body.snapshot.pick_task?.id ?? null));
    requireValue(stable(receipt.original_items) === stable(body.snapshot.items) && Array.isArray(receipt.final_items) && Array.isArray(receipt.removed_delivery_item_ids) && Array.isArray(receipt.movements) && Array.isArray(receipt.direct_component_allocations) && Array.isArray(receipt.direct_completion_coverage));
    const expected = body.snapshot.expected_final_items, originals = body.snapshot.items, seen = new Set();
    for (const item of receipt.final_items) {
      const source = originals.find(line => line.delivery_item_id === item.delivery_item_id), final = expected.find(line => line.delivery_item_id === item.delivery_item_id);
      requireValue(source && final && !seen.has(item.delivery_item_id) && positive(item.revision_number) && item.product_id === source.product_id && item.allocation_mode === source.allocation_mode && item.source_type === source.source_type && item.order_item_id === source.order_item_id && item.source_product_id === source.source_product_id && item.customer_id === source.customer_id);
      requireValue(positive(final.customer_quantity) && item.customer_quantity === final.customer_quantity && item.physical_quantity === final.physical_quantity && nullableText(item.customer_unit) && nullableText(item.physical_unit) && (final.customer_unit === null || item.customer_unit === final.customer_unit) && (final.physical_unit === null || item.physical_unit === final.physical_unit) && stable(item.quantity_contract) === stable(source.quantity_contract));
      requireValue(Array.isArray(item.goods) && item.goods.length > 0 && item.goods.every(g => nullableId(g.component_snapshot_id) && nullableId(g.source_product_id) && positive(g.customer_id) && nonnegative(g.physical_quantity) && nullableText(g.physical_unit)) && Array.isArray(item.allocation_ids) && item.allocation_ids.every(positive));
      seen.add(item.delivery_item_id);
    }
    for (const id of receipt.removed_delivery_item_ids) {
      const final = expected.find(line => line.delivery_item_id === id);
      requireValue(positive(id) && !seen.has(id) && final && final.customer_quantity === 0 && final.physical_quantity === 0);
      seen.add(id);
    }
    requireValue(seen.size === originals.length);
    const movementIds = new Set();
    for (const movement of receipt.movements) {
      requireValue(positive(movement.movement_id) && !movementIds.has(movement.movement_id) && positive(movement.inventory_lot_id) && nullableId(movement.source_product_id) && nullableId(movement.owner_customer_id) && receipt.final_items.some(item => item.delivery_item_id === movement.delivery_item_id) && nullableId(movement.component_snapshot_id) && positive(movement.physical_quantity) && nullableText(movement.unit) && nullableId(movement.location_id) && movement.operator_id === actor && typeof movement.occurred_at === 'string' && movement.occurred_at.length > 0);
      movementIds.add(movement.movement_id);
    }
    const directIds = new Set();
    for (const allocation of receipt.direct_component_allocations) {
      requireValue(positive(allocation.allocation_id) && !directIds.has(allocation.allocation_id) && receipt.final_items.some(item => item.delivery_item_id === allocation.delivery_item_id) && positive(allocation.production_completion_id) && positive(allocation.component_snapshot_id) && positive(allocation.physical_quantity));
      directIds.add(allocation.allocation_id);
    }
    conservation(receipt,body);
    if (!resolving) requireValue(value.current === null);
    return clone(receipt);
  }
  function conservation(receipt,body) {
    const wanted=new Map(),credits=new Map(),semi=new Map();
    const credit=(key,value)=>credits.set(key,add(credits.get(key)||fraction(0),value));
    // Ordinary parent reservation/direct credit is in customer quantity; goods is physical quantity.
    // Component, subkit and unordered credit already uses its own frozen requirement unit.
    const parentCredit=(key,value)=>{
      const [id,component,product]=JSON.parse(key),source=body.snapshot.items.find(i=>i.delivery_item_id===id),final=receipt.final_items.find(i=>i.delivery_item_id===id);
      return source.source_type==='order' && component===null && product===source.source_product_id ? [value[0]*BigInt(final.physical_quantity),value[1]*BigInt(final.customer_quantity)] : value;
    };
    for (const final of receipt.final_items) {
      const expected=body.snapshot.expected_final_items.find(i=>i.delivery_item_id===final.delivery_item_id);
      requireValue(final.goods.length===expected.goods.length);
      const canonical=goods=>goods.map(g=>{const next=clone(g);delete next.physical_unit;return next;});
      requireValue(stable(canonical(final.goods))===stable(canonical(expected.goods)));
      final.goods.forEach((good,index)=>{
        requireValue(expected.goods[index].physical_unit===null || good.physical_unit===expected.goods[index].physical_unit);
        const key=bucket(final.delivery_item_id,good.component_snapshot_id,good.source_product_id);
        wanted.set(key,add(wanted.get(key)||fraction(0),fraction(good.physical_quantity)));
      });
    }
    for (const movement of receipt.movements) {
      requireValue(nullableId(movement.requirement_product_id) && positive(movement.requirement_quantity) && positive(movement.requirement_denominator) && ['inventory','semi','subkit'].includes(movement.source_kind) && nullableId(movement.semi_requirement_id));
      const key=bucket(movement.delivery_item_id,movement.component_snapshot_id,movement.requirement_product_id);
      requireValue(wanted.has(key));
      const amount=fraction(movement.requirement_quantity,movement.requirement_denominator);
      if (movement.source_kind==='semi') {
        const source=body.snapshot.items.find(i=>i.delivery_item_id===movement.delivery_item_id);
        requireValue(positive(movement.semi_requirement_id) && source.semi_requirements.some(r=>r.requirement_id===movement.semi_requirement_id));
        if(!semi.has(key))semi.set(key,new Map());
        const materials=semi.get(key);materials.set(movement.semi_requirement_id,add(materials.get(movement.semi_requirement_id)||fraction(0),amount));
      } else { requireValue(movement.semi_requirement_id===null);credit(key,movement.source_kind==='inventory'?parentCredit(key,amount):amount); }
    }
    for (const [key,materials] of semi) {
      const id=JSON.parse(key)[0],source=body.snapshot.items.find(i=>i.delivery_item_id===id);let coverage=null;
      for (const requirement of source.semi_requirements) {
        const amount=materials.get(requirement.requirement_id)||fraction(0),value=[amount[0],amount[1]*BigInt(requirement.pieces_per_box)];
        if(coverage===null||less(value,coverage))coverage=value;
      }
      requireValue(coverage!==null);credit(key,parentCredit(key,coverage));
    }
    for (const allocation of receipt.direct_component_allocations) {
      const goods=receipt.final_items.find(i=>i.delivery_item_id===allocation.delivery_item_id).goods.filter(g=>g.component_snapshot_id===allocation.component_snapshot_id);
      requireValue(goods.length===1);credit(bucket(allocation.delivery_item_id,allocation.component_snapshot_id,goods[0].source_product_id),fraction(allocation.physical_quantity));
    }
    const directIds=new Set();
    for (const direct of receipt.direct_completion_coverage) {
      const source=body.snapshot.items.find(i=>i.delivery_item_id===direct.delivery_item_id);
      requireValue(source && !directIds.has(direct.delivery_item_id) && direct.order_item_id===source.order_item_id && positive(direct.order_item_id) && direct.source_product_id===source.source_product_id && stable(direct.completion_sources)===stable(source.direct_completion_sources) && direct.completion_sources.length>0 && nonnegative(direct.delivered_before) && direct.delivered_before===source.order_delivered_quantity && nonnegative(direct.available_before) && positive(direct.credited_quantity));
      const total=source.direct_completion_sources.reduce((sum,c)=>sum+BigInt(c.direct_quantity),0n),available=total-BigInt(direct.delivered_before),bounded=available>0n?available:0n;
      requireValue(BigInt(direct.available_before)===bounded && BigInt(direct.credited_quantity)<=bounded);
      const key=bucket(direct.delivery_item_id,null,direct.source_product_id);requireValue(wanted.has(key));credit(key,parentCredit(key,fraction(direct.credited_quantity)));directIds.add(direct.delivery_item_id);
    }
    for (const [key,quantity] of wanted) requireValue(equal(quantity,credits.get(key)||fraction(0)));
    for (const key of credits.keys()) requireValue(wanted.has(key));
  }
  function resolve(value, record, actor) {
    requireValue(object(value) && ['completed', 'closed', 'not_recorded', 'trace'].includes(value.status) && value.current_actor_id === actor && value.idempotency_key === record.key);
    if (value.status !== 'closed') requireValue(value.closure_receipt === null);
    if (value.status === 'completed') { completed(value, record, actor, true); requireValue(value.trace === null); }
    else if (value.status === 'closed') { closed(value,record,actor,true); requireValue(value.trace === null); }
    else requireValue(value.dispatch_receipt === null && (value.status === 'not_recorded' ? value.trace === null : object(value.trace) && value.trace.delivery_id === record.deliveryId && typeof value.trace.delivery_number === 'string' && value.trace.url === '/api/deliveries/' + record.deliveryId));
    requireValue(object(value.current) && value.current.delivery_id === record.deliveryId && positive(value.current.customer_id) && positive(value.current.version) && typeof value.current.delivery_number === 'string' && typeof value.current.status === 'string' && nonnegative(value.current.total_quantity) && nullableText(value.current.dispatched_at));
    return value;
  }
  function closed(value,record,actor,resolving=false) {
    requireValue(object(value) && value.current_actor_id === actor && (resolving ? value.status === 'closed' : value.schema_version === 1 && value.result === 'closed') && value.dispatch_receipt === null);
    command(record.body,actor);
    const proof=value.closure_receipt,request=clone(record.body);delete request.expected_actor_id;delete request.idempotency_key;
    requireValue(object(proof) && proof.schema_version === 1 && proof.idempotency_key === record.key && proof.actor_id === actor && /^[a-f0-9]{64}$/.test(proof.request_hash) && stable(proof.request) === stable(request) && proof.delivery_id === record.deliveryId && proof.customer_id === record.body.snapshot.delivery.customer_id && typeof proof.delivery_number === 'string' && proof.delivery_number.length>0 && typeof proof.closed_at === 'string' && proof.closed_at.length>0);
    if (!resolving) requireValue(value.current === null);
    return clone(proof);
  }
  function create(actor, storage) {
    requireValue(positive(actor), '请登录后核对发货记录');
    const stem = actor + ':', keyOf = (prefix, key) => prefix + stem + key;
    function read(prefix, key) { try { const raw = storage.getItem(keyOf(prefix, key)); return raw ? JSON.parse(raw) : null; } catch (_) { throw new Error('无法读取待核对发货记录，请恢复浏览器存储后核对；没有新发货'); } }
    function write(prefix, record) {
      try { storage.setItem(keyOf(prefix, record.key), JSON.stringify(record)); if (stable(read(prefix, record.key)) !== stable(record)) throw new Error('readback'); }
      catch (_) { throw new Error('无法保留原发货内容，请检查浏览器存储空间；没有发送新发货'); }
    }
    function get(key) {
      const record = confirmedMemory.get(stem + key) || read(CONFIRMED, key) || read(CLOSING,key) || read(UNKNOWN, key) || read(PENDING, key);
      if (record) {
        requireValue(record.actor === actor && record.key === key && record.body?.idempotency_key === key && record.body.expected_actor_id === actor && positive(record.deliveryId) && record.body.snapshot?.delivery?.id === record.deliveryId && ['prepared','sending','unknown','closing','confirmed','closed'].includes(record.state), '待核对发货记录损坏，请按原单号联系管理员核对');
        command(record.body,actor);
        snapshot({schema_version:1,current_actor_id:actor,snapshot:record.body.snapshot,snapshot_hash:record.body.snapshot_hash,display:record.display},actor,record.deliveryId);
        if (record.state === 'confirmed') completed({schema_version:1,result:'completed',current_actor_id:actor,dispatch_receipt:record.receipt,current:null},record,actor);
        if (record.state === 'closed') closed({schema_version:1,result:'closed',current_actor_id:actor,dispatch_receipt:null,closure_receipt:record.closure,current:null},record,actor);
      }
      return record;
    }
    function list() {
      const keys = new Set();
      try { for (let i = 0; i < storage.length; i++) { const key = storage.key(i); for (const prefix of [PENDING, UNKNOWN, CLOSING, CONFIRMED]) if (key?.startsWith(prefix + stem)) keys.add(key.slice((prefix + stem).length)); } }
      catch (_) { throw new Error('无法读取待核对发货记录，请恢复浏览器存储后核对'); }
      return [...keys].map(get).filter(Boolean).sort((a, b) => a.createdAt - b.createdAt);
    }
    function prepare(body, display) {
      command(body, actor);
      const existing = get(body.idempotency_key);
      if (existing) { requireValue(stable(existing.body) === stable(body), '原发货内容不能修改，请先查原结果'); return existing; }
      requireValue(!list().some(r => r.deliveryId === body.snapshot.delivery.id && !['confirmed','closed'].includes(r.state)), '该送货单已有待核对发货，请先查原结果；其他送货单不受影响');
      const record = { schema: 1, actor, key: body.idempotency_key, deliveryId: body.snapshot.delivery.id, body: clone(body), display: clone(display), state: 'prepared', createdAt: Date.now() };
      write(PENDING, record); return record;
    }
    function sending(key) { const r = get(key); requireValue(r && !['confirmed','closed','closing'].includes(r.state), '原发货已确认或正在结束，请查原结果'); const next = { ...r, state: r.state === 'prepared' ? 'sending' : 'unknown' }; write(PENDING, next); return get(key); }
    function unknown(key) { const r = get(key); requireValue(!!r, '没有找到原发货请求，请按原单号核对'); if (['confirmed','closed','closing'].includes(r.state)) return r; const next = { ...r, state: 'unknown' }; write(UNKNOWN, next); return get(key); }
    function closing(key) { const r=get(key);requireValue(!!r,'没有找到原发货请求，请按原单号核对');if(['confirmed','closed'].includes(r.state))return r;write(CLOSING,{...r,state:'closing'});return get(key); }
    function confirm(key, receipt, current) {
      const r = get(key); requireValue(!!r, '原发货请求未保留，请联系管理员核对');
      if (r.state === 'closed') return r;
      const next = { ...r, state: 'confirmed', receipt: clone(receipt), current: clone(current ?? null) };
      // Independent confirmed proof is never removed by a pending/unknown writer from another tab.
      confirmedMemory.set(stem + key, next);
      write(CONFIRMED, next); return get(key);
    }
    function close(key,proof,current) { const r=get(key);requireValue(!!r,'原请求未保留，请查原结果');if(['confirmed','closed'].includes(r.state))return r;const next={...r,state:'closed',closure:clone(proof),current:clone(current??null)};try { write(CONFIRMED,next); } catch (error) { confirmedMemory.set(stem+key,{...next,state:'closing'});throw error; }confirmedMemory.set(stem+key,next);return get(key); }
    return { actor, get, list, prepare, sending, unknown, closing, confirm, close };
  }
  global.TmDeliveryDispatchRecovery = { create, validateSnapshot: snapshot, validateCommand: command, validateCompleted: completed, validateClosed:closed, validateResolve: resolve, stable, positive };
})(typeof window === 'undefined' ? globalThis : window);
