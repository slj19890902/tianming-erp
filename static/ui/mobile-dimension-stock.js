(() => {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const digits = value => String(value).replace(/[^0-9]/g, "").slice(0, 5);
  const base = "/api/mobile/erp/warehouse/dimension-stock";
  function mount({apiGet, apiPost}) {
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
    </style><h2>按尺寸找库存</h2><form id="dsForm">
      <div class="ds-grid"><select id="dsKind" aria-label="库存形态"><option value="board">纸板 / 半成品</option><option value="box">纸箱成品</option></select>
      <select id="dsFlute" aria-label="楞型"><option value="">全部楞型</option>${["A","B","E","AB","BE","AAA","ABC","NONE"].map(v=>`<option value="${v}">${v==="NONE"?"卡纸":v+"楞"}</option>`).join("")}</select></div>
      ${[["length","长"],["width","宽"],["height","高"]].map(([axis,label])=>`<label class="ds-axis" id="ds-${axis}-row" ${axis==="height"?"hidden":""}>${label}<select id="ds-${axis}-op" aria-label="${label}条件"><option value="ge">≥</option><option value="eq">＝</option><option value="le">≤</option></select><input id="ds-${axis}" type="text" inputmode="numeric" pattern="[0-9]{1,5}" maxlength="5" placeholder="毫米" aria-label="${label}毫米"></label>`).join("")}
      <div class="ds-actions"><button type="submit">查库存</button><button type="button" id="dsClear">清空</button></div></form>
      <p id="dsStatus" role="status" aria-live="polite"></p><button id="dsRecover" type="button" hidden>查看上次取用</button><div id="dsResults"></div>
      <div class="ds-actions" id="dsPager" hidden><button id="dsPrev" type="button">上一页</button><span id="dsPage"></span><button id="dsNext" type="button">下一页</button></div>
      <div id="dsDetail" class="ds-detail" hidden></div>`;
    const get = id => root.querySelector("#" + id);
    let offset = 0, items = [], criteria = null, selected = null, busy = false, serial = 0, detailSerial = 0;
    const pendingKey = "tm-dimension-take-pending-v1";
    let pending = null;
    try { pending = JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch (_) {}
    const status = text => { get("dsStatus").textContent = text; };
    const qtyText = item => `可用 ${item.available}${item.unit} · 预占 ${item.reserved}${item.unit}`;
    const close = () => { if (busy) return; window.TmProductDrawings?.disposeWithin(get("dsDetail")); selected = null; detailSerial++; get("dsDetail").hidden = true; };
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
      if (busy) return;
      const token = ++serial;
      try {
        if (reset || !criteria) { criteria = readCriteria(); offset = 0; }
        close(); status("查询中…"); window.TmProductDrawings?.disposeWithin(get("dsResults")); get("dsResults").replaceChildren(); get("dsPager").hidden=true;
        const params = new URLSearchParams(criteria); params.set("offset",offset); params.set("limit",30);
        const data = await apiGet(base+"?"+params);
        if (token!==serial) return;
        items = data.items; status(`找到 ${data.total} 个库存批次 · 按尺寸接近度排列`);
        get("dsResults").innerHTML = items.map((item,index)=>`<div class="ds-result-group" data-index="${index}"><button class="ds-result" type="button" data-index="${index}"><strong>${esc(item.code || item.name)}</strong>　${esc(item.dimensions.filter(v=>v!=null).join("×"))} mm<br>${esc(item.name)} · ${esc(item.customer)}<br><small>${esc(qtyText(item))}${item.status==="frozen"?" · 冻结":""}　查看位置 ›</small></button></div>`).join("");
        get("dsResults").querySelectorAll("button").forEach(button=>button.onclick=()=>detail(items[Number(button.dataset.index)],data.can_execute));
        get("dsResults").querySelectorAll(".ds-result-group").forEach(group=>window.TmProductDrawings?.append(group,items[Number(group.dataset.index)]));
        get("dsPager").hidden=data.total<=30; get("dsPrev").disabled=offset===0; get("dsNext").disabled=offset+30>=data.total;
        get("dsPage").textContent=`${Math.floor(offset/30)+1} / ${Math.max(1,Math.ceil(data.total/30))}`;
      } catch (error) { if(token===serial) status(error.message); }
    }
    async function detail(item, canExecute) {
      if (busy) return;
      selected = item; const token = ++detailSerial;
      const restore = pending?.lotId===item.id;
      window.TmProductDrawings?.disposeWithin(get("dsDetail"));
      get("dsDetail").hidden=false;
      get("dsDetail").innerHTML=`<strong>${esc(item.code)} · ${esc(item.name)}</strong><p>${esc(item.location_name || "位置待核实")}</p>
        <p>${esc(qtyText(item))} · 实物 ${item.physical}${esc(item.unit)}${item.damaged?` · 损坏 ${item.damaged}${esc(item.unit)}`:""}</p>
        ${restore?"<p>有一笔待确认请求；重试不会重复扣库。</p>":""}
        <div class="ds-grid"><label>取用数量（${esc(item.unit)}）<input id="dsQuantity" inputmode="numeric" type="text" maxlength="10" placeholder="数量"></label><label>用途<select id="dsPurpose"><option value="cash">现金取用</option><option value="sample">免费打样</option></select></label></div>
        <div class="ds-actions"><button class="ds-take" id="dsTake" type="button" ${!canExecute||(!item.can_take&&!restore)?"disabled":""}>${restore?"重试取用":"取用"}</button><button class="ds-cancel" id="dsCancel" type="button">取消</button></div>
        <p id="dsTakeStatus" role="status"></p><details><summary>最近取用记录</summary><div id="dsHistory">加载中…</div></details>`;
      window.TmProductDrawings?.append(get("dsDetail"), item);
      if (restore) {get("dsQuantity").value=pending.body.quantity;get("dsPurpose").value=pending.body.purpose;}
      get("dsQuantity").disabled=restore;get("dsPurpose").disabled=restore;
      get("dsCancel").onclick=close;
      get("dsQuantity").oninput=event=>event.target.value=event.target.value.replace(/[^0-9]/g,"").slice(0,10);
      get("dsTake").onclick=submit;
      get("dsDetail").scrollIntoView({block:"nearest",behavior:"smooth"});
      try {
        const result=await apiGet(base+"/"+item.id+"/history");
        if(token!==detailSerial)return;
        get("dsHistory").innerHTML=result.items.length?result.items.map(row=>`<p><small>${esc(new Date(row.time).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}))} · ${esc(row.account)} · ${esc(row.purpose)} ${row.quantity}${esc(item.unit)}</small></p>`).join(""):"暂无记录";
      } catch(error){if(token===detailSerial)get("dsHistory").textContent=error.message;}
    }
    async function submit() {
      if(busy||!selected)return;
      const item=selected, raw=get("dsQuantity").value;
      if(!/^[0-9]{1,10}$/.test(raw)||Number(raw)<1||Number(raw)>2147483647){get("dsTakeStatus").textContent="请输入正整数数量";return;}
      if(pending && pending.lotId!==item.id){get("dsTakeStatus").textContent="请先回到上次取用的批次确认结果";return;}
      if(!pending){
        if(Number(raw)>item.available){get("dsTakeStatus").textContent="不能超过可用数量";return;}
        const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
        pending={lotId:item.id,body:{quantity:Number(raw),purpose:get("dsPurpose").value,
          expected_version:item.version,location_id:item.location_id,address_version:item.address_version,
          idempotency_key:Array.from(bytes,v=>v.toString(16).padStart(2,"0")).join("")}};
        try{sessionStorage.setItem(pendingKey,JSON.stringify(pending));}catch(_){pending=null;get("dsTakeStatus").textContent="浏览器无法保存请求，请允许网站存储后重试";return;}
      }
      busy=true; get("dsTake").disabled=true;get("dsCancel").disabled=true;get("dsQuantity").disabled=true;get("dsPurpose").disabled=true;
      let succeeded=false;
      try{
        const result=await apiPost(base+"/"+item.id+"/take",pending.body);
        pending=null;sessionStorage.removeItem(pendingKey);get("dsRecover").hidden=true;succeeded=true;
        status(`已记录取用 ${result.quantity}${item.unit} · 流水 ${result.movement_id}`);
      }catch(error){
        if(error.requestRejected || [400,403,404,422].includes(error.status)){pending=null;sessionStorage.removeItem(pendingKey);}
        get("dsRecover").hidden=!pending;
        get("dsTakeStatus").textContent=error.message+(pending?"；可点重试确认结果":"；请重新查询");
      }finally{busy=false;get("dsCancel").disabled=false;get("dsTake").disabled=!pending;get("dsTake").textContent=pending?"重试取用":"取用";}
      if(succeeded){close();await search();status("取用已记录，库存已更新");}
    }
    for(const axis of ["length","width","height"])get("ds-"+axis).oninput=event=>{event.target.value=digits(event.target.value);};
    get("dsKind").onchange=()=>{get("ds-height-row").hidden=get("dsKind").value!=="box";};
    get("dsForm").onsubmit=event=>{event.preventDefault();search(true);};
    get("dsPrev").onclick=()=>{if(!busy){offset=Math.max(0,offset-30);search();}};
    get("dsNext").onclick=()=>{if(!busy){offset+=30;search();}};
    get("dsClear").onclick=()=>{if(busy)return;serial++;criteria=null;offset=0;get("dsForm").reset();get("ds-height-row").hidden=true;window.TmProductDrawings?.disposeWithin(get("dsResults"));get("dsResults").replaceChildren();get("dsPager").hidden=true;close();status("");};
    get("dsRecover").onclick=async()=>{
      if(busy||!pending)return;
      try{const data=await apiGet(base+"/"+pending.lotId+"/detail");await detail(data.item,data.can_execute);}
      catch(error){status(error.message);}
    };
    if(pending){get("dsRecover").hidden=false;status("上次取用结果待确认，可查看并重试。");}
  }
  window.TmDimensionStock={mount,digits};
})();
