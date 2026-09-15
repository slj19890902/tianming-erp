/* global window */
(function attachDesktopDeliveryMargin(global) {
  "use strict";

  const MARGIN_PATH = "/api/dashboard/customer-delivery-margin";
  const CUSTOMER_PATH = "/api/master/customers";
  const STATUS = {empty:"无送货", complete:"完整可比", complete_with_reference:"含参考补充", partial:"部分可比"};
  const GAP_LABELS = {missing_sales_price:"销售售价缺口", missing_sales_tax_basis:"销售税口径缺口", missing_actual_material_cost:"实际材料成本缺口", missing_management_material_cost:"管理材料成本缺口", missing_reference_supplement:"参考补充缺口", foreign_currency_rate_missing:"外币汇率待核对"};
  let mounted = null;
  Object.assign(GAP_LABELS, {missing_sales_unit:"销售单位待补", estimate_only:"成本依据待确认", missing_purchase_lineage:"采购来源待关联", no_delivery_cost_source:"出库成本来源待关联", management_cost_incomplete:"材料成本待补齐", invalid_sales_contract:"销售快照异常", actual_cost_not_frozen:"实际成本待结转"});
  const savedStates = new Map();

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function number(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  function money(value) {
    const parsed = number(value);
    return parsed === null ? "待补" : `¥${parsed.toLocaleString("zh-CN", {minimumFractionDigits:2, maximumFractionDigits:2})}`;
  }
  function rate(value) {
    const parsed = number(value);
    return parsed === null ? "—" : `${(parsed * 100).toFixed(2)}%`;
  }
  function marginRate(metrics) {
    return metrics?.sales_amount !== null && metrics?.sales_amount !== undefined && Number(metrics.sales_amount) === 0 ? "—" : rate(metrics?.material_margin_rate);
  }
  function coverage(value) {
    const parsed = number(value);
    return parsed === null ? "待补" : `${(parsed * 100).toFixed(2)}%`;
  }
  function statusText(value) { return STATUS[String(value || "partial")] || "待核对"; }
  function gapText(item) {
    const reasons = Array.isArray(item?.reason_codes) ? item.reason_codes : [item?.reason || item?.reason_message];
    return reasons.filter(Boolean).map(reason => GAP_LABELS[String(reason)] || String(reason)).join("、") || "原因待补";
  }
  function gapSummary(metrics) {
    const parts = [];
    if (Number(metrics?.sales_gap_lines || 0) > 0) parts.push(`销售缺口 ${Number(metrics.sales_gap_lines)} 行`);
    if (Number(metrics?.management_cost_gap_lines || 0) > 0) parts.push(`成本待补 ${Number(metrics.management_cost_gap_lines)} 行`);
    return parts.length ? parts.join("；") : "—";
  }
  function costPart(metrics, key) { return metrics?.[key] === null || metrics?.[key] === undefined || metrics?.[key] === "" ? "待补" : money(metrics[key]); }
  function defaultDates() {
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit"}).formatToParts(new Date());
    const date = Object.fromEntries(parts.filter(item => item.type !== "literal").map(item => [item.type, item.value]));
    return {from:`${date.year}-${date.month}-01`, to:`${date.year}-${date.month}-${date.day}`};
  }
  function quantityText(metrics) {
    const parts = (Array.isArray(metrics?.quantities) ? metrics.quantities : []).map(item => `${item?.quantity ?? "待补"} ${item?.unit || "单位待完善"}`.trim());
    const unknown = number(metrics?.unknown_unit_quantity);
    if (unknown !== null && unknown > 0) parts.push(`${unknown} 单位未知`);
    return parts.length ? parts.join("；") : (number(metrics?.delivery_line_count) === 0 ? "无送货" : "数量待补");
  }
  function metricValue(metrics, full, known) {
    if (metrics?.[full] !== null && metrics?.[full] !== undefined && metrics?.[full] !== "") return {value:money(metrics[full]), partial:false};
    if (metrics?.[known] !== null && metrics?.[known] !== undefined && metrics?.[known] !== "") return {value:`${money(metrics[known])}（部分）`, partial:true};
    return {value:"待补", partial:false};
  }

  function create(config = {}) {
    const root = config.root || document.getElementById("desktopDeliveryMarginRoot");
    if (!root || config.isAllowed?.() === false || config.shell?.delivery_margin_allowed === false) return null;
    if (mounted) mounted.destroy();
    const find = id => root.querySelector(`#${id}`) || document.getElementById(id);
    const refs = {
      state:find("desktopDeliveryMarginState"), summary:find("desktopDeliveryMarginSummary"), chart:find("desktopDeliveryMarginBarChart"), trend:find("desktopDeliveryMarginTrend"), customers:find("desktopDeliveryMarginCustomers"), gaps:find("desktopDeliveryMarginGaps"), customer:find("desktopDeliveryMarginCustomer"), customerSearch:find("desktopDeliveryMarginCustomerSearch"), from:find("desktopDeliveryMarginDateFrom"), to:find("desktopDeliveryMarginDateTo"), apply:find("desktopDeliveryMarginApply"), retry:find("desktopDeliveryMarginRetry"), previous:find("desktopDeliveryMarginPrev"), next:find("desktopDeliveryMarginNext"), page:find("desktopDeliveryMarginPage"), note:find("desktopDeliveryMarginCustomerNote"), root,
    };
    if (Object.values(refs).some(value => !value)) return null;
    const dates = defaultDates();
    const identityKey = String(config.identityKey || "desktop-margin-default");
    const saved = savedStates.get(identityKey) || {};
    const state = {filters:{customerId:"", dateFrom:dates.from, dateTo:dates.to, ...(saved.filters || {})}, page:Number(saved.page || 1), pageSize:25, total:0, loading:false, loaded:false, payload:null, error:"", optionsError:"", generation:0, controller:null, optionsController:null, optionsTimer:null, disposed:false, customerOptions:saved.selectedCustomer ? [saved.selectedCustomer] : []};
    const apiGet = config.apiGet || ((path, request = {}) => global.axios.get(path, request).then(response => response.data));
    const allowed = () => config.isAllowed ? config.isAllowed() !== false : config.shell?.delivery_margin_allowed !== false;
    const mergeOptions = options => {
      const map = new Map(state.customerOptions.map(item => [String(item.id ?? item.customer_id), item]));
      (Array.isArray(options) ? options : []).forEach(item => { const id = item?.id ?? item?.customer_id; if (id !== null && id !== undefined && id !== "") map.set(String(id), item); });
      state.customerOptions = [...map.values()];
      refs.customer.replaceChildren(el("option", "", "全部授权客户"));
      state.customerOptions.sort((a,b) => String(a.name ?? a.customer_name ?? "").localeCompare(String(b.name ?? b.customer_name ?? ""), "zh-CN")).forEach(item => {
        const option = el("option", "", item.name ?? item.customer_name ?? `客户 ${item.id ?? item.customer_id}`);
        option.value = String(item.id ?? item.customer_id);
        refs.customer.append(option);
      });
      refs.customer.value = state.filters.customerId || "";
    };
    const clearResults = () => {
      state.payload = null; state.total = 0; state.loaded = false;
      refs.summary.replaceChildren(); refs.chart.replaceChildren(); refs.trend.replaceChildren(); refs.customers.replaceChildren(); refs.gaps.replaceChildren();
    };
    const showState = (message, kind = "empty") => { refs.state.hidden = false; refs.state.className = `status ${kind}`; refs.state.replaceChildren(el("span", "", message)); };
    const renderCard = (label, value, hint, partial = false) => {
      const card = el("div", `card dashboard-delivery-margin-metric${partial ? " partial" : ""}`);
      card.append(el("strong", "", value), el("span", "", label));
      if (hint) card.append(el("small", "muted", hint));
      refs.summary.append(card);
    };
    function renderCharts(metrics, daily) {
      refs.chart.replaceChildren();
      const sales = metricValue(metrics, "sales_amount", "known_sales_amount");
      const cost = metricValue(metrics, "material_cost", "known_material_cost");
      const max = Math.max(number(metrics?.sales_amount) || number(metrics?.known_sales_amount) || 0, number(metrics?.material_cost) || number(metrics?.known_material_cost) || 0, 1);
      [["销售额", sales, "sales"], ["材料成本", cost, "cost"]].forEach(([label, value, kind]) => {
        const row = el("div", "dashboard-delivery-margin-bar-row");
        const track = el("div", "dashboard-delivery-margin-bar-track");
        const bar = el("i", `dashboard-delivery-margin-bar ${kind}`);
        const amount = value.partial ? metrics?.[kind === "sales" ? "known_sales_amount" : "known_material_cost"] : metrics?.[kind === "sales" ? "sales_amount" : "material_cost"];
        const numericAmount = number(amount);
        bar.hidden = numericAmount === null;
        bar.style.width = numericAmount === null ? "0%" : `${Math.min(100, (Math.abs(numericAmount) / max) * 100)}%`;
        track.append(bar);
        row.append(el("span", "", label), track, el("b", "", value.value));
        refs.chart.append(row);
      });
      refs.chart.append(el("p", "muted dashboard-delivery-margin-chart-note", "完整值缺失时显示已知部分；金额缺口不按零补齐。"));
      refs.trend.replaceChildren();
      const rows = Array.isArray(daily) ? daily : [];
      if (!rows.length) { refs.trend.append(el("p", "empty", "暂无每日送货，趋势暂无数据。")); return; }
      rows.forEach(item => {
        const metricsForDay = item?.metrics || {};
        const row = el("div", "dashboard-delivery-margin-trend-row");
        const bar = el("i", `dashboard-delivery-margin-trend-bar${number(metricsForDay.material_margin) < 0 ? " negative" : ""}`);
        const value = number(metricsForDay.material_margin_rate);
        bar.hidden = value === null;
        bar.style.width = value === null ? "0%" : `${Math.min(100, Math.abs(value) * 100)}%`;
        row.append(el("span", "", item?.date || "日期待补"), bar, el("b", "", marginRate(metricsForDay)));
        refs.trend.append(row);
      });
      refs.trend.append(el("p", "muted dashboard-delivery-margin-chart-note", "每日比率按当日销售额计算；销售额为零显示横线。"));
    }
    function renderCustomers(payload) {
      refs.customers.replaceChildren();
      const items = Array.isArray(payload?.customers?.items) ? payload.customers.items : [];
      if (!items.length) { refs.customers.append(el("p", "empty", "当前日期与客户筛选没有送货明细。")); return; }
      const table = el("table", "dashboard-delivery-margin-table");
      const head = el("thead"); const header = el("tr");
      ["客户", "送货数量", "销售额", "实际材料成本", "参考补充成本", "材料成本", "材料毛利", "毛利率", "覆盖率", "缺口分类"].forEach(label => header.append(el("th", "", label)));
      head.append(header); table.append(head);
      const body = el("tbody");
      items.forEach(item => {
        const metrics = item?.metrics || {}; const row = el("tr");
        const gap = gapSummary(metrics);
        const marginText = `${money(metrics.material_margin)}${metrics.status === "complete_with_reference" ? "（含参考补充）" : ""}`;
        [item?.customer_name || "客户未填写", quantityText(metrics), metricValue(metrics, "sales_amount", "known_sales_amount").value, costPart(metrics, "actual_material_cost"), costPart(metrics, "supplemental_material_cost"), metricValue(metrics, "material_cost", "known_material_cost").value, marginText, marginRate(metrics), coverage(metrics.coverage_rate), gap].forEach((value, index) => {
          const cell = el(index === 0 ? "th" : "td", "", value); if (index === 6 && number(metrics.material_margin) < 0) cell.className = "negative"; row.append(cell);
        });
        body.append(row);
      });
      table.append(body); refs.customers.append(table);
    }
    function renderGaps(gaps) {
      refs.gaps.replaceChildren();
      const details = el("details", "dashboard-delivery-margin-gaps");
      refs.gaps.append(el('p','muted',`已采用参考成本 ${Number(gaps?.reference_lines || 0)} 行（可计算，非历史实际采购价）`));
      details.append(el("summary", "", `待补资料（${Number(gaps?.total_lines || 0)} 行，展开处理）`));
      const examples = Array.isArray(gaps?.examples) ? gaps.examples : [];
      if (examples.length) {
        const list = el("ul");
        examples.forEach(item => list.append(el("li", "", `${item?.customer_po || item?.delivery_number || "单号待补"} · ${item?.product_code || "编码待补"} · ${gapText(item)}`)));
        details.append(list);
      } else details.append(el("p", "empty", "当前没有缺口示例。"));
      if (gaps?.examples_truncated === true) details.append(el("p", "muted", "仅展示前20条示例，覆盖率以完整汇总为准。"));
      refs.gaps.append(details);
    }
    function renderPayload(payload) {
      const metrics = payload?.summary || {};
      refs.summary.replaceChildren(); refs.summary.hidden = false;
      const sales = metricValue(metrics, "sales_amount", "known_sales_amount"); const cost = metricValue(metrics, "material_cost", "known_material_cost");
      renderCard("送货行", String(Number(metrics.delivery_line_count || 0)), "已覆盖明细");
      renderCard("送货数量", quantityText(metrics), "计价销售行；单位未知单列");
      renderCard("含税销售额", sales.value, metrics.sales_amount === null || metrics.sales_amount === undefined ? `已知部分 ${money(metrics.known_sales_amount)}` : "完整", sales.partial);
      renderCard("材料成本（实际＋参考）", cost.value, `实际：${costPart(metrics, "actual_material_cost")} · 参考：${costPart(metrics, "supplemental_material_cost")} · 待补 ${Number(metrics.management_cost_gap_lines || 0)} 行`, cost.partial);
      renderCard("材料毛利", money(metrics.material_margin), metrics.status === "complete_with_reference" ? "含已批准参考补充" : statusText(metrics.status), false);
      renderCard("材料毛利率", marginRate(metrics), "销售额为零显示横线");
      renderCard("覆盖率", coverage(metrics.coverage_rate), `${Number(metrics.comparable_lines || 0)} / ${Number(metrics.delivery_line_count || 0)} 行可比`);
      renderCharts(metrics, payload?.daily); renderCustomers(payload); renderGaps(payload?.gaps || {});
      state.total = Number(payload?.customers?.total || 0); state.pageSize = Number(payload?.customers?.page_size || state.pageSize);
      const pages = Math.max(1, Math.ceil(state.total / state.pageSize)); refs.page.textContent = `第 ${Math.min(state.page, pages)} / ${pages} 页`; refs.previous.disabled = state.page <= 1; refs.next.disabled = state.page >= pages || !state.total;
    }
    async function loadCustomerOptions(keyword = "") {
      if (state.disposed) return;
      state.optionsController?.abort(); const controller = new AbortController(); state.optionsController = controller;
      try {
        const params = {page:1, page_size:50, include_inactive:true}; if (String(keyword).trim()) params.keyword = String(keyword).trim();
        const payload = await apiGet(CUSTOMER_PATH, {params, signal:controller.signal});
        if (!controller.signal.aborted && state.optionsController === controller) { state.optionsError = ""; refs.note.hidden = true; mergeOptions(payload?.items || []); }
      } catch (error) {
        if (!controller.signal.aborted && state.optionsController === controller && (error?.response?.status === 403 || error?.status === 403)) { state.optionsError = "当前账号无法读取客户筛选清单，仅显示全部授权汇总。"; refs.note.hidden = false; refs.note.textContent = state.optionsError; }
      } finally { if (state.optionsController === controller) state.optionsController = null; }
    }
    async function load(page = state.page) {
      if (state.disposed || !allowed()) return false;
      if (!state.filters.dateFrom || !state.filters.dateTo || state.filters.dateFrom > state.filters.dateTo) { state.error = "请选择有效的起止日期"; showState(state.error, "red"); return false; }
      state.controller?.abort(); const controller = new AbortController(); state.controller = controller; const generation = ++state.generation;
      state.page = Math.max(1, Number(page) || 1); state.loading = true; state.error = ""; refs.apply.disabled = true; refs.retry.hidden = true; showState("正在读取送货材料毛利…", "blue");
      try {
        const params = {page:state.page, page_size:state.pageSize, date_from:state.filters.dateFrom, date_to:state.filters.dateTo}; if (state.filters.customerId) params.customer_id = state.filters.customerId;
        const payload = await apiGet(MARGIN_PATH, {params, signal:controller.signal});
        if (state.disposed || controller.signal.aborted || generation !== state.generation) return false;
        state.payload = payload || {}; state.loaded = true; refs.state.hidden = true; renderPayload(state.payload); return true;
      } catch (error) {
        if (state.disposed || controller.signal.aborted || generation !== state.generation) return false;
        state.payload = null; state.loaded = false; clearResults();
        if (error?.response?.status === 401 || error?.status === 401) { destroy(); return false; }
        showState(error?.response?.status === 403 || error?.status === 403 ? "当前账号无权查看送货材料毛利。" : (error?.message || "送货材料毛利读取失败，请重试"), error?.response?.status === 403 || error?.status === 403 ? "orange" : "red"); refs.retry.hidden = false; return false;
      } finally {
        if (state.controller === controller) state.controller = null;
        if (!state.disposed && generation === state.generation) { state.loading = false; refs.apply.disabled = false; }
      }
    }
    const onCustomerChange = () => { if (!state.disposed) { state.filters.customerId = refs.customer.value; load(1); } };
    const onCustomerSearch = () => { if (!state.disposed) { clearTimeout(state.optionsTimer); state.optionsTimer = setTimeout(() => loadCustomerOptions(refs.customerSearch.value || ""), 180); } };
    const applyFilters = () => { if (!state.disposed) { state.filters.dateFrom = refs.from.value; state.filters.dateTo = refs.to.value; state.filters.customerId = refs.customer.value; load(1); } };
    const onPrevious = () => { if (!state.disposed && state.page > 1) load(state.page - 1); };
    const onNext = () => { const pages = Math.max(1, Math.ceil(state.total / state.pageSize)); if (!state.disposed && state.page < pages) load(state.page + 1); };
    const onRetry = () => load(state.page);
    refs.from.value = state.filters.dateFrom; refs.to.value = state.filters.dateTo; refs.note.hidden = true; mergeOptions(config.customerOptions || []);
    refs.customer.addEventListener("change", onCustomerChange); refs.customerSearch.addEventListener("input", onCustomerSearch); refs.apply.addEventListener("click", applyFilters); refs.retry.addEventListener("click", onRetry); refs.previous.addEventListener("click", onPrevious); refs.next.addEventListener("click", onNext);
    function onPageChange(page) {
      if (page !== "dashboard") { state.controller?.abort(); state.optionsController?.abort(); state.controller = null; state.optionsController = null; state.generation += 1; state.loaded = false; state.loading = false; refs.apply.disabled = false; clearResults(); return; }
      if (!state.loaded && !state.loading && !state.controller) load(state.page);
    }
    function updateCustomerOptions(options) { mergeOptions(options || []); }
    function detach({preserve=false} = {}) {
      state.disposed = true; state.controller?.abort(); state.optionsController?.abort(); clearTimeout(state.optionsTimer); state.generation += 1;
      refs.customer.removeEventListener("change", onCustomerChange); refs.customerSearch.removeEventListener("input", onCustomerSearch); refs.apply.removeEventListener("click", applyFilters); refs.retry.removeEventListener("click", onRetry); refs.previous.removeEventListener("click", onPrevious); refs.next.removeEventListener("click", onNext);
      if (preserve) {
        const selected = state.customerOptions.find(item => String(item.id ?? item.customer_id) === String(state.filters.customerId));
        savedStates.set(identityKey, {filters:{...state.filters}, page:state.page, selectedCustomer:selected ? {id:selected.id ?? selected.customer_id, name:selected.name ?? selected.customer_name ?? `客户 ${state.filters.customerId}`} : state.filters.customerId ? {id:state.filters.customerId, name:`客户 ${state.filters.customerId}`} : null});
        if (global.TmDesktopDeliveryMargin) global.TmDesktopDeliveryMargin._savedIdentityKey = identityKey;
      } else savedStates.delete(identityKey);
      state.controller = null; state.optionsController = null; state.customerOptions = []; clearResults(); refs.customer.replaceChildren(el("option", "", "全部授权客户")); refs.customer.value = ""; refs.customerSearch.value = ""; refs.from.value = ""; refs.to.value = ""; refs.note.textContent = ""; refs.note.hidden = true; refs.state.replaceChildren(); refs.state.hidden = true; refs.apply.disabled = false; mounted = null; if (global.TmDesktopDeliveryMargin) { global.TmDesktopDeliveryMargin._mountedRoot = null; global.TmDesktopDeliveryMargin._identityKey = null; if (!preserve) global.TmDesktopDeliveryMargin._savedIdentityKey = null; }
    }
    function leave() { detach({preserve:true}); }
    function destroy() { detach({preserve:false}); }
    mounted = {state, load, onPageChange, updateCustomerOptions, leave, destroy, identityKey};
    if (global.TmDesktopDeliveryMargin) { global.TmDesktopDeliveryMargin._mountedRoot = root; global.TmDesktopDeliveryMargin._identityKey = identityKey; }
    loadCustomerOptions(); load(state.page);
    return mounted;
  }
  const api = {mount:create, create, destroy:() => { if (mounted) mounted.destroy(); else savedStates.clear(); }, leave:() => mounted?.leave?.(), onPageChange:page => mounted?.onPageChange?.(page), updateCustomerOptions:options => mounted?.updateCustomerOptions?.(options), defaultDates, money, rate, marginRate, coverage};
  global.TmDesktopDeliveryMargin = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window === "undefined" ? globalThis : window);
