/* Uses the existing mobile stocktake session, formal product master and ledger. */
const inbound = { generation: 0, context: null, attempt: null, busy: false, type: "finished", stockLot: null };
function inboundAttemptStorageKey() { return `erp-initial-inbound:${state.user?.id}`; }
function persistInboundAttempt() {
  try {
    if (inbound.attempt) sessionStorage.setItem(inboundAttemptStorageKey(), JSON.stringify(inbound.attempt));
    else sessionStorage.removeItem(inboundAttemptStorageKey());
  } catch { /* The in-page retry key remains available when storage is disabled. */ }
}

function resetInitialInbound() {
  inbound.type = "finished";
  $("inboundCandidates").replaceChildren();
  $("inboundSave").textContent = "保存入库";
  inbound.unmountSheet?.();inbound.unmountSheet=null;
  $("sheetGoods").replaceChildren();
  $("sheetGoods").classList.add("hidden");
  $("finishedGoods").classList.remove("hidden");
  $("lotList").classList.remove("hidden");
  document.querySelectorAll("[data-goods-type]").forEach(b=>b.classList.toggle("primary",b.dataset.goodsType==="finished"));
  inbound.generation += 1;
  inbound.context = null;
  inbound.stockLot = null;
  inbound.attempt = null;
  $("initialInbound").classList.add("hidden");
  $("inboundCustomerQuery").value = "";
  $("inboundCustomer").innerHTML = '<option value="all">全部客户（权限范围内）</option>';
  $("inboundQuantity").value = "";
  $("inboundProduct").value = "";
  $("inboundExistingAcknowledged").checked = false;
  $("inboundResult").textContent = "";
  $("inboundSave").disabled = true;
}

function renderInitialInbound() {
  if(typeof embeddedStocktake!=="undefined"&&embeddedStocktake){$("initialInbound").classList.add("hidden");return;}
  const enabled = state.user?.role === "admin" && state.selectedLocation && !state.locked;
  $("initialInbound").classList.toggle("hidden", !enabled);
  if (!enabled) return;
  const selectedProduct = new URLSearchParams(window.location.search);
  if (!inbound.prefilled && selectedProduct.get("product_id") && selectedProduct.get("customer_id")) {
    inbound.prefilled = true;
    $("inboundCustomer").innerHTML = `<option value="${Number(selectedProduct.get("customer_id"))}">${h(selectedProduct.get("customer_name"))}</option>`;
    $("inboundProduct").innerHTML = `<option value="${Number(selectedProduct.get("product_id"))}">${h(selectedProduct.get("product_code"))} · ${h(selectedProduct.get("product_name"))}</option>`;
    $("inboundQuantity").value = selectedProduct.get("counted_quantity") || "";
  }
  $("inboundFields").disabled = false;
  $("inboundRefresh").disabled = false;
  $("inboundContext").textContent = "仅添加未登记实物；已有库存请盘点或移货。";
  try {
    const saved = JSON.parse(sessionStorage.getItem(inboundAttemptStorageKey()) || "null");
    if (saved?.items?.[0]?.location_id === Number(pick(state.selectedLocation, ["id", "location_id"]))) {
      inbound.attempt = saved;
      $("inboundResult").textContent = "上次入库结果尚未确认，请点击保存入库核对同一笔，系统不会重复记账。";
      setInboundBusy(false);
      return;
    }
  } catch { /* Invalid local state never changes the ledger. */ }
  if ($("inboundProduct").value) refreshInboundContext();
}

function setInboundBusy(value) {
  inbound.busy = value;
  state.submitting = value;
  $("inboundFields").disabled = value || Boolean(inbound.attempt);
  document.querySelectorAll("[data-goods-type],#inboundNotListed").forEach(b=>b.disabled=value||Boolean(inbound.attempt));
  $("inboundSave").disabled = value || (!inbound.context && !inbound.attempt);
  $("inboundRefresh").disabled = value || Boolean(inbound.attempt);
  $("inboundCancel").disabled = value || Boolean(inbound.attempt);
  ["backToLocations", "openCurrentMap", "fillAllButton"].forEach(id => $(id).disabled = value || Boolean(inbound.attempt));
  updateSubmitState();
  if (inbound.attempt) $("submitButton").disabled = true;
}

