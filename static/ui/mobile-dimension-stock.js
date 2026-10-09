(() => {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const digits = value => String(value).replace(/[^0-9]/g, "").slice(0, 5);
  const base = "/api/mobile/erp/warehouse/dimension-stock";
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const validBody = body => body && positive(body.quantity) && body.quantity <= 2147483647
    && ["cash", "sample"].includes(body.purpose) && positive(body.expected_version) && positive(body.location_id)
    && Number.isSafeInteger(body.address_version) && body.address_version >= 0
    && typeof body.idempotency_key === "string" && /^[A-Za-z0-9_-]{16,64}$/.test(body.idempotency_key);
  const validReceipt = (result, quantity) => result && !Array.isArray(result) && positive(result.movement_id)
    && result.quantity === quantity && positive(result.quantity) && typeof result.replayed === "boolean";
  const sameBody = (a, b) => ["quantity", "purpose", "expected_version", "location_id", "address_version", "idempotency_key"].every(key => a?.[key] === b?.[key]);
  let current = null;
  function destroy() { current?.destroy(); current = null; }
  function mount({apiGet, apiPost, userId, displayName = "当前账号"}) {
    if (!positive(userId)) { destroy(); return; }
    if (current?.ownerId === userId && current.live) return;
    destroy();
    const root = document.getElementById("dimensionStock");
    if (!root) return;
    root.innerHTML = `<style>
      #dimensionStock {margin-bottom:18px;min-width:0} #dimensionStock h2{margin:0 0 10px;font-size:18px}
      #dimensionStock .ds-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
      #dimensionStock .ds-axis{display:grid;grid-template-columns:28px 58px minmax(0,1fr);align-items:center;gap:4px;margin-top:8px}
      #dimensionStock input,#dimensionStock select{min-width:0;width:100%;font-size:16px;min-height:42px;box-sizing:border-box}
      #dimensionStock button{min-height:44px;padding:8px 12px} #dimensionStock .ds-result{display:block;width:100%;text-align:left;margin-top:8px;white-space:normal;overflow-wrap:anywhere;background:#f4f8ff;color:#163253;border:1px solid #cbd5e1;border-radius:8px}
      #dimensionStock .ds-actions{display:flex;gap:8px;margin-top:10px} #dimensionStock .ds-actions>*{flex:1}
      #dimensionStock .ds-take{background:#b91c1c;color:white;border:0;border-radius:8px} #dimensionStock .ds-cancel{background:#15803d;color:white;border:0;border-radius:8px}
      #dimensionStock .ds-detail{margin-top:10px;padding:10px;border:1px solid #cbd5e1;border-radius:8px}
      #dimensionStock small{font-size:13px} #dimensionStock [hidden]{display:none!important}
      #dimensionStock #dsRecoveryItem{white-space:pre-line;overflow-wrap:anywhere}
    </style><h2>按尺寸找库存</h2><form id="dsForm">
      <div class="ds-grid"><select id="dsKind" aria-label="库存形态"><option value="board">纸板 / 半成品</option><option value="box">纸箱成品</option></select>
      <select id="dsFlute" aria-label="楞型"><option value="">全部楞型</option>${["A","B","E","AB","BE","AAA","ABC","NONE"].map(v=>`<option value="${v}">${v==="NONE"?"卡纸":v+"楞"}</option>`).join("")}</select></div>
      ${[["length","长"],["width","宽"],["height","高"]].map(([axis,label])=>`<label class="ds-axis" id="ds-${axis}-row" ${axis==="height"?"hidden":""}>${label}<select id="ds-${axis}-op" aria-label="${label}条件"><option value="ge">≥</option><option value="eq">＝</option><option value="le">≤</option></select><input id="ds-${axis}" type="text" inputmode="numeric" pattern="[0-9]{1,5}" maxlength="5" placeholder="毫米" aria-label="${label}毫米"></label>`).join("")}
      <div class="ds-actions"><button type="submit">查库存</button><button type="button" id="dsClear">清空</button></div></form>
      <p id="dsReceipt" role="status" aria-live="polite" hidden></p><button id="dsRefresh" type="button" hidden>刷新库存</button>
      <p id="dsStatus" role="status" aria-live="polite"></p><button id="dsRecover" type="button" hidden>核对上次取用</button>
      <button id="dsReloadRecovery" type="button" hidden>重新读取恢复记录</button>
      <section id="dsRecovery" class="ds-detail" hidden><p id="dsRecoveryItem" hidden></p><p id="dsRecoveryStatus" role="status" aria-live="polite"></p>
        <div class="ds-actions"><button id="dsContinue" class="ds-take" type="button" hidden>按当前账号继续这笔取用</button>
        <button id="dsRecoveryAck" type="button" hidden>已核对</button><button id="dsRecoveryStock" type="button" hidden>查看库存 / 取用记录</button></div></section>
      <div id="dsResults"></div>
      <div class="ds-actions" id="dsPager" hidden><button id="dsPrev" type="button">上一页</button><span id="dsPage"></span><button id="dsNext" type="button">下一页</button></div>
      <div id="dsDetail" class="ds-detail" hidden></div>`;
    const get = id => root.querySelector("#" + id);
    let offset = 0, items = [], criteria = null, selected = null, selectedCanExecute = false, busy = false, serial = 0, detailSerial = 0, resolveSerial = 0;
    const pendingKey = `tm-dimension-take-pending-v2:${userId}`, legacyKey = "tm-dimension-take-pending-v1";
    let pending = null, legacy = null, resolved = null, storageBlocked = false, accountChanged = false;
    const controllers = new Set();
    const instance = {ownerId: userId, live: true, destroy() {
      instance.live = false; serial++; detailSerial++; resolveSerial++;
      controllers.forEach(controller => controller.abort()); controllers.clear();
      window.TmProductDrawings?.disposeWithin(root);
      root.querySelectorAll("button,input,select").forEach(element => element.disabled = true);
    }};
    current = instance;
    const active = () => current === instance && instance.live;
    const status = text => { if (active()) get("dsStatus").textContent = text; };
    const recoveryStatus = text => { if (active()) { get("dsRecovery").hidden = false; get("dsRecoveryStatus").textContent = text; } };
    async function request(fn, url, body) {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      if (controller) controllers.add(controller);
      try { return body === undefined ? await fn(url, {signal: controller?.signal, isCurrent:active}) : await fn(url, body, {signal: controller?.signal, isCurrent:active}); }
      finally { if (controller) controllers.delete(controller); }
    }
    function updateTake() {
      if (!active() || !selected || !get("dsTake")) return;
      const restore = pending?.lotId === selected.id;
      get("dsTake").disabled = busy || storageBlocked || accountChanged || pending?.outcome === "confirmed"
        || (legacy?.lotId === selected.id && !restore) || (pending && !restore)
        || (!selectedCanExecute && !pending?.mustResolve) || (!selected.can_take && !restore);
      get("dsTake").textContent = restore ? pending.mustResolve ? "核对取用结果" : "重试取用" : "取用";
    }
    function syncRecovery() {
      if (!active()) return;
      get("dsRecover").hidden = !(pending || legacy);
      get("dsRecover").disabled = busy || accountChanged;
      get("dsReloadRecovery").hidden = !storageBlocked;
      get("dsContinue").disabled = busy || storageBlocked || accountChanged;
      get("dsRecoveryAck").disabled = busy || accountChanged;
      updateTake();
    }
    function storageFailure(message) {
      storageBlocked = true; recoveryStatus(message || "恢复记录暂无法读写，请恢复浏览器存储后重新读取"); syncRecovery();
    }
    function readRecovery() {
      if (!active() || busy) return;
      try {
        const raw = sessionStorage.getItem(pendingKey), old = sessionStorage.getItem(legacyKey);
        let own = raw ? JSON.parse(raw) : null;
        if (raw && (!own || own.version !== 2 || own.ownerId !== userId || !positive(own.lotId) || !validBody(own.body)
          || own.body.expected_actor_id !== userId || !["unknown", "confirmed"].includes(own.outcome)
          || typeof own.sent !== "boolean"
          || (own.outcome === "confirmed" && !validReceipt(own.receipt, own.body.quantity)))) throw new Error("当前账号恢复记录不完整，请保留记录并重新读取");
        const prior = old ? JSON.parse(old) : null;
        if (old && (!prior || !positive(prior.lotId) || !validBody(prior.body))) throw new Error("旧取用记录不完整，请保留记录并重新读取");
        if (pending?.outcome === "confirmed" && own?.lotId === pending.lotId && sameBody(pending.body, own.body)) own = pending;
        pending = own; legacy = prior; storageBlocked = false; resolved = null;
        get("dsContinue").hidden = true; get("dsRecoveryAck").hidden = true; get("dsRecoveryStock").hidden = true;
        get("dsRecoveryItem").hidden = true;
        if (pending?.outcome === "confirmed") showReceipt(pending.receipt);
        if (pending || legacy) recoveryStatus(pending?.outcome === "confirmed" ? "取用已完成，恢复记录尚待清理；核对不会再次扣库" : legacy && !pending ? "有一笔旧取用结果待核对；先查流水，再决定是否继续" : "上次取用结果待确认，可核对流水或同键重试");
        else get("dsRecovery").hidden = true;
      } catch (error) { storageFailure(error.message); }
      syncRecovery();
    }
    function savePending(record) {
      if (!active()) return false;
      try {
        const raw = sessionStorage.getItem(pendingKey), saved = raw ? JSON.parse(raw) : null;
        if (raw && (!saved || saved.ownerId !== userId || saved.lotId !== record.lotId || !sameBody(saved.body, record.body))) {
          storageFailure("恢复记录已变化，先核对当前账号的取用结果"); return false;
        }
        sessionStorage.setItem(pendingKey, JSON.stringify(record)); return true;
      }
      catch (_error) { storageFailure("无法保存恢复记录，本次不会再发送取用；请恢复存储后重新读取"); return false; }
    }
    function removeOwned(record) {
      if (!active()) return false;
      try {
        const raw = sessionStorage.getItem(pendingKey), saved = raw ? JSON.parse(raw) : null;
        if (raw && (!saved || saved.ownerId !== userId || saved.lotId !== record.lotId || !sameBody(saved.body, record.body))) { storageFailure("恢复记录已变化，先核对当前账号的取用结果"); return false; }
        sessionStorage.removeItem(pendingKey); return true;
      } catch (_error) { storageFailure("取用结果已保留，但恢复缓存尚未清理；可再次只读核对"); return false; }
    }
    function removeLegacy(record) {
      if (!active()) return false;
      try {
        const raw = sessionStorage.getItem(legacyKey), saved = raw ? JSON.parse(raw) : null;
        if (saved && saved.lotId === record.lotId && sameBody(saved.body, record.body)) { sessionStorage.removeItem(legacyKey); legacy = null; }
        else legacy = saved;
        return true;
      } catch (_error) { storageFailure("已查实取用结果，旧恢复缓存尚未清理；保留记录并再核对"); return false; }
    }
    function showReceipt(result) {
      if (!active()) return;
      get("dsReceipt").textContent = `${result.account ? `${result.account}已取用` : "已取用"} ${result.quantity}（流水 ${result.movement_id}）`;
      get("dsReceipt").hidden = false;
    }
    function confirmPending(record, result) {
      if (!active()) return;
      pending = {...record, outcome: "confirmed", receipt: result}; showReceipt(result);
      if (savePending(pending) && (!record.legacy || removeLegacy(record)) && removeOwned(record)) pending = null;
      resolved = null; get("dsContinue").hidden = true; get("dsRecoveryAck").hidden = true;
      if (pending) recoveryStatus("取用已完成，恢复记录尚待清理；核对不会再次扣库");
      else if (legacy) recoveryStatus("还有一笔旧取用结果待核对");
      else get("dsRecovery").hidden = true;
      syncRecovery();
    }
    const qtyText = item => `可用 ${item.available}${item.unit} · 预占 ${item.reserved}${item.unit}`;
    const close = () => { if (!active() || busy) return; window.TmProductDrawings?.disposeWithin(get("dsDetail")); selected = null; detailSerial++; get("dsDetail").hidden = true; };
    function readCriteria() {
      const params = new URLSearchParams({kind: get("dsKind").value});
      for (const axis of ["length","width",...(params.get("kind")==="box"?["height"]:[])]) {
        const value = get("ds-"+axis).value;
        if (value) {
          if (!/^[0-9]{1,5}$/.test(value) || Number(value)<1) throw new Error("尺寸请输入1至99999毫米");
          params.set(axis,value); params.set(axis+"_op",get("ds-"+axis+"-op").value);
        }
      }
      if (get("dsFlute").value) params.set("flute",get("dsFlute").value);
      if (params.size===1) throw new Error("请填写尺寸或选择楞型");
      return params;
    }
    async function search(reset = false) {
      if (!active() || busy) return false;
      const token = ++serial;
      try {
        if (reset || !criteria) { criteria = readCriteria(); offset = 0; }
        close(); status("查询中…"); window.TmProductDrawings?.disposeWithin(get("dsResults")); get("dsResults").replaceChildren(); get("dsPager").hidden=true;
        const params = new URLSearchParams(criteria); params.set("offset",offset); params.set("limit",30);
        const data = await request(apiGet, base+"?"+params);
        if (!active() || token!==serial) return false;
        items = data.items; status(`找到 ${data.total} 个库存批次 · 按尺寸接近度排列`);
        get("dsResults").innerHTML = items.map((item,index)=>`<div class="ds-result-group" data-index="${index}"><button class="ds-result" type="button" data-index="${index}"><strong>${esc(item.code || item.name)}</strong>　${esc(item.dimensions.filter(v=>v!=null).join("×"))} mm<br>${esc(item.name)} · ${esc(item.customer)}<br><small>${esc(qtyText(item))}${item.status==="frozen"?" · 冻结":""}　查看位置 ›</small></button></div>`).join("");
        get("dsResults").querySelectorAll("button").forEach(button=>button.onclick=()=>detail(items[Number(button.dataset.index)],data.can_execute));
        get("dsResults").querySelectorAll(".ds-result-group").forEach(group=>window.TmProductDrawings?.append(group,items[Number(group.dataset.index)]));
        get("dsPager").hidden=data.total<=30; get("dsPrev").disabled=offset===0; get("dsNext").disabled=offset+30>=data.total;
        get("dsPage").textContent=`${Math.floor(offset/30)+1} / ${Math.max(1,Math.ceil(data.total/30))}`;
        get("dsRefresh").hidden = true; return true;
      } catch (error) { if(active() && token===serial) status(error.message); return false; }
    }
    async function detail(item, canExecute) {
      if (!active() || busy) return;
      selected = item; selectedCanExecute = canExecute === true; const token = ++detailSerial;
      const restore = pending?.lotId===item.id;
      window.TmProductDrawings?.disposeWithin(get("dsDetail"));
      get("dsDetail").hidden=false;
      get("dsDetail").innerHTML=`<strong>${esc(item.code)} · ${esc(item.name)}</strong><p>${esc(item.location_name || "位置待核实")}</p>
        <p>${esc(qtyText(item))} · 实物 ${item.physical}${esc(item.unit)}${item.damaged?` · 损坏 ${item.damaged}${esc(item.unit)}`:""}</p>
        ${restore?"<p>有一笔取用待确认，保留原数量与用途。</p>":""}
        <div class="ds-grid"><label>取用数量（${esc(item.unit)}）<input id="dsQuantity" inputmode="numeric" type="text" maxlength="10" placeholder="数量"></label><label>用途<select id="dsPurpose"><option value="cash">现金取用</option><option value="sample">免费打样</option></select></label></div>
        <div class="ds-actions"><button class="ds-take" id="dsTake" type="button" ${!canExecute||(!item.can_take&&!restore)?"disabled":""}>${restore?"重试取用":"取用"}</button><button class="ds-cancel" id="dsCancel" type="button">取消</button></div>
        <p id="dsTakeStatus" role="status"></p><details><summary>最近取用记录</summary><div id="dsHistory">加载中…</div></details>`;
      window.TmProductDrawings?.append(get("dsDetail"), item);
      if (restore) {get("dsQuantity").value=pending.body.quantity;get("dsPurpose").value=pending.body.purpose;}
      get("dsQuantity").disabled=restore;get("dsPurpose").disabled=restore;
      get("dsCancel").onclick=close;
      get("dsQuantity").oninput=event=>event.target.value=event.target.value.replace(/[^0-9]/g,"").slice(0,10);
      get("dsTake").onclick=submit;
      updateTake();
      get("dsDetail").scrollIntoView({block:"nearest",behavior:"smooth"});
      try {
        const result=await request(apiGet, base+"/"+item.id+"/history");
        if(!active() || token!==detailSerial)return;
        get("dsHistory").innerHTML=result.items.length?result.items.map(row=>`<p><small>${esc(new Date(row.time).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}))} · ${esc(row.account)} · ${esc(row.purpose)} ${row.quantity}${esc(item.unit)}</small></p>`).join(""):"暂无记录";
      } catch(error){if(active() && token===detailSerial)get("dsHistory").textContent=error.message;}
    }
    async function submit() {
      if(!active() || busy || storageBlocked || accountChanged || !selected || pending?.outcome === "confirmed")return;
      const item=selected, raw=get("dsQuantity").value;
      if (pending?.mustResolve) { await resolveRecovery(); return; }
      if (legacy?.lotId === item.id && !pending) { await resolveRecovery(); return; }
      if(!/^[0-9]{1,10}$/.test(raw)||Number(raw)<1||Number(raw)>2147483647){get("dsTakeStatus").textContent="请输入正整数数量";return;}
      if(pending && pending.lotId!==item.id){get("dsTakeStatus").textContent="请先回到上次取用的批次确认结果";return;}
      if(!pending){
        if (!selectedCanExecute || !item.can_take) return;
        if(Number(raw)>item.available){get("dsTakeStatus").textContent="不能超过可用数量";return;}
        const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
        pending={version:2,ownerId:userId,lotId:item.id,outcome:"unknown",sent:false,body:{quantity:Number(raw),purpose:get("dsPurpose").value,
          expected_version:item.version,location_id:item.location_id,address_version:item.address_version,
          expected_actor_id:userId,
          idempotency_key:Array.from(bytes,v=>v.toString(16).padStart(2,"0")).join("")}};
      }
      await sendPending();
    }
    function identityMatches(result) {
      if (!positive(result?.current_actor_id)) throw new Error("核对回执不完整，请再次核对");
      if (result?.current_actor_id === userId) return true;
      accountChanged = true;
      recoveryStatus("登录账号已变化，原取用记录已保留；请退出后按当前账号重新登录核对");
      syncRecovery(); return false;
    }
    async function refreshAfterSuccess() {
      if (!active()) return;
      close(); items = []; window.TmProductDrawings?.disposeWithin(get("dsResults")); get("dsResults").replaceChildren(); get("dsPager").hidden = true;
      const updated = criteria ? await search() : false;
      if (!active()) return;
      status(updated ? "取用已记录，库存已更新" : "取用已记录，库存尚未刷新；刷新只查库存");
      get("dsRefresh").hidden = updated;
    }
    async function sendPending() {
      if (!active() || busy || storageBlocked || accountChanged || !pending || pending.outcome === "confirmed") return;
      const hadUnknown = pending.sent === true || pending.legacy === true;
      const record = {...pending, sent:true}; pending = record;
      if (!savePending(record)) return;
      busy = true; syncRecovery();
      if (selected && get("dsCancel")) {
        get("dsCancel").disabled = true; get("dsQuantity").disabled = true; get("dsPurpose").disabled = true;
      }
      let succeeded = false;
      try {
        const result = await request(apiPost, base+"/"+record.lotId+"/take", record.body);
        if (!active()) return;
        // Take authenticates expected_actor_id; the receipt must describe this exact quantity.
        if (!validReceipt(result, record.body.quantity)) throw new Error("服务返回的取用回执不完整，结果待核对");
        confirmPending(record, result); succeeded = true;
      } catch (error) {
        if (!active()) return;
        if (error.actorMismatch) accountChanged = true;
        const rejected = error.requestRejected || [400,403,404,422].includes(error.status);
        if (rejected && !hadUnknown && !error.preservePending && !error.actorMismatch) {
          if (removeOwned(record)) pending = null;
        } else {
          pending = {...record, mustResolve: Boolean(rejected || error.preservePending || error.actorMismatch)};
          savePending(pending);
        }
        const text = error.message + (pending ? "；原请求已保留，可核对取用结果" : "；请重新查询库存");
        recoveryStatus(text);
        if (selected && get("dsTakeStatus")) get("dsTakeStatus").textContent = text;
      } finally {
        if (active()) { busy = false; if (selected && get("dsCancel")) get("dsCancel").disabled = false; syncRecovery(); }
      }
      if (succeeded && active()) await refreshAfterSuccess();
    }
    async function resolveRecovery() {
      if (!active() || busy || accountChanged) return;
      const record = pending || legacy, source = pending ? "pending" : "legacy";
      if (!record) return;
      const token = ++resolveSerial; busy = true; resolved = null;
      get("dsContinue").hidden = true; get("dsRecoveryAck").hidden = true;
      get("dsRecoveryItem").hidden = true;
      recoveryStatus("正在核对这笔取用流水…"); syncRecovery();
      try {
        const result = await request(apiPost, base+"/"+record.lotId+"/resolve", {...record.body, expected_actor_id:userId});
        if (!active() || token !== resolveSerial || !identityMatches(result)) return;
        if (result.status === "completed" && validReceipt(result, record.body.quantity) && result.replayed === true
          && positive(result.actor_id) && typeof result.account === "string" && typeof result.observed_at === "string") {
          showReceipt(result);
          if (source === "pending") confirmPending(record, result);
          else get("dsRecoveryAck").hidden = false;
          resolved = {record, source, result};
          recoveryStatus(`已查实：${result.account}取用 ${result.quantity}，流水 ${result.movement_id}。核对不会再次扣库`);
        } else if (result.status === "not_recorded" && typeof result.can_continue === "boolean"
          && (result.continue_reason === null || typeof result.continue_reason === "string") && typeof result.observed_at === "string") {
          if (result.can_continue) {
            const data = await request(apiGet, base+"/"+record.lotId+"/detail"), item = data?.item;
            if (!active() || token !== resolveSerial) return;
            if (!item || item.id !== record.lotId || typeof item.code !== "string" || !item.code.trim()
              || typeof item.name !== "string" || !item.name.trim() || typeof item.unit !== "string" || !item.unit.trim()
              || typeof item.location_name !== "string" || !item.location_name.trim()) throw new Error("产品与货位资料不完整，请再次核对");
            get("dsRecoveryItem").textContent = `${item.code} · ${item.name}\n${item.location_name}\n原取用 ${record.body.quantity}${item.unit} · ${record.body.purpose === "sample" ? "免费打样" : "现金取用"}`;
            get("dsRecoveryItem").hidden = false;
            if (item.version !== record.body.expected_version || item.location_id !== record.body.location_id
              || item.address_version !== record.body.address_version || !item.can_take || item.available < record.body.quantity) {
              recoveryStatus("当前位置只供核对，库存或位置已变化；原记录保留，请再次核对");
              get("dsRecoveryStock").hidden = false; return;
            }
          }
          resolved = {record, source, result};
          recoveryStatus(result.can_continue ? `本次核对未见流水。确认继续将以${displayName}执行以上取用，仍使用原请求` : `本次核对未见流水，暂不能继续：${result.continue_reason || "请核对库存及权限"}。原记录保留，可查看库存 / 取用记录，并请管理员核对`);
          get("dsContinue").hidden = !result.can_continue;
        } else throw new Error("核对回执不完整，原记录保留，请再次核对");
        get("dsRecoveryStock").hidden = false;
      } catch (error) {
        if (active() && token === resolveSerial) { if (error.actorMismatch) accountChanged = true; recoveryStatus(error.message + "；原记录保留，可再次核对"); }
      } finally { if (active() && token === resolveSerial) { busy = false; syncRecovery(); } }
    }
    async function continueResolved() {
      if (!active() || busy || storageBlocked || accountChanged || !resolved || resolved.result.status !== "not_recorded" || !resolved.result.can_continue) return;
      const {record, source} = resolved;
      if (source === "legacy" && pending) { recoveryStatus("请先核对当前账号的另一笔取用"); return; }
      pending = source === "legacy" ? {version:2, ownerId:userId, lotId:record.lotId, body:{...record.body, expected_actor_id:userId}, outcome:"unknown", sent:true, legacy:true}
        : {...record, mustResolve:false};
      resolved = null; get("dsContinue").hidden = true;
      await sendPending();
    }
    for(const axis of ["length","width","height"])get("ds-"+axis).oninput=event=>{event.target.value=digits(event.target.value);};
    get("dsKind").onchange=()=>{get("ds-height-row").hidden=get("dsKind").value!=="box";};
    get("dsForm").onsubmit=event=>{event.preventDefault();search(true);};
    get("dsPrev").onclick=()=>{if(!busy){offset=Math.max(0,offset-30);search();}};
    get("dsNext").onclick=()=>{if(!busy){offset+=30;search();}};
    get("dsClear").onclick=()=>{if(!active() || busy)return;serial++;criteria=null;offset=0;get("dsForm").reset();get("ds-height-row").hidden=true;window.TmProductDrawings?.disposeWithin(get("dsResults"));get("dsResults").replaceChildren();get("dsPager").hidden=true;close();status("");};
    get("dsRecover").onclick=resolveRecovery;
    get("dsContinue").onclick=continueResolved;
    get("dsReloadRecovery").onclick=readRecovery;
    get("dsRefresh").onclick=()=>search(!criteria);
    get("dsRecoveryAck").onclick=()=>{
      if (!active() || busy || accountChanged || resolved?.source !== "legacy" || resolved.result.status !== "completed") return;
      if (removeLegacy(resolved.record)) { resolved = null; get("dsRecoveryAck").hidden = true; recoveryStatus("已核对并清理这笔旧恢复记录"); }
      syncRecovery();
    };
    get("dsRecoveryStock").onclick=async()=>{
      if (!active() || busy || accountChanged) return;
      const record = resolved?.record || pending || legacy; if (!record) return;
      try { const data = await request(apiGet, base+"/"+record.lotId+"/detail"); if (active()) await detail(data.item, data.can_execute); }
      catch (error) { if (active()) recoveryStatus(error.message); }
    };
    readRecovery();
  }
  window.TmDimensionStock={mount,destroy,digits};
})();
