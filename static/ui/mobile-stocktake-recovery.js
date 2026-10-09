(function (global) {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value));
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const nonnegative = value => Number.isSafeInteger(value) && value >= 0;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = value => typeof value === "string" && value.trim() !== "";
  // Pure UTF-8 SHA256 also works on factory HTTP LAN pages without crypto.subtle.
  function sha256(value) {
    const bytes = new TextEncoder().encode(value), size = Math.ceil((bytes.length + 9) / 64) * 64;
    const padded = new Uint8Array(size); padded.set(bytes); padded[bytes.length] = 128;
    const view = new DataView(padded.buffer); view.setUint32(size - 4, bytes.length * 8);
    const constants = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    const hash = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    const rotate = (n, bits) => (n >>> bits) | (n << (32 - bits));
    for (let offset = 0; offset < size; offset += 64) {
      const words = new Uint32Array(64);
      for (let i = 0; i < 16; i++) words[i] = view.getUint32(offset + i * 4);
      for (let i = 16; i < 64; i++) { const a = words[i-15], b = words[i-2]; words[i] = (words[i-16] + (rotate(a,7)^rotate(a,18)^(a>>>3)) + words[i-7] + (rotate(b,17)^rotate(b,19)^(b>>>10))) >>> 0; }
      let [a,b,c,d,e,f,g,h] = hash;
      for (let i = 0; i < 64; i++) { const first = (h + (rotate(e,6)^rotate(e,11)^rotate(e,25)) + ((e&f)^(~e&g)) + constants[i] + words[i]) >>> 0, second = ((rotate(a,2)^rotate(a,13)^rotate(a,22)) + ((a&b)^(a&c)^(b&c))) >>> 0; h=g;g=f;f=e;e=(d+first)>>>0;d=c;c=b;b=a;a=(first+second)>>>0; }
      [a,b,c,d,e,f,g,h].forEach((word, i) => hash[i] = (hash[i] + word) >>> 0);
    }
    return hash.map(word => word.toString(16).padStart(8,"0")).join("");
  }
  function validBody(body, owner, sending = false) {
    return object(body) && positive(body.location_id) && text(body.idempotency_key) && body.idempotency_key === body.idempotency_key.trim() && body.idempotency_key.length <= 120 && body.expected_actor_id === owner &&
      (body.location_layout_version === null || positive(body.location_layout_version)) && (sending ? positive(body.location_address_version) : nonnegative(body.location_address_version)) && text(body.location_position_status) && (!sending || body.location_position_status.length <= 30) &&
      (body.published_map_revision === null || (typeof body.published_map_revision === "string" && (!sending || (text(body.published_map_revision) && body.published_map_revision.length <= 64)))) && Array.isArray(body.items) &&
      body.items.every(row => object(row) && positive(row.inventory_lot_id) && nonnegative(row.counted_quantity) && (sending ? positive(row.expected_version) : nonnegative(row.expected_version)) && nonnegative(row.expected_available) && nonnegative(row.expected_reserved)) &&
      new Set(body.items.map(row=>row.inventory_lot_id)).size === body.items.length;
  }
  function create(options) {
    const owner = options.ownerId;
    if (!positive(owner)) throw Error("请先确认登录账号");
    const prefix = `tm-mobile-stocktake-recovery-v1:${owner}:`;
    const confirmedRecords = new Map();
    let records = [], pending = null, busy = false, issue = "", message = "", accountChanged = false, notFound = false, active = true;
    const current = () => active && (!options.isCurrent || options.isCurrent());
    const keyFor = record => prefix + encodeURIComponent(record.body.idempotency_key);
    const snapshot = () => ({records:copy(records),pending:pending?copy(pending):null,busy,issue,message,accountChanged,canRetry:notFound && !accountChanged && !issue && pending?.outcome!=="confirmed",blocked:busy || records.some(record=>record.body.location_id===options.locationId()) || !!issue || accountChanged});
    function notify() { if (current()) options.onChange(snapshot()); }
    function readRecords() {
      const rows = [];
      for (let i=0; i<options.storage.length; i++) {
        const key = options.storage.key(i);
        if (!key?.startsWith(prefix)) continue;
        const raw = options.storage.getItem(key); if (raw === null) continue;
        const record = JSON.parse(raw);
        if (!object(record) || record.version!==1 || record.ownerId!==owner || !["submit","confirm"].includes(record.action) || !validBody(record.body,owner) || !["unknown","confirmed"].includes(record.outcome) || !object(record.snapshot) || !object(record.snapshot.location) || record.snapshot.location.id!==record.body.location_id || !Array.isArray(record.snapshot.lots) || key!==keyFor(record)) throw Error("本机盘点记录不完整，请保留记录并联系管理员核对");
        const known = confirmedRecords.get(key);
        rows.push(known && JSON.stringify(known.body)===JSON.stringify(record.body)?copy(known):record);
      }
      return rows.sort((a,b)=>(a.createdAt||0)-(b.createdAt||0) || a.body.idempotency_key.localeCompare(b.body.idempotency_key));
    }
    function reload() {
      if (busy || !current()) return;
      try { const previous=pending; records=readRecords();pending=records.find(row=>row.body.idempotency_key===previous?.body.idempotency_key)||records[0]||null;if(previous?.outcome==="confirmed" && pending && JSON.stringify(previous.body)===JSON.stringify(pending.body))pending=previous;issue="";notFound=false; }
      catch (error) { issue="无法读取本机盘点记录。请检查浏览器存储后重新读取；原记录已保留。"; }
      notify();
    }
    function preserve(error) {
      notFound=false;
      if(pending)pending.lastError=error?.message||"未收到完整回执";
      if(error?.code==="STOCKTAKE_ACTOR_MISMATCH") accountChanged=true;
      issue="";
      message=accountChanged?"账号已变化，请切回原账号核对；原盘点请求已保留。":`盘点结果暂未确认。原数量和请求已保留，请先核对这笔盘点。${error?.message?" "+error.message:""}`;
      notify();
      if(current()) options.onUnknown(message);
    }
    function identity(result) {
      if (!object(result) || !positive(result.current_actor_id)) throw Error("盘点回执不完整，请再次核对");
      if(result.current_actor_id!==owner) { accountChanged=true; throw Error("账号已变化，请切回原账号核对"); }
    }
    function validate(order, record) {
      identity(order);
      const body=record.body;
      if(order.request_action!==record.action || order.request_idempotency_key!==body.idempotency_key || !positive(order.id) || !text(order.stocktake_number) || !positive(order.version) || order.submitted_by!==owner || order.location_id!==body.location_id || order.location_layout_version!==body.location_layout_version || order.location_address_version!==body.location_address_version || order.location_position_status!==body.location_position_status || order.published_map_revision!==body.published_map_revision || order.idempotency_key!==(record.action==="confirm"?"mobile-confirm-"+sha256(body.idempotency_key):body.idempotency_key) || !Array.isArray(order.items) || order.items.length!==body.items.length || !Array.isArray(order.reviews)) throw Error("盘点回执不完整或与原请求不一致，请再次核对");
      if(record.action==="confirm" ? order.status!=="approved" : !["submitted","approved","rejected"].includes(order.status)) throw Error("盘点状态尚不能确认，请再次核对");
      if(order.status!=="submitted" && (!positive(order.reviewed_by) || !text(order.reviewed_at) || !order.reviews.some(row=>positive(row.id) && row.reviewed_by===order.reviewed_by && row.reviewed_at===order.reviewed_at && row.from_status==="submitted" && row.action===(order.status==="approved"?"approve":"reject") && row.to_status===order.status))) throw Error("盘点审核回执不完整，请再次核对");
      const seen=new Set();
      for(const row of order.items) {
        const original=body.items.find(item=>item.inventory_lot_id===row.inventory_lot_id);
        if(!original || seen.has(row.inventory_lot_id) || row.counted_quantity!==original.counted_quantity || row.lot_version_snapshot!==original.expected_version || row.quantity_available_snapshot!==original.expected_available || row.quantity_reserved_snapshot!==original.expected_reserved) throw Error("盘点批次回执与原数量或快照不一致，请再次核对");
        seen.add(row.inventory_lot_id);
      }
    }
    function receiptText(order) {
      return order.status==="approved"?"盘点已确认，ERP库存已按实盘数量更新。":order.status==="submitted"?"盘点已上报，等待仓库审核；库存尚未更新。":"这笔盘点已被审核退回，请重新核对后再发起盘点。";
    }
    function complete(order, record) {
      if(!current()) return;
      record.outcome="confirmed";record.receipt=copy(order);pending=record;
      confirmedRecords.set(keyFor(record),record);
      let cleaned=false;
      try {
        const key=keyFor(record),raw=options.storage.getItem(key);
        if(raw===null) { cleaned=true; }
        else {
          const stored=JSON.parse(raw);
          if(stored.ownerId!==owner || stored.action!==record.action || JSON.stringify(stored.body)!==JSON.stringify(record.body)) throw Error("本机原记录已变化");
          options.storage.setItem(key,JSON.stringify(record));
          if(options.storage.getItem(key)!==JSON.stringify(record)) throw Error("本机原记录已变化");
          options.storage.removeItem(key);cleaned=true;
        }
      } catch (error) { record.cleanupError="这笔盘点已确认，本机记录未能清理。请重新读取后只核对这笔，勿重复提交。"; }
      try { records=readRecords(); }
      catch(error) { issue="无法读取本机盘点记录。请保留记录并检查浏览器存储后重新读取。"; }
      notFound=false;
      message=receiptText(order);
      if(cleaned) { pending=records.find(row=>row.body.location_id===record.body.location_id)||null; }
      notify();
      options.onReceipt(copy(order),receiptText(order),{cleaned,remaining:records.length,record:copy(record)});
    }
    async function post(record) {
      busy=true;notFound=false;notify();
      try {
        const order=await options.request(record.action==="confirm"?"/api/warehouse/stocktakes/confirm":"/api/warehouse/stocktakes",{method:"POST",body:JSON.stringify(record.body),isCurrent:current});
        if(!current()) return;
        validate(order,record);complete(order,record);
      } catch(error) {
        if(!current()) return;
        preserve(error);
        if(error.status===401)options.onAuth(error);
      } finally { if(current()){busy=false;notify();} }
    }
    async function submit(action,body,originalSnapshot) {
      if(!current() || busy || accountChanged) return;
      if(!validBody(body,owner,true) || !["submit","confirm"].includes(action)) { options.onUnknown("盘点请求不完整，本次没有发送。请重新读取货位并核对输入。");return; }
      try {
        records=readRecords();
        if(records.some(record=>record.body.location_id===body.location_id) || issue) { pending=records.find(record=>record.body.location_id===body.location_id)||pending;notify();return; }
        const record={version:1,ownerId:owner,action,body:copy(body),snapshot:copy(originalSnapshot),createdAt:Date.now(),outcome:"unknown"};
        const key=keyFor(record);
        if(options.storage.getItem(key)!==null) throw Error("本机已有这笔盘点，请先核对");
        const raw=JSON.stringify(record);options.storage.setItem(key,raw);
        if(options.storage.getItem(key)!==raw) throw Error("本机盘点记录保存后已变化，请先核对");
        pending=record;records=readRecords();notify();
        // Independent request keys preserve both facts if another tab saves concurrently.
        if(records.filter(row=>row.body.location_id===body.location_id).length!==1) { options.onUnknown("本货位有多笔待确认盘点，请逐笔核对；原记录全部保留。");return; }
        await post(record);
      } catch(error) { if(current()){issue="无法安全保存本机盘点记录，本次没有发送。请重新读取记录后再试。";notify();options.onUnknown(issue);} }
    }
    async function resolve() {
      if(!current() || busy || accountChanged || !pending) return;
      const record=copy(pending);busy=true;notFound=false;notify();
      try {
        const result=await options.request("/api/warehouse/stocktakes/resolve",{method:"POST",body:JSON.stringify({action:record.action,body:record.body}),isCurrent:current});
        if(!current()) return;
        identity(result);
        if(result.request_action!==record.action || result.request_idempotency_key!==record.body.idempotency_key || !text(result.observed_at) || !["found","not_found"].includes(result.status)) throw Error("核对回执不完整，请再次核对");
        if(result.status==="found") { validate(result.order,record);complete(result.order,record); }
        else { if(result.order!==null)throw Error("核对回执不完整，请再次核对");if(record.outcome!=="confirmed")notFound=true;issue="";message=record.outcome==="confirmed"?"此前已确认成功，当前未能查到原回执。请继续只读核对，勿重复提交。":"当前没有找到匹配盘点单，仍不能确定原请求未执行。原条件若已变化，同一笔可能无法重试；请管理员核对盘点记录。";options.onUnknown(message); }
      } catch(error) { if(current()){preserve(error);if(error.status===401)options.onAuth(error);} }
      finally { if(current()){busy=false;notify();} }
    }
    async function retry() {
      if(!current() || busy || !notFound || accountChanged || issue || !pending || pending.outcome==="confirmed") return;
      const record=copy(pending);
      try { const stored=JSON.parse(options.storage.getItem(keyFor(record)));if(!stored || JSON.stringify(stored.body)!==JSON.stringify(record.body) || stored.outcome==="confirmed")throw Error("本机原记录已变化，请重新读取后核对");await post(record); }
      catch(error) { if(current()){issue=error.message;notify();} }
    }
    function select(key) { if(busy || !current())return;pending=records.find(record=>keyFor(record)===key)||pending;notFound=false;message="";notify(); }
    reload();
    return {submit,resolve,retry,reload,select,keyFor,state:snapshot,destroy(){active=false;}};
  }
  global.TmStocktakeRecovery={create,sha256};
})(window);