function invalidateInboundSelection() {
  inbound.generation += 1;
  inbound.context = null;
  inbound.stockLot = null;
  $("inboundCandidates").replaceChildren();
  $("inboundSave").disabled = true;
  $("inboundExistingAcknowledged").checked = false;
  $("inboundExistingLabel").classList.add("hidden");
  updateSubmitState();
}

async function findInboundCustomers() {
  invalidateInboundSelection();
  const keyword = $("inboundCustomerQuery").value.trim();
  $("inboundCustomer").innerHTML = (keyword ? '<option value="">请选择客户</option>' : "") + '<option value="all">全部客户（权限范围内）</option>';
  $("inboundProduct").innerHTML = '<option value="">请先选客户</option>';
  const generation = inbound.generation;
  try {
    const params = new URLSearchParams({keyword: $("inboundCustomerQuery").value.trim(), page: "1", page_size: "50"});
    const data = await api(`/api/master/customers?${params}`);
    if (generation !== inbound.generation) return;
    $("inboundCustomer").innerHTML += (data.items || []).map(row => `<option value="${Number(row.id)}">${h(row.chinese_short_name || row.name)} · ${h(row.name)}</option>`).join("");
    $("inboundContext").textContent = "请选择客户（最多50条）";
  } catch (error) { if (generation === inbound.generation) showMessage(error.message); }
}

async function findErpProducts() {
  invalidateInboundSelection();
  $("inboundProduct").innerHTML = '<option value="">请选择产品</option>';
  const customer = $("inboundCustomer").value;
  if (!customer) { showMessage("请先选择客户"); return; }
  const generation = inbound.generation;
  try {
    const params = new URLSearchParams({q: $("inboundProductQuery").value.trim(), limit: "10"});
    if (customer !== "all") params.set("customer_id", customer);
    else if (!params.get("q")) { showMessage("全部客户请输入规格、编码或名称"); return; }
    const data = await api(`/api/warehouse/floor3/product-candidates?${params}`);
    if (generation !== inbound.generation || customer !== $("inboundCustomer").value) return;
    $("inboundProduct").innerHTML += (data.items || []).map(row => `<option data-customer-id="${Number(row.customer_id)}" data-customer-name="${h(row.customer_short_name || row.customer_name)}" value="${Number(row.product_id)}">${h(row.customer_short_name || row.customer_name)} · ${row.match_score != null ? `接近度${row.match_score}% · ` : ""}${h(row.product_code || row.customer_material_code)} · ${h(row.product_name)} · ${h(row.specification||"")}</option>`).join("");
    $("inboundCandidates").replaceChildren();
    $("inboundContext").textContent = data.items?.length ? "请选择ERP产品（前10条）" : "未找到匹配的ERP产品，请调整关键词";
  } catch (error) { if (generation === inbound.generation) showMessage(error.message); }
}

async function refreshInboundContext() {
  if(inbound.stockLot && !inbound.busy && !inbound.attempt){await findInboundProducts();return;}
  if (inbound.busy || inbound.attempt) return;
  invalidateInboundSelection();
  const product = $("inboundProduct").value;
  const location = pick(state.selectedLocation, ["id", "location_id"]);
  if (!product || !location) return;
  const generation = inbound.generation;
  $("inboundContext").textContent = "正在核对系统现存数量…";
  try {
    const params = new URLSearchParams({location_id: String(location), product_id: product});
    const data = await api(`/api/warehouse/twin-operations/initial-stock-context?${params}`);
    if (generation !== inbound.generation) return;
    inbound.context = data;
    refreshInboundEntryCost();
    $("inboundContext").textContent = data.can_add
      ? `已登记 ${data.existing_quantity} 只 · ${data.existing_location_count} 个货位`
      : data.block_reason;
    $("inboundExistingLabel").classList.toggle("hidden", !data.existing_quantity);
    $("inboundSave").disabled = !data.can_add;
  } catch (error) { if (generation === inbound.generation) $("inboundContext").textContent = error.message; }
}

