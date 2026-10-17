(function(global) {
  'use strict';
  let state = null, inFlight = null;
  async function request(url, method='GET', body) {
    const response = await fetch(url, {method, credentials:'same-origin', cache:'no-store',
      headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作未完成，请核对送货单状态');
    return data;
  }
  async function prepare(data) {
    const api = global.TmDeliveryDispatchRecovery;
    const store = api.create(data.current_actor_id, global.localStorage);
    const pending = store.list().filter(r=>r.deliveryId===data.id && !['closed','confirmed'].includes(r.state));
    if (pending.length > 1) throw new Error('本送货单存在多个待核对请求，请返回送货单查原结果');
    state = {id:data.id, actor:data.current_actor_id, status:data.status, version:data.delivery_version,
             store, record:pending[0] || null, packet:null};
    if (!state.record && data.status === 'pending') {
      const packet = await request(`/api/deliveries/${data.id}/dispatch-snapshot`);
      api.validateSnapshot(packet, state.actor, state.id, state.version);
      state.packet = packet;
    }
    if (!['pending','dispatched'].includes(data.status)) throw new Error('当前送货单不能打印发货');
  }
  async function run(data) {
    if (!state || state.id!==data.id || state.actor!==data.current_actor_id) throw new Error('请重新读取送货单后打印');
    const {store, actor, id} = state, api = global.TmDeliveryDispatchRecovery;
    let record = state.record;
    if (record) {
      if (record.state==='closing') throw new Error('原发货正在结束确认，请返回送货单查原结果');
      const value = await request(`/api/deliveries/${id}/dispatch-results/${encodeURIComponent(record.key)}/resolve`, 'POST',
        {expected_actor_id:actor, original_request:record.body});
      api.validateResolve(value,record,actor);
      if (value.status==='completed') { store.confirm(record.key,value.dispatch_receipt,value.current);state.record=null;state.status='dispatched';return; }
      if (value.status!=='not_recorded') throw new Error('原发货结果待核对，请返回送货单查原结果');
    } else if (state.status==='dispatched') return;
    if (!record) {
      const p = state.packet;
      if (!p || p.snapshot.delivery.version!==state.version) throw new Error('送货内容已变化，请重新读取');
      const body = {expected_actor_id:actor,idempotency_key:global.TmOperationKey.create(),
        expected_version:state.version,snapshot_hash:p.snapshot_hash,snapshot:p.snapshot,confirm_pick_exception:false};
      record = store.prepare(body,p.display);
    }
    state.record=record;
    try {
      store.sending(record.key);
      const value = await request(`/api/deliveries/${id}/dispatch`,'PUT',record.body);
      api.validateCompleted(value,record,actor);
      store.confirm(record.key,value.dispatch_receipt,value.current);
      state.record=null;state.status='dispatched';data.status='dispatched';
    } catch(error) { store.unknown(record.key);throw error; }
  }
  function confirm(data) {
    if (inFlight) return inFlight;
    inFlight=run(data).finally(()=>{inFlight=null;});return inFlight;
  }
  global.TmDeliveryPrintDispatch={prepare,confirm};
})(typeof window==='undefined'?globalThis:window);
