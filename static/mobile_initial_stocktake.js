/* Uses the existing mobile stocktake session, formal product master and ledger. */
const inbound = { generation: 0, context: null, attempt: null, busy: false };
function inboundAttemptStorageKey() { return `erp-initial-inbound:${state.user?.id}`; }
function persistInboundAttempt() {
  try {
    if (inbound.attempt) sessionStorage.setItem(inboundAttemptStorageKey(), JSON.stringify(inbound.attempt));
    else sessionStorage.removeItem(inboundAttemptStorageKey());
  } catch { /* The in-page retry key remains available when storage is disabled. */ }
}

function resetInitialInbound() {
  inbound.generation += 1;
  inbound.context = null;
  inbound.attempt = null;
  $("initialInbound").classList.add("hidden");
  $("inboundQuantity").value = "";
  $("inboundProduct").value = "";
  $("inboundExistingAcknowledged").checked = false;
  $("inboundResult").textContent = "";
  $("inboundSave").disabled = true;
}

function renderInitialInbound() {
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
  $("inboundContext").textContent = "可在同一货位添加不同产品。只填写尚未登记的新增实物；已有批次数量请在上方盘点修改，其他货位已登记的实物请移库。";
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
  $("inboundSave").disabled = true;
  $("inboundExistingAcknowledged").checked = false;
  $("inboundExistingLabel").classList.add("hidden");
  updateSubmitState();
}

async function findInboundCustomers() {
  invalidateInboundSelection();
  $("inboundCustomer").innerHTML = '<option value="">请选择客户</option>';
  $("inboundProduct").innerHTML = '<option value="">请先选客户</option>';
  const generation = inbound.generation;
  try {
    const params = new URLSearchParams({keyword: $("inboundCustomerQuery").value.trim(), page: "1", page_size: "50"});
    const data = await api(`/api/master/customers?${params}`);
    if (generation !== inbound.generation) return;
    $("inboundCustomer").innerHTML += (data.items || []).map(row => `<option value="${Number(row.id)}">${h(row.chinese_short_name || row.name)} · ${h(row.name)}</option>`).join("");
    $("inboundContext").textContent = "请选择客户；结果最多50条，找不到时请补全客户名称。";
  } catch (error) { if (generation === inbound.generation) showMessage(error.message); }
}

async function findInboundProducts() {
  invalidateInboundSelection();
  $("inboundProduct").innerHTML = '<option value="">请选择产品</option>';
  const customer = $("inboundCustomer").value;
  if (!customer) { showMessage("请先选择客户"); return; }
  const generation = inbound.generation;
  try {
    const params = new URLSearchParams({q: $("inboundProductQuery").value.trim(), customer_id: customer, limit: "50"});
    const data = await api(`/api/warehouse/floor3/product-candidates?${params}`);
    if (generation !== inbound.generation || customer !== $("inboundCustomer").value) return;
    $("inboundProduct").innerHTML += (data.items || []).map(row => `<option value="${Number(row.product_id)}">${h(row.product_code || row.customer_material_code)} · ${h(row.product_name)}</option>`).join("");
    $("inboundContext").textContent = data.items?.length ? "请选择产品，结果最多50条，可输入完整存货编码缩小范围。" : "未找到产品，可核对存货编码后展开管理员新增。";
  } catch (error) { if (generation === inbound.generation) showMessage(error.message); }
}

async function refreshInboundContext() {
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
    $("inboundContext").textContent = data.can_add
      ? `该产品系统已登记 ${data.existing_quantity} 只，分布在 ${data.existing_location_count} 个位置。${data.existing_quantity ? "请先核对是否为同一批实物；已登记货物应移货归位。" : "可填写当前货位尚未登记的实际数量。"}`
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
    showMessage("产品已保存，请填写实际数量后保存入库", "success");
  } catch (error) { showMessage(`${error.message}。若网络中断，请先搜索此编码核对是否已建成。`); }
  finally { setInboundBusy(false); }
  await refreshInboundContext();
}

async function saveInitialInbound() {
  if (inbound.busy || state.locked) return;
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
        quantity, stock_date: $("inboundDate").value, source_kind: "existing_stocktake"}],
    };
    persistInboundAttempt();
  }
  const locationId = inbound.attempt.items[0].location_id;
  setInboundBusy(true);
  try {
    await api("/api/warehouse/twin-operations/stocktake-batches", {method: "POST", body: JSON.stringify(inbound.attempt)});
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
$("inboundFindCustomer").onclick = findInboundCustomers;
$("inboundFindProduct").onclick = findInboundProducts;
$("inboundCustomer").onchange = () => { invalidateInboundSelection(); $("inboundProduct").innerHTML = '<option value="">请选择产品</option>'; findInboundProducts(); };
$("inboundProduct").onchange = refreshInboundContext;
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