async function createInboundProduct() {
  if (inbound.busy || inbound.attempt) return;
  const customer = Number($("inboundCustomer").value);
  const code = $("inboundCode").value.trim(), name = $("inboundName").value.trim();
  if (!customer || !code || !name) { showMessage("请先选客户，并填写实际存货编码和产品名称"); return; }
  const payload = {customer_id: customer, product_code: code, customer_material_code: code, product_name: name, box_category: $("inboundCategory").value};
  for (const [id, key] of [["inboundLength", "length_mm"], ["inboundWidth", "width_mm"], ["inboundHeight", "height_mm"]]) {
    if ($(id).value) {
      const value = Number($(id).value);
      if (!Number.isInteger(value) || value <= 0) { showMessage("尺寸必须是正整数毫米，未知可留空"); return; }
      payload[key] = value;
    }
  }
  invalidateInboundSelection();
  setInboundBusy(true);
  try {
    const product = await api("/api/products/stocktake-create", {method: "POST", body: JSON.stringify(payload)});
    $("inboundProduct").innerHTML = `<option value="${Number(product.id)}">${h(code)} · ${h(name)}</option>`;
    $("inboundNewProduct").open = false;
    $("inboundProductQuery").value = code;
    showMessage("产品已保存，请补齐材质、单位及报料资料后继续入库", "success");
  } catch (error) { showMessage(`${error.message}。若网络中断，请先搜索此编码核对是否已建成。`); }
  finally { setInboundBusy(false); }
  await refreshInboundContext();
  if(Number($("inboundProduct").value))$("inboundEditProduct").click();
}

async function saveInitialInbound() {
  if (inbound.busy || state.locked) return;
  if (!inbound.attempt && inbound.stockLot) {
    const lot=inbound.stockLot, source=lot.registered_location, target=state.selectedLocation;
    const quantity=Number($("inboundQuantity").value);
    if(!Number.isSafeInteger(quantity)||quantity<=0||quantity>lot.quantity_movable){showMessage("请输入不超过该批可搬数量的正整数");return;}
    const targetId=Number(pick(target,["id","location_id"]));
    if(source.location_id===targetId){showMessage("该批已经在当前货位，请在上方核对实盘数量");return;}
    const key=idempotencyKey();
    const payload=source.is_pending_relocation ? {
      location_id:targetId,expected_layout_version:target.layout_version,expected_address_version:target.address_version,
      expected_map_revision:target.published_map_revision,expected_version:lot.lot_version,quantity,idempotency_key:key,confirmed:true
    } : {
      expected_version:lot.lot_version,quantity,expected_source_location_id:source.location_id,
      expected_source_address_version:source.address_version,expected_source_layout_version:source.layout_version,
      expected_source_map_revision:source.published_map_revision,target_location_id:targetId,
      expected_target_layout_version:target.layout_version,expected_target_address_version:target.address_version,
      expected_target_map_revision:target.published_map_revision,idempotency_key:key,physical_move_confirmed:true
    };
    inbound.attempt={items:[{location_id:targetId}],move_path:source.is_pending_relocation?`/api/warehouse/twin-operations/pending-lots/${lot.lot_id}/place`:`/api/mobile/erp/warehouse/lots/${lot.lot_id}/moves`,move_payload:payload};
    persistInboundAttempt();
  }
  if (!inbound.attempt) {
    const quantity = Number($("inboundQuantity").value);
    if (!inbound.context?.can_add || !Number.isSafeInteger(quantity) || quantity <= 0 || !$("inboundDate").value) {
      showMessage("请核对系统库存，并填写正整数实际数量和入库日期"); return;
    }
    const acknowledged = $("inboundExistingAcknowledged").checked;
    if (inbound.context.existing_quantity && !acknowledged) { showMessage("请先核对已登记库存，避免把需要移货的货物重复入库"); return; }
    inbound.attempt = {
      idempotency_key: idempotencyKey(), confirmed: true,
      initial_inventory_snapshot: inbound.context.snapshot,
      existing_inventory_acknowledged: acknowledged,
      items: [{client_item_id: "mobile-initial", operation: "add", inventory_type: "finished", unit: "boxes",
        location_id: Number(pick(state.selectedLocation, ["id", "location_id"])),
        expected_layout_version: Number(state.selectedLocation.layout_version),
        customer_id: Number($("inboundCustomer").value), product_id: Number($("inboundProduct").value),
        quantity, stock_date: $("inboundDate").value, source_kind: "existing_stocktake",
        stock_stage: $("inboundStage")?.value || "complete"}],
    };
    persistInboundAttempt();
  }
  const locationId = inbound.attempt.items[0].location_id;
  setInboundBusy(true);
  try {
    await api(inbound.attempt.move_path || "/api/warehouse/twin-operations/stocktake-batches", {method: "POST", body: JSON.stringify(inbound.attempt.move_payload || inbound.attempt)});
    inbound.attempt = null;
    persistInboundAttempt();
    setInboundBusy(false);
    await openLocation(locationId);
    showMessage("入库已保存，已重新读取当前货位。可点“换库位”继续盘点。", "success");
  } catch (error) {
    const uncertain = !error.status || error.status >= 500;
    if (!uncertain) { inbound.attempt = null; persistInboundAttempt(); }
    $("inboundResult").textContent = uncertain
      ? "保存结果暂未确认。请点击保存入库重试同一笔，系统会防止重复记账；暂勿关闭页面。"
      : error.message;
    setInboundBusy(false);
    if (!uncertain) await refreshInboundContext();
  }
}

