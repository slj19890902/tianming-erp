/* global window */
(function attachDeliveryMargin(global) {
  "use strict";

  const API_PATH = "/api/dashboard/customer-delivery-margin";
  const STATUS_LABELS = {
    empty: "无送货",
    complete: "完整可比",
    complete_with_reference: "含参考补充",
    partial: "部分可比",
  };
  let mounted = null;

  function element(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined && text !== null) value.textContent = String(text);
    return value;
  }

  function beijingParts() {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date());
    return Object.fromEntries(parts.filter(part => part.type !== "literal").map(part => [part.type, part.value]));
  }

  function defaultDates() {
    const today = beijingParts();
    return {
      from: `${today.year}-${today.month}-01`,
      to: `${today.year}-${today.month}-${today.day}`,
    };
  }

  function numeric(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function money(value) {
    const parsed = numeric(value);
    if (parsed === null) return null;
    return `¥${parsed.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  }

  function rate(value) {
    const parsed = numeric(value);
    if (parsed === null) return null;
    return `${(parsed * 100).toFixed(2)}%`;
  }

  function metricsStatus(metrics) {
    return STATUS_LABELS[String(metrics?.status || "partial")] || "需核对";
  }

  function metricAmount(metrics, fullKey, knownKey, gapKey) {
    const complete = money(metrics?.[fullKey]);
    if (complete !== null) return {text: complete, partial: false, missing: false};
    const known = money(metrics?.[knownKey]);
    const lineCount = numeric(metrics?.delivery_line_count);
    const gapCount = numeric(metrics?.[gapKey]);
    const hasOnlyUnknownLines = known === "¥0.00" && lineCount !== null && lineCount > 0 && gapCount !== null && gapCount >= lineCount;
    if (known !== null && !hasOnlyUnknownLines) return {text: `${known}（部分）`, partial: true, missing: false};
    return {text: "待核对", partial: false, missing: true};
  }

  function textMetric(value, fallback = "待核对") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function quantityText(metrics) {
    const quantities = Array.isArray(metrics?.quantities) ? metrics.quantities : [];
    const parts = quantities.map(item => {
      const quantity = numeric(item?.quantity);
      const unit = String(item?.unit || "单位待完善").trim();
      return quantity === null ? `${unit} 数量待核对` : `${quantity} ${unit}`;
    });
    const unknown = numeric(metrics?.unknown_unit_quantity);
    if (unknown !== null && unknown > 0) parts.push(`${unknown} 单位待核对`);
    return parts.length ? parts.join("；") : (numeric(metrics?.delivery_line_count) === 0 ? "无送货" : "数量待核对");
  }

  function queryString(filters, page) {
    const params = new URLSearchParams({page: String(page), page_size: "20"});
    if (filters.customerId) params.set("customer_id", filters.customerId);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    return `${API_PATH}?${params.toString()}`;
  }

  function renderMetricCard(container, label, amount, hint) {
    const card = element("div", `metric${amount.partial ? " partial" : ""}${amount.missing ? " gap" : ""}`);
    card.append(element("strong", "", amount.text), element("span", "", label));
    if (hint) card.append(element("small", "muted", hint));
    container.append(card);
  }

  function renderBarChart(container, metrics) {
    container.replaceChildren();
    const legend = element("div", "delivery-margin-chart-legend");
    const salesLegend = element("span", "sales");
    salesLegend.append(element("i"), element("span", "", "销售额"));
    const costLegend = element("span", "cost");
    costLegend.append(element("i"), element("span", "", "材料成本"));
    legend.append(salesLegend, costLegend);
    container.append(legend);
    const salesAmount = metricAmount(metrics, "sales_amount", "known_sales_amount", "sales_gap_lines");
    const costAmount = metricAmount(metrics, "material_cost", "known_material_cost", "management_cost_gap_lines");
    const sales = salesAmount.missing ? null : numeric(metrics?.sales_amount) ?? numeric(metrics?.known_sales_amount);
    const cost = costAmount.missing ? null : numeric(metrics?.material_cost) ?? numeric(metrics?.known_material_cost);
    const max = Math.max(sales ?? 0, cost ?? 0, 1);
    [["销售额", sales, "sales", metrics?.sales_amount], ["材料成本", cost, "cost", metrics?.material_cost]].forEach(([label, value, kind, complete]) => {
      const row = element("div", "delivery-margin-bar-row");
      row.append(element("span", "delivery-margin-bar-label", label));
      const track = element("div", "delivery-margin-bar-track");
      const bar = element("div", `delivery-margin-bar ${kind}${value === null ? " unknown" : ""}`);
      bar.style.width = value === null ? "3px" : `${Math.max(2, Math.min(100, value / max * 100))}%`;
      const valueText = complete === null || complete === undefined
        ? (value === null ? "待核对" : `${money(value)}（已知）`)
        : money(complete);
      bar.title = `${label}：${valueText}`;
      track.append(bar, element("span", "delivery-margin-bar-value", valueText));
      row.append(track);
      container.append(row);
    });
    container.append(element("p", "delivery-margin-chart-note", "柱图按当前筛选范围展示；部分数据仅代表已知金额，不能替代完整材料毛利。"));
  }

  function renderTrend(container, daily) {
    container.replaceChildren();
    const rows = Array.isArray(daily) ? daily : [];
    if (!rows.length) {
      container.append(element("p", "muted", "当前日期范围没有每日送货记录。"));
      return;
    }
    rows.forEach(item => {
      const row = element("div", "delivery-margin-trend-row");
      const value = numeric(item?.metrics?.material_margin_rate);
      const track = element("div", "delivery-margin-trend-track");
      const bar = element("div", `delivery-margin-trend-bar${value === null ? " unknown" : ""}${value !== null && value < 0 ? " negative" : ""}`);
      if (value !== null) bar.style.width = `${Math.min(100, Math.max(2, Math.abs(value) * 100))}%`;
      else bar.style.width = "3px";
      track.append(bar);
      row.append(
        element("span", "delivery-margin-trend-date", item?.date || "日期未知"),
        track,
        element("span", "delivery-margin-trend-value", rate(item?.metrics?.material_margin_rate) || "待核对"),
      );
      container.append(row);
    });
  }

  function renderCustomerRows(container, payload, onToggle) {
    container.replaceChildren();
    const items = Array.isArray(payload?.customers?.items) ? payload.customers.items : [];
    if (!items.length) {
      container.append(element("p", "muted", "当前筛选范围没有客户送货记录。"));
      return;
    }
    items.forEach((item, index) => {
      const row = element("article", "delivery-margin-customer-row");
      const toggle = element("button", "delivery-margin-customer-toggle");
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", "false");
      const name = item?.customer_name || "客户未填写";
      toggle.append(element("strong", "", name), element("span", "", metricsStatus(item?.metrics)));
      const facts = element("div", "delivery-margin-customer-facts");
      const metrics = item?.metrics || {};
      const sales = metricAmount(metrics, "sales_amount", "known_sales_amount", "sales_gap_lines");
      const cost = metricAmount(metrics, "material_cost", "known_material_cost", "management_cost_gap_lines");
      const materialMargin = {text: money(metrics.material_margin) || "待核对", partial: false, missing: metrics.material_margin === null || metrics.material_margin === undefined};
      [["销售额", sales], ["材料成本", cost], ["材料成本毛利", materialMargin], ["材料毛利率", {text: rate(metrics.material_margin_rate) || "待核对", partial: false}], ["送货数量", {text: quantityText(metrics), partial: false}], ["可比覆盖", {text: `${rate(metrics.coverage_rate) || "待核对"}（${textMetric(metrics.comparable_lines, "0")}行）`, partial: false}]].forEach(([label, amount]) => {
        const fact = element("div", "delivery-margin-customer-fact");
        fact.append(element("strong", "", amount.text), element("span", "", label));
        facts.append(fact);
      });
      facts.hidden = true;
      toggle.addEventListener("click", () => {
        const expanded = !facts.hidden;
        facts.hidden = expanded;
        toggle.setAttribute("aria-expanded", String(!expanded));
        onToggle?.(index, !expanded);
      });
      row.append(toggle, facts);
      container.append(row);
    });
  }

  function renderGaps(container, gaps) {
    const labels={missing_sales_unit:'销售单位待补',missing_sales_tax_basis:'销售税口径待补',missing_sales_price:'销售单价待补',estimate_only:'成本依据待确认',missing_purchase_lineage:'采购来源待关联',no_delivery_cost_source:'出库成本来源待关联',management_cost_incomplete:'材料成本待补齐',invalid_sales_contract:'销售快照异常',actual_cost_not_frozen:'实际成本待结转'};
    const total = Number(gaps?.total_lines);
    const lineCount = Number.isFinite(total) && total > 0 ? total : 0;
    container.replaceChildren();
    container.append(element('p','muted',`已采用参考成本 ${Number(gaps?.reference_lines || 0)} 行（非历史实际采购价）`));
    const details = element("details", "delivery-margin-gap-summary");
    const summary = element("summary", "", lineCount ? `待补资料（${lineCount} 行，展开处理）` : "待补资料（无）");
    details.append(summary);
    if (lineCount) {
      const reasons = Object.entries(gaps?.reason_counts || {}).map(([key, value]) => `${labels[key] || '来源待核对'} ${value}行`).join("；");
      if (reasons) details.append(element("p", "muted", `原因统计：${reasons}`));
      const list = element("div", "delivery-margin-gap-list");
      (Array.isArray(gaps?.examples) ? gaps.examples : []).forEach(example => {
        const line = [example?.delivery_number ? `送货 ${example.delivery_number}` : "", example?.delivery_item_id ? `行 ${example.delivery_item_id}` : ""].filter(Boolean).join("｜");
        const item = element("div", "delivery-margin-gap-item", line || "缺口明细");
        const reasonCodes = example?.reason || example?.reason_message || (Array.isArray(example?.reason_codes) ? example.reason_codes.map(x=>labels[x] || '来源待核对').join("、") : "需核对来源");
        item.append(element("div", "delivery-margin-gap-reasons", reasonCodes));
        list.append(item);
      });
      if (gaps?.examples_truncated === true) list.append(element("p", "muted", "仅展示前20条示例，覆盖率以汇总指标为准。"));
      details.append(list);
    }
    container.append(details);
  }

  function renderCustomerOptions(select, options, selected) {
    const unique = new Map();
    (Array.isArray(options) ? options : []).forEach(item => {
      const id = item?.customer_id ?? item?.id;
      if (id === null || id === undefined || id === "") return;
      unique.set(String(id), item?.customer_name || item?.name || `客户 ${id}`);
    });
    select.replaceChildren(element("option", "", "全部可见客户"));
    select.firstChild.value = "";
    [...unique.entries()].sort((a, b) => a[1].localeCompare(b[1], "zh-CN")).forEach(([id, name]) => {
      const option = element("option", "", name);
      option.value = id;
      select.append(option);
    });
    select.value = selected || "";
  }

  function mount({shell, apiGet}) {
    const section = document.getElementById("deliveryMarginSection");
    if (!section || shell?.delivery_margin_allowed !== true) {
      if (mounted) mounted.destroy();
      if (section) section.hidden = true;
      return null;
    }
    if (mounted) {
      mounted.shell = shell;
      mounted.refreshCustomerOptions(shell?.customer_options || []);
      return mounted;
    }
    section.hidden = false;
    const panel = document.getElementById("deliveryMarginPanel");
    const stateBox = document.getElementById("deliveryMarginState");
    const summary = document.getElementById("deliveryMarginSummary");
    const chart = document.getElementById("deliveryMarginBarChart");
    const trend = document.getElementById("deliveryMarginTrend");
    const customers = document.getElementById("deliveryMarginCustomers");
    const gaps = document.getElementById("deliveryMarginGaps");
    const customerSelect = document.getElementById("deliveryMarginCustomer");
    const customerSearch = document.getElementById("deliveryMarginCustomerSearch");
    const customerNote = document.getElementById("deliveryMarginCustomerNote");
    const fromInput = document.getElementById("deliveryMarginDateFrom");
    const toInput = document.getElementById("deliveryMarginDateTo");
    const reloadButton = document.getElementById("loadDeliveryMargin");
    const applyButton = document.getElementById("applyDeliveryMargin");
    const prev = document.getElementById("deliveryMarginPrev");
    const next = document.getElementById("deliveryMarginNext");
    const pageText = document.getElementById("deliveryMarginPageText");
    const defaults = defaultDates();
    fromInput.value = defaults.from;
    toInput.value = defaults.to;
    const localState = {shell, filters: {customerId: "", dateFrom: defaults.from, dateTo: defaults.to}, page: 1, total: 0, pageSize: 20, controller: null, optionsController: null, optionsTimer: null, generation: 0, disposed: false, loaded: false, loading: false, customerOptions: [], customerSearch: ""};

    function setState(message, kind) {
      stateBox.hidden = false;
      stateBox.className = `status ${kind || "empty"}`;
      stateBox.replaceChildren(element("div", "", message));
    }

    function refreshCustomerOptions(options) {
      localState.customerOptions = [...localState.customerOptions, ...(Array.isArray(options) ? options : [])];
      renderCustomerOptions(customerSelect, localState.customerOptions, localState.filters.customerId);
    }

    async function loadCustomerOptions(keyword = "") {
      if (localState.disposed) return;
      localState.optionsController?.abort();
      const controller = new AbortController();
      localState.optionsController = controller;
      try {
        const params = new URLSearchParams({page: "1", page_size: "50", include_inactive: "true"});
        if (String(keyword || "").trim()) params.set("keyword", String(keyword).trim());
        const payload = await apiGet(`/api/master/customers?${params.toString()}`, {signal: controller.signal});
        if (!controller.signal.aborted && localState.optionsController === controller) {
          customerNote.hidden = true;
          refreshCustomerOptions(payload?.items || []);
        }
      } catch (error) {
        // The margin response remains usable if this optional authorized-list read is unavailable.
        if (error?.name !== "AbortError" && localState.optionsController === controller && error?.status === 403) {
          customerNote.hidden = false;
          customerNote.textContent = "当前账号无法读取客户筛选清单，仅显示全部可见客户汇总。";
        }
      } finally {
        if (localState.optionsController === controller) localState.optionsController = null;
      }
    }

    async function load(page = localState.page) {
      if (localState.disposed) return;
      localState.controller?.abort();
      const controller = new AbortController();
      localState.controller = controller;
      const generation = ++localState.generation;
      localState.page = Math.max(1, Number(page) || 1);
      localState.loading = true;
      setState("正在读取送货材料毛利…", "empty");
      reloadButton.disabled = true;
      applyButton.disabled = true;
      try {
        const payload = await apiGet(queryString(localState.filters, localState.page), {signal: controller.signal});
        if (controller.signal.aborted || generation !== localState.generation || localState.page !== Number(payload?.customers?.page || localState.page)) return;
        localState.loaded = true;
        panel.hidden = false;
        stateBox.hidden = true;
        const metrics = payload?.summary || {};
        summary.replaceChildren();
        renderMetricCard(summary, "销售额（含税可比）", metricAmount(metrics, "sales_amount", "known_sales_amount", "sales_gap_lines"), metricsStatus(metrics));
        renderMetricCard(summary, "材料成本（管理口径）", metricAmount(metrics, "material_cost", "known_material_cost", "management_cost_gap_lines"), metricsStatus(metrics));
        renderMetricCard(summary, "材料毛利", {text: money(metrics.material_margin) || "待核对", missing: metrics.material_margin === null || metrics.material_margin === undefined, partial: false}, "不含工资、能耗、外协和期间费用");
        renderMetricCard(summary, "材料毛利率", {text: rate(metrics.material_margin_rate) || "待核对", missing: metrics.material_margin_rate === null || metrics.material_margin_rate === undefined, partial: false}, `送货数量 ${quantityText(metrics)}｜覆盖率 ${rate(metrics.coverage_rate) || "待核对"}`);
        summary.hidden = false;
        renderBarChart(chart, metrics);
        renderTrend(trend, payload?.daily);
        renderCustomerRows(customers, payload, () => {});
        renderGaps(gaps, payload?.gaps);
        localState.total = Number(payload?.customers?.total) || 0;
        localState.pageSize = Number(payload?.customers?.page_size) || 20;
        const totalPages = Math.max(1, Math.ceil(localState.total / localState.pageSize));
        pageText.textContent = `第 ${localState.page} / ${totalPages} 页`;
        prev.disabled = localState.page <= 1;
        next.disabled = localState.page >= totalPages || !localState.total;
        // The report page is not a customer directory; only merge an explicit authorized option payload.
        refreshCustomerOptions(payload?.customer_options || []);
      } catch (error) {
        if (error?.name === "AbortError" || generation !== localState.generation) return;
        setState(error?.message || "送货材料毛利读取失败", "error");
      } finally {
        if (localState.controller === controller) localState.controller = null;
        if (generation === localState.generation) localState.loading = false;
        if (generation === localState.generation) {
          reloadButton.disabled = false;
          applyButton.disabled = false;
        }
      }
    }

    const onCustomerChange = () => { localState.filters.customerId = customerSelect.value; };
    const onCustomerSearch = () => {
      if (localState.disposed) return;
      localState.customerSearch = customerSearch?.value || "";
      clearTimeout(localState.optionsTimer);
      localState.optionsTimer = setTimeout(() => loadCustomerOptions(localState.customerSearch), 180);
    };
    customerSelect.addEventListener("change", onCustomerChange);
    customerSearch?.addEventListener("input", onCustomerSearch);
    const applyFilters = () => {
      if (localState.disposed) return;
      const dateFrom = fromInput.value;
      const dateTo = toInput.value;
      if (!dateFrom || !dateTo || dateFrom > dateTo) {
        setState("请输入有效的开始和结束日期（开始不能晚于结束）。", "error");
        return;
      }
      localState.filters.dateFrom = dateFrom;
      localState.filters.dateTo = dateTo;
      localState.filters.customerId = customerSelect.value;
      load(1);
    };
    const onApply = applyFilters;
    const onReload = applyFilters;
    const onPrev = () => { if (localState.page > 1) load(localState.page - 1); };
    const onNext = () => {
      const totalPages = Math.max(1, Math.ceil(localState.total / localState.pageSize));
      if (localState.page < totalPages) load(localState.page + 1);
    };
    applyButton.addEventListener("click", onApply);
    reloadButton.addEventListener("click", onReload);
    prev.addEventListener("click", onPrev);
    next.addEventListener("click", onNext);
    refreshCustomerOptions(shell?.customer_options || []);
    function onPageChange(page) {
      if (page !== "home") {
        if (localState.controller) {
          localState.controller.abort();
          localState.controller = null;
          localState.generation += 1;
          localState.loaded = false;
          localState.loading = false;
        }
        localState.optionsController?.abort();
        return;
      }
      if (!localState.loaded && !localState.loading && !localState.controller) load(localState.page);
    }
    function destroy() {
      localState.disposed = true;
      localState.controller?.abort();
      localState.optionsController?.abort();
      clearTimeout(localState.optionsTimer);
      customerSelect.removeEventListener("change", onCustomerChange);
      customerSearch?.removeEventListener("input", onCustomerSearch);
      applyButton.removeEventListener("click", onApply);
      reloadButton.removeEventListener("click", onReload);
      prev.removeEventListener("click", onPrev);
      next.removeEventListener("click", onNext);
      localState.controller = null;
      localState.optionsController = null;
      localState.generation += 1;
      localState.loaded = false;
      localState.loading = false;
      summary.replaceChildren();
      chart.replaceChildren();
      trend.replaceChildren();
      customers.replaceChildren();
      gaps.replaceChildren();
      renderCustomerOptions(customerSelect, [], "");
      customerNote.textContent = "";
      customerNote.hidden = true;
      stateBox.replaceChildren();
      stateBox.hidden = true;
      panel.hidden = false;
      section.hidden = true;
      mounted = null;
    }
    mounted = {shell, load, refreshCustomerOptions, onPageChange, destroy, state: localState};
    loadCustomerOptions();
    load(1);
    return mounted;
  }

  const api = {mount, onPageChange: page => mounted?.onPageChange?.(page), destroy: () => mounted?.destroy?.(), defaultDates, numeric, money, rate, queryString};
  global.TmMobileDeliveryMargin = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window === "undefined" ? globalThis : window);