$("inboundDate").value = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
$("inboundCustomerQuery").oninput = () => {
  invalidateInboundSelection();
  $("inboundCustomer").innerHTML = $("inboundCustomerQuery").value.trim()
    ? '<option value="">搜索后选择客户</option>' : '<option value="all">全部客户（权限范围内）</option>';
};
$("inboundFindCustomer").onclick = findInboundCustomers;
$("inboundFindProduct").onclick = findInboundProducts;
$("inboundCustomer").onchange = () => { invalidateInboundSelection(); $("inboundProduct").innerHTML = '<option value="">请选择产品</option>'; findInboundProducts(); };
$("inboundProduct").onchange = () => {
  if ($("inboundStage")) $("inboundStage").value = "complete";
  const option = $("inboundProduct").selectedOptions[0];
  if (option?.dataset.customerId && $("inboundCustomer").value !== option.dataset.customerId) {
    const customer = document.createElement("option");
    customer.value = option.dataset.customerId;
    customer.textContent = option.dataset.customerName;
    $("inboundCustomer").append(customer);$("inboundCustomer").value = customer.value;
  }
  refreshInboundContext();
};
$("inboundRefresh").onclick = refreshInboundContext;
$("inboundCreateProduct").onclick = createInboundProduct;
$("inboundSave").onclick = saveInitialInbound;
$("inboundQuantity").oninput = updateSubmitState;
$("inboundCancel").onclick = () => {
  if (inbound.busy || inbound.attempt) return;
  resetInitialInbound();
  renderInitialInbound();
  updateSubmitState();
};
renderInitialInbound();

async function findInboundProducts(){
  if(inbound.busy||inbound.attempt)return;
  invalidateInboundSelection();inbound.stockLot=null;
  $("inboundProduct").innerHTML='<option value="">从下方选择库存；未找到可查ERP产品</option>';
  $("inboundSave").textContent="保存入库";
  const customer=$("inboundCustomer").value;if(!customer){showMessage("请先选择客户");return;}
  const generation=inbound.generation;
  $("inboundContext").textContent="正在查找库存…";
  try{
    const params=new URLSearchParams({inventory_keyword:$("inboundProductQuery").value.trim(),limit:"30"});
    if(customer!=="all")params.set("customer_id",customer);
    else if(!params.get("inventory_keyword")){showMessage("全部客户请输入规格、编码或名称");return;}
    const data=await api(`/api/mobile/erp/warehouse/physical-inventory/search?${params}`);
    if(generation!==inbound.generation)return;
    $("inboundCandidates").replaceChildren();
    for(const lot of data.items||[]){
      const b=document.createElement("button");b.type="button";b.className="btn";
      const loc=lot.registered_location;
      b.innerHTML=`${h(lot.product_code)} · ${h(lot.product_name)}<small>${h(lot.customer_short_name || lot.customer_name || "")} · ${h(lot.specification)} · ${h(lot.quantity_total)}只 · ${h(loc.is_pending_relocation?"未归位":loc.employee_location_name)}</small>`;
      b.disabled=!lot.can_move;
      b.onclick=()=>{if(inbound.busy||inbound.attempt||generation!==inbound.generation)return;inbound.stockLot=lot;inbound.context={can_add:true};$("inboundQuantity").value=lot.quantity_movable;$("inboundSave").disabled=false;$("inboundSave").textContent=loc.is_pending_relocation?"确认归位":"确认移到此位";$("inboundContext").textContent=`已选 ${lot.product_code} · ${lot.quantity_movable}只，登记到当前货位`;$("inboundExistingLabel").classList.add("hidden");updateSubmitState();};
      $("inboundCandidates").append(b);
    }
    $("inboundContext").textContent=data.items?.length?"先列未归位库存，再列已有货位库存":"无匹配库存，点击未在列表查找产品";
    if(customer==="all"&&!data.items?.length)await findErpProducts();
  }catch(e){if(generation===inbound.generation)$("inboundContext").textContent=e.message;}
}
$("inboundNotListed").onclick=()=>{if(inbound.busy||inbound.attempt)return;inbound.stockLot=null;$("inboundSave").textContent="保存入库";findErpProducts();};
window.mobileGoodsConfig=()=>({locationId:Number(pick(state.selectedLocation,["id","location_id"])),layoutVersion:state.selectedLocation.layout_version,raw:inbound.type==="raw",canSave:state.user?.role==="admin"&&!state.locked});
window.mobileGoodsBusy=value=>{setInboundBusy(value)};
window.mobileGoodsSaved=async()=>{setInboundBusy(false);await openLocation(pick(state.selectedLocation,["id","location_id"]));showMessage("货物已入库","success");};
document.querySelectorAll("[data-goods-type]").forEach(button=>button.onclick=()=>{
  if(inbound.busy||inbound.attempt)return;
  inbound.type=button.dataset.goodsType;
  $("lotList").classList.toggle("hidden",inbound.type!=="finished");
  document.querySelectorAll("[data-goods-type]").forEach(b=>b.classList.toggle("primary",b===button));
  $("finishedGoods").classList.toggle("hidden",inbound.type!=="finished");
  $("sheetGoods").classList.toggle("hidden",inbound.type==="finished");
  inbound.unmountSheet?.();inbound.unmountSheet=null;
  $("sheetGoods").replaceChildren();
  if(inbound.type!=="finished"){
    if(!window.mountMobileGoods){$("sheetGoods").textContent="表单正在加载，请稍后重新选择";return;}
    inbound.unmountSheet=window.mountMobileGoods($("sheetGoods"),{...window.mobileGoodsConfig(),unassigned:inbound.type==="unassigned",onBusyChange:window.mobileGoodsBusy,onSaved:window.mobileGoodsSaved});
  }
  updateSubmitState();
});

async function refreshInboundEntryCost(){
  const id=Number($("inboundProduct").value), generation=inbound.generation;
  $("inboundEntryCost").textContent='';
  if(!id||inbound.stockLot||state.user?.role!=='admin')return;
  try{const text=await TMEntryProduct.preview(id,$("inboundStage").value);if(generation===inbound.generation&&id===Number($("inboundProduct").value))$("inboundEntryCost").textContent=text;}catch(error){if(generation===inbound.generation)$("inboundEntryCost").textContent='成本预览：'+error.message;}
}
$("inboundEditProduct").onclick=async()=>{
  if(inbound.busy||inbound.attempt||state.locked||state.user?.role!=='admin')return;
  if(inbound.stockLot){showMessage('已有库存沿用入库时的资料及成本；本入口用于新实物入仓。');return;}
  const productId=Number($("inboundProduct").value);
  try{const saved=await TMEntryProduct.open({productId,stockStage:$("inboundStage").value});if(saved&&productId===Number($("inboundProduct").value)){await refreshInboundContext();}}catch(error){showMessage(error.message);}
};
$("inboundStage").addEventListener('change',refreshInboundEntryCost);
