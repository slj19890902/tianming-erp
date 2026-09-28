(function (global) {
  "use strict";

  const PAPER_WIDTH_MM = 80;
  const PAPER_HEIGHT_MM = 40;
  const STAGE_SCALE = 8;
  const MIN_TEXT_SIZE_MM = 1.2;
  const V7_MIN_FONT_SIZE_MM = Object.freeze({
    cutting_mode: 2.6,
    rack_location: 2.6,
    custom_note: 2.6,
    customer_inventory_code: 3.0,
    product_name: 2.6,
    report_specification: 3.0,
  });
  const V1_CATALOG_VERSION = "p1-103-v1";
  const V2_CATALOG_VERSION = "p1-103-v2";
  const V3_CATALOG_VERSION = "p1-103-v3";
  const V4_CATALOG_VERSION = "p1-112-v1";
  const V5_CATALOG_VERSION = "p1-115-v1";
  const V6_CATALOG_VERSION = "p1-117-v1";
  const V7_CATALOG_VERSION = "p1-118-v1";
  const LEGACY_ELEMENT_LABELS = Object.freeze({
    board_specification: "片料尺寸",
    inventory_code: "纸箱存货编码",
    flute_type: "楞型",
    cutting_mode: "开料方式",
    customer_name: "客户名称",
    mold_label_name: "模具标签名称",
    mold_chinese_short_name: "模具中文简写",
    product_specification: "产品尺寸",
    mold_qr: "模具二维码",
  });
  const CURRENT_ELEMENT_LABELS = Object.freeze({
    board_specification: "片料尺寸",
    product_specification: "产品尺寸",
    flute_type: "楞型",
    customer_name: "客户名称",
    mold_number: "模具编号",
    mold_qr: "模具二维码",
  });
  const V6_ELEMENT_LABELS = Object.freeze({
    board_specification: "片料尺寸",
    product_specification: "产品尺寸",
    flute_type: "楞型",
    mold_identity: "客户简称与模具标签名称",
    mold_chinese_short_name: "模具中文简写",
    mold_qr: "模具二维码",
  });
  const V7_ELEMENT_LABELS = Object.freeze({
    cutting_mode: "开料方式",
    rack_location: "模具货架位置",
    custom_note: "自定义显示内容",
    section_rule_top: "顶部与中部横线",
    product_name: "产品名称",
    customer_inventory_code: "客户简称与存货编码",
    mold_qr: "模具二维码",
    section_rule_bottom: "中部与底部横线",
    report_specification: "片料尺寸",
  });
  const ELEMENT_LABELS = Object.freeze({
    ...LEGACY_ELEMENT_LABELS,
    ...CURRENT_ELEMENT_LABELS,
    ...V6_ELEMENT_LABELS,
    ...V7_ELEMENT_LABELS,
  });

  let editorConfig = null;
  let adminState = null;
  let editorLayout = null;
  let selectedElementId = "customer_name";
  let dragState = null;
  let mutationAttempt = null;
  let operationBusy = false;
  let draftPreviewTimer = null;

  const byId = (id) => document.getElementById(id);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const rounded = (value) => Math.round(Number(value) * 1000) / 1000;
  const clipped = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
  const finite = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    let body = null;
    try {
      body = await response.json();
    } catch (_error) {
      body = null;
    }
    if (!response.ok) {
      const error = new Error(
        typeof body?.detail === "string"
          ? body.detail
          : body?.detail?.message || `请求失败（${response.status}）`
      );
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function validateEnvelope(envelope) {
    if (!envelope || typeof envelope !== "object") {
      throw new Error("40×80模具标签缺少已发布布局");
    }
    const version = Number(envelope.version);
    const layout = envelope.layout;
    if (!Number.isInteger(version) || version < 0 || !layout || typeof layout !== "object") {
      throw new Error("40×80模具标签布局版本无效");
    }
    if (![V1_CATALOG_VERSION, V2_CATALOG_VERSION, V3_CATALOG_VERSION, V4_CATALOG_VERSION, V5_CATALOG_VERSION, V6_CATALOG_VERSION, V7_CATALOG_VERSION].includes(layout.catalog_version)) {
      throw new Error("40×80模具标签元素目录不受支持");
    }
    if (
      Number(layout.paper?.width_mm) !== PAPER_WIDTH_MM
      || Number(layout.paper?.height_mm) !== PAPER_HEIGHT_MM
    ) {
      throw new Error("40×80模具标签内容区尺寸无效");
    }
    const currentCatalog = [V4_CATALOG_VERSION, V5_CATALOG_VERSION].includes(layout.catalog_version);
    const catalogLabels = layout.catalog_version === V7_CATALOG_VERSION
      ? V7_ELEMENT_LABELS
      : layout.catalog_version === V6_CATALOG_VERSION
      ? V6_ELEMENT_LABELS
      : currentCatalog ? CURRENT_ELEMENT_LABELS : LEGACY_ELEMENT_LABELS;
    const elementIds = Object.keys(catalogLabels);
    if (!Array.isArray(layout.elements) || layout.elements.length !== elementIds.length) {
      throw new Error("40×80模具标签元素不完整");
    }
    const seen = new Set();
    for (const element of layout.elements) {
      if (!elementIds.includes(element?.id) || seen.has(element.id)) {
        throw new Error("40×80模具标签存在未知或重复元素");
      }
      seen.add(element.id);
      const x = finite(element.x_mm);
      const y = finite(element.y_mm);
      const width = finite(element.width_mm);
      const height = finite(element.height_mm);
      if (
        x === null || y === null || width === null || height === null
        || x < 0 || y < 0 || width <= 0 || height <= 0
        || x + width > PAPER_WIDTH_MM + .001
        || y + height > PAPER_HEIGHT_MM + .001
      ) {
        throw new Error(`${catalogLabels[element.id]}的位置或尺寸无效`);
      }
      const productSizeHidden = (
        layout.catalog_version === V3_CATALOG_VERSION
        && element.id === "product_specification"
        && element.visible === false
      );
      if (
        layout.catalog_version === V3_CATALOG_VERSION
        && element.id === "product_specification"
        && !productSizeHidden
      ) {
        throw new Error("当前40×80版式暂不显示产品尺寸");
      }
      if (!productSizeHidden && element.visible !== true) {
        throw new Error(`${catalogLabels[element.id]}不能隐藏`);
      }
      if (element.kind === "qr") {
        if (width !== 14.2 || height !== 14.2) {
          throw new Error("模具二维码必须保持14.2毫米正方形");
        }
        if (
          layout.catalog_version === V7_CATALOG_VERSION
          && (x !== 64.4 || y !== 18.7)
        ) {
          throw new Error("模具二维码必须保持既定位置");
        }
      } else if (element.kind === "rule") {
        const lineWidth = finite(element.line_width_mm);
        const color = String(element.color || "");
        if (lineWidth === null || lineWidth < .1 || lineWidth > 1 || height !== lineWidth || !/^#[0-9a-fA-F]{6}$/.test(color)) {
          throw new Error(`${catalogLabels[element.id]}的线条样式无效`);
        }
      } else if (
        element.kind !== "text"
        || finite(element.font_size_mm) === null
        || Number(element.font_size_mm) < MIN_TEXT_SIZE_MM
        || ![400, 700, 800, 900].includes(Number(element.font_weight))
        || !["left", "center", "right"].includes(element.text_align)
      ) {
        throw new Error(`${catalogLabels[element.id]}的文字样式无效`);
      }
      if (
        layout.catalog_version === V6_CATALOG_VERSION
        && ["mold_identity", "mold_chinese_short_name"].includes(element.id)
        && element.text_align !== "left"
      ) {
        throw new Error(`${catalogLabels[element.id]}必须左对齐`);
      }
    }
    for (let index = 0; index < layout.elements.length; index += 1) {
      const left = layout.elements[index];
      for (const right of layout.elements.slice(index + 1)) {
        if (left.visible === false || right.visible === false) continue;
        const overlap = !(
          Number(left.x_mm) + Number(left.width_mm) <= Number(right.x_mm) + .04
          || Number(right.x_mm) + Number(right.width_mm) <= Number(left.x_mm) + .04
          || Number(left.y_mm) + Number(left.height_mm) <= Number(right.y_mm) + .04
          || Number(right.y_mm) + Number(right.height_mm) <= Number(left.y_mm) + .04
        );
        if (overlap) {
          throw new Error(`${catalogLabels[left.id]}与${catalogLabels[right.id]}发生重叠`);
        }
      }
    }
    if (layout.catalog_version === V7_CATALOG_VERSION) {
      const elements = Object.fromEntries(layout.elements.map((element) => [element.id, element]));
      const ruleAt = (id, y) => {
        const rule = elements[id];
        if (Number(rule.x_mm) !== 1.2 || Number(rule.y_mm) !== y || Number(rule.width_mm) !== 77.6 || Number(rule.height_mm) !== .2) {
          throw new Error(`${catalogLabels[id]}必须位于三段式边界`);
        }
      };
      ruleAt("section_rule_top", 16.6);
      ruleAt("section_rule_bottom", 34.8);
      ["cutting_mode", "rack_location", "custom_note"].forEach((id) => {
        const element = elements[id];
        if (Number(element.y_mm) < .6 || Number(element.y_mm) + Number(element.height_mm) > 16.6) {
          throw new Error(`${catalogLabels[id]}必须位于顶部16毫米段`);
        }
      });
      ["product_name", "customer_inventory_code", "mold_qr"].forEach((id) => {
        const element = elements[id];
        if (Number(element.y_mm) < 16.8 || Number(element.y_mm) + Number(element.height_mm) > 34.8) {
          throw new Error(`${catalogLabels[id]}必须位于中部18毫米段`);
        }
      });
      ["product_name", "customer_inventory_code"].forEach((id) => {
        const element = elements[id];
        if (Number(element.x_mm) + Number(element.width_mm) > Number(elements.mold_qr.x_mm)) {
          throw new Error(`${catalogLabels[id]}不得与模具二维码重叠`);
        }
      });
      if (Number(elements.report_specification.y_mm) !== 35 || Number(elements.report_specification.height_mm) !== 5) {
        throw new Error("片料尺寸必须独占底部5毫米段");
      }
    }
    return envelope;
  }

  function valueForElement(row, elementId, catalogVersion = V3_CATALOG_VERSION) {
    const product = Array.isArray(row?.products) ? row.products[0] : null;
    if (catalogVersion === V7_CATALOG_VERSION) {
      const customer = String(row?.label_customer_name ?? product?.customer_short_name ?? "").trim() || "待完善";
      const inventoryCode = String(row?.label_inventory_code ?? product?.product_code ?? "").trim() || "待完善";
      const values = {
        report_specification: String(row?.label_report_specification ?? product?.report_specification ?? "").trim() || "待完善",
        cutting_mode: String(row?.label_cutting_mode ?? product?.default_cutting_mode ?? "").trim() || "待完善",
        rack_location: String(row?.label_rack_location ?? "").trim() || "待完善",
        custom_note: String(row?.label_custom_note ?? "").trim(),
        product_name: String(row?.label_product_name ?? product?.product_name ?? "").trim() || "待完善",
        customer_inventory_code: `${customer} ${inventoryCode}`,
      };
      return values[elementId] || "";
    }
    if (catalogVersion === V6_CATALOG_VERSION) {
      const customer = String(row?.label_customer_name ?? product?.customer_short_name ?? "").trim() || "待完善";
      const labelName = String(row?.label_mold_name ?? "").trim()
        || String(row?.label_mold_number ?? "").trim()
        || "待完善";
      const values = {
        board_specification: `片料 ${String(row?.label_report_specification ?? product?.report_specification ?? "").trim() || "待完善"}`,
        product_specification: String(row?.label_product_specification ?? product?.specification ?? "").trim() || "待完善",
        flute_type: String(row?.label_flute_type ?? product?.flute_type ?? "").trim() || "待完善",
        mold_identity: `${customer} ${labelName}`,
        mold_chinese_short_name: String(row?.label_mold_chinese_short_name ?? "").trim(),
      };
      return values[elementId] || "";
    }
    if ([V4_CATALOG_VERSION, V5_CATALOG_VERSION].includes(catalogVersion)) {
      const baselineValues = {
        board_specification: String(row?.label_report_specification ?? product?.report_specification ?? "").trim() || "待完善",
        product_specification: String(row?.label_product_specification ?? product?.specification ?? "").trim() || "待完善",
        flute_type: String(row?.label_flute_type ?? product?.flute_type ?? "").trim() || "待完善",
        customer_name: String(row?.label_customer_name ?? product?.customer_short_name ?? "").trim() || "待完善",
        mold_number: String(row?.label_mold_number ?? "").trim() || "待完善",
      };
      if (catalogVersion === V5_CATALOG_VERSION) {
        return `${elementId === "board_specification" ? "片料 " : ""}${baselineValues[elementId] || ""}`;
      }
      const prefixes = {
        board_specification: "片料 ",
        product_specification: "产品 ",
        flute_type: "楞型 ",
      };
      return `${prefixes[elementId] || ""}${baselineValues[elementId] || ""}`;
    }
    const values = {
      board_specification: String(row?.label_report_specification ?? product?.report_specification ?? "").trim() || "待完善",
      inventory_code: String(row?.label_inventory_code ?? product?.product_code ?? "").trim() || "待完善",
      flute_type: String(row?.label_flute_type ?? product?.flute_type ?? "").trim() || "待完善",
      cutting_mode: String(row?.label_cutting_mode ?? product?.default_cutting_mode ?? "").trim() || "待完善",
      customer_name: String(row?.label_customer_name ?? product?.customer_short_name ?? "").trim() || "待完善",
      mold_label_name: String(row?.label_mold_name ?? "").trim() || "待完善",
      mold_chinese_short_name: String(row?.label_mold_chinese_short_name ?? "").trim()
        || (catalogVersion === V3_CATALOG_VERSION ? "" : "待完善"),
      product_specification: String(row?.label_product_specification ?? product?.specification ?? "").trim() || "待完善",
    };
    const value = values[elementId] || "";
    if (catalogVersion !== V1_CATALOG_VERSION) return value;
    const legacyPrefixes = {
      board_specification: "片料 ",
      inventory_code: "纸箱 ",
      flute_type: "楞 ",
      cutting_mode: "开 ",
      mold_chinese_short_name: "中文 ",
      product_specification: "尺寸 ",
    };
    return `${legacyPrefixes[elementId] || ""}${value}`;
  }

  function identityHtml(row) {
    const product = Array.isArray(row?.products) ? row.products[0] : null;
    const customer = String(row?.label_customer_name ?? product?.customer_short_name ?? "").trim() || "待完善";
    const labelName = String(row?.label_mold_name ?? "").trim()
      || String(row?.label_mold_number ?? "").trim()
      || "待完善";
    return `<span class="mold-identity-customer">${escapeHtml(customer)}</span> <span class="mold-identity-label">${escapeHtml(labelName)}</span>`;
  }

  function textElementHtml(row, element, catalogVersion) {
    if (catalogVersion === V6_CATALOG_VERSION && element.id === "mold_identity") {
      return identityHtml(row);
    }
    return escapeHtml(valueForElement(row, element.id, catalogVersion));
  }

  function elementInlineStyle(element, scale = 1) {
    const declarations = [
      `left:${Number(element.x_mm) * scale}${scale === 1 ? "mm" : "px"}`,
      `top:${Number(element.y_mm) * scale}${scale === 1 ? "mm" : "px"}`,
      `width:${Number(element.width_mm) * scale}${scale === 1 ? "mm" : "px"}`,
      `height:${Number(element.height_mm) * scale}${scale === 1 ? "mm" : "px"}`,
    ];
    if (element.kind === "text") {
      declarations.push(
        `font-size:${Number(element.font_size_mm) * scale}${scale === 1 ? "mm" : "px"}`,
        `font-weight:${Number(element.font_weight)}`,
        `justify-content:${element.text_align === "left" ? "flex-start" : element.text_align === "right" ? "flex-end" : "center"}`,
        `text-align:${element.text_align}`
      );
    } else if (element.kind === "rule") {
      declarations.push(`background-color:${element.color}`);
    }
    return declarations.join(";");
  }

  function labelHtml(row, envelope, options = {}) {
    validateEnvelope(envelope);
    const prototypeMode = options.prototypeMode === true;
    const elements = envelope.layout.elements.filter((element) => element.visible !== false).map((element) => {
      const style = elementInlineStyle(element);
      if (element.kind === "qr") {
        return row?.qr_data_url
          ? `<img class="mold-layout-element mold-layout-qr" data-layout-id="mold_qr" style="${style}" src="${escapeHtml(row.qr_data_url)}" alt="扫码查看模具实时信息">`
          : `<div class="mold-layout-element mold-layout-qr-missing" data-layout-id="mold_qr" style="${style}">二维码<br>待生成</div>`;
      }
      if (element.kind === "rule") {
        return `<div class="mold-layout-element mold-layout-rule" data-layout-id="${escapeHtml(element.id)}" data-layout-label="${escapeHtml(ELEMENT_LABELS[element.id])}" style="${style}" aria-hidden="true"></div>`;
      }
      const value = valueForElement(row, element.id, envelope.layout.catalog_version);
      const identityClass = envelope.layout.catalog_version === V6_CATALOG_VERSION && element.id === "mold_identity"
        ? " mold-layout-identity-line"
        : "";
      const minFontSize = envelope.layout.catalog_version === V7_CATALOG_VERSION
        ? V7_MIN_FONT_SIZE_MM[element.id]
        : null;
      const fitAttributes = minFontSize === undefined || minFontSize === null
        ? ""
        : ` data-min-font-mm="${minFontSize}" data-full-text="${escapeHtml(value)}"`;
      return `<div class="mold-layout-element mold-layout-text${identityClass}${value.includes("待完善") ? " missing" : ""}" data-layout-id="${escapeHtml(element.id)}" data-layout-label="${escapeHtml(ELEMENT_LABELS[element.id])}" data-max-font-mm="${Number(element.font_size_mm)}"${fitAttributes} style="${style}">${textElementHtml(row, element, envelope.layout.catalog_version)}</div>`;
    }).join("");
    return `<article class="mold-label-page" data-layout-catalog="${escapeHtml(envelope.layout.catalog_version)}"><div class="label template-80x40 layout-driven">${elements}${prototypeMode ? '<span class="prototype-mark">样例</span>' : ""}</div></article>`;
  }

  function fitAndValidate(container) {
    const failures = [];
    for (const node of container.querySelectorAll('.mold-label-page[data-layout-catalog="p1-118-v1"] .mold-layout-text[data-min-font-mm]')) {
      let size = Number(node.dataset.maxFontMm);
      const minimum = Number(node.dataset.minFontMm);
      node.style.fontSize = `${size}mm`;
      const overflowing = () => (
        node.scrollWidth > node.clientWidth + 1
        || node.scrollHeight > node.clientHeight + 1
      );
      while (size > minimum && overflowing()) {
        size = Math.max(minimum, rounded(size - .1));
        node.style.fontSize = `${size}mm`;
      }
      node.dataset.appliedFontMm = String(size);
      if (!overflowing()) continue;
      const fullText = node.dataset.fullText || node.textContent || "";
      const ellipsis = "…";
      let low = 0;
      let high = fullText.length;
      while (low < high) {
        const middle = Math.ceil((low + high) / 2);
        node.textContent = `${fullText.slice(0, middle)}${ellipsis}`;
        if (overflowing()) high = middle - 1;
        else low = middle;
      }
      node.textContent = `${fullText.slice(0, low)}${ellipsis}`;
      node.dataset.truncated = "true";
      if (overflowing()) failures.push(node.dataset.layoutLabel || "文字");
    }
    return [...new Set(failures)];
  }

  function preflightLayout(layout) {
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.style.cssText = "position:fixed;left:-10000px;top:0;visibility:hidden;pointer-events:none";
    host.innerHTML = labelHtml(
      editorConfig?.sampleRow?.() || {},
      {version: adminState?.published?.version || 0, layout}
    );
    document.body.appendChild(host);
    try {
      return fitAndValidate(host);
    } finally {
      host.remove();
    }
  }

  async function renderPublishedResult(result, successMessage) {
    try {
      await editorConfig.onPublished(result.published);
      showStatus(successMessage, "success");
    } catch (error) {
      showStatus(
        `${successMessage}当前页面预览未完成：${error?.message || "请刷新后重新核对"}`,
        "error"
      );
    }
  }

  function currentElement() {
    return editorLayout?.elements?.find((element) => element.id === selectedElementId) || null;
  }

  function showStatus(message, kind = "") {
    const node = byId("moldLayoutStatus");
    if (!node) return;
    node.textContent = message;
    node.className = `mold-layout-status${kind ? ` ${kind}` : ""}`;
  }

  function notifyDraftChanged() {
    if (draftPreviewTimer !== null) global.clearTimeout(draftPreviewTimer);
    draftPreviewTimer = global.setTimeout(() => {
      draftPreviewTimer = null;
      if (!editorLayout) return;
      const draft = clone(editorLayout);
      global.document.dispatchEvent(new CustomEvent("tm-mold-label-draft", {detail: draft}));
      if (!editorConfig?.onDraftChanged) return;
      Promise.resolve(editorConfig.onDraftChanged(draft)).catch((error) => {
        console.warn("模具标签草稿预览未完成", error);
      });
    }, 120);
  }

  function setBusy(busy) {
    operationBusy = busy;
    [
      "moldLayoutSave",
      "moldLayoutDefault",
      "moldLayoutRollback",
      "moldLayoutClose",
    ].forEach((id) => {
      const node = byId(id);
      if (node) node.disabled = busy;
    });
    const rollback = byId("moldLayoutRollback");
    if (rollback) rollback.disabled = busy || !adminState?.can_rollback;
  }

  function stageElementHtml(element) {
    const selected = element.id === selectedElementId ? " selected" : "";
    const style = elementInlineStyle(element, STAGE_SCALE);
    if (element.kind === "qr") {
      return `<div class="mold-layout-element mold-layout-qr-missing${selected}" data-editor-element="${element.id}" style="${style}">二维码<br>14.2mm</div>`;
    }
    if (element.kind === "rule") {
      return `<div class="mold-layout-element mold-layout-rule${selected}" data-editor-element="${element.id}" style="${style}" aria-hidden="true"></div>`;
    }
    const sample = editorConfig?.sampleRow?.() || {};
    const identityClass = editorLayout?.catalog_version === V6_CATALOG_VERSION && element.id === "mold_identity"
      ? " mold-layout-identity-line"
      : "";
    return `<div class="mold-layout-element mold-layout-text${identityClass}${selected}" data-editor-element="${element.id}" style="${style}">${textElementHtml(sample, element, editorLayout?.catalog_version)}</div>`;
  }

  function renderEditor() {
    if (!editorLayout || !adminState) return;
    validateEnvelope({version: adminState.published.version, layout: editorLayout});
    const visibleElements = editorLayout.elements.filter((element) => element.visible !== false);
    byId("moldLayoutStage").innerHTML = visibleElements.map(stageElementHtml).join("");
    const select = byId("moldLayoutElement");
    select.innerHTML = visibleElements.map((element) => (
      `<option value="${escapeHtml(element.id)}">${escapeHtml(ELEMENT_LABELS[element.id])}</option>`
    )).join("");
    select.value = selectedElementId;
    const element = currentElement();
    if (!element) return;
    byId("moldLayoutX").value = element.x_mm;
    byId("moldLayoutY").value = element.y_mm;
    byId("moldLayoutWidth").value = element.width_mm;
    byId("moldLayoutHeight").value = element.height_mm;
    const isText = element.kind === "text";
    const fixedSectionGeometry = element.kind === "rule" || (
      editorLayout.catalog_version === V7_CATALOG_VERSION && element.id === "report_specification"
    );
    byId("moldLayoutX").disabled = element.kind === "rule";
    byId("moldLayoutY").disabled = fixedSectionGeometry;
    byId("moldLayoutWidth").disabled = !isText;
    byId("moldLayoutHeight").disabled = !isText || fixedSectionGeometry;
    byId("moldLayoutFontField").hidden = !isText;
    const fixedLeft = editorLayout.catalog_version === V6_CATALOG_VERSION
      && ["mold_identity", "mold_chinese_short_name"].includes(element.id);
    byId("moldLayoutAlignField").hidden = !isText || fixedLeft;
    byId("moldLayoutFont").value = isText ? element.font_size_mm : "";
    byId("moldLayoutAlign").value = isText ? element.text_align : "center";
    byId("moldLayoutVersion").textContent = `当前已发布 v${adminState.published.version}`;
    setBusy(operationBusy);
  }

  function verticalBounds(element) {
    if (editorLayout?.catalog_version !== V7_CATALOG_VERSION) {
      return [0, PAPER_HEIGHT_MM - Number(element.height_mm)];
    }
    if (["cutting_mode", "rack_location", "custom_note"].includes(element.id)) {
      return [.6, 16.6 - Number(element.height_mm)];
    }
    if (["product_name", "customer_inventory_code", "mold_qr"].includes(element.id)) {
      return [16.8, 34.8 - Number(element.height_mm)];
    }
    if (element.id === "report_specification") return [35, 35];
    return [0, PAPER_HEIGHT_MM - Number(element.height_mm)];
  }

  function widthLimit(element) {
    if (
      editorLayout?.catalog_version === V7_CATALOG_VERSION
      && ["product_name", "customer_inventory_code"].includes(element.id)
    ) return 64.4 - Number(element.x_mm);
    return PAPER_WIDTH_MM - Number(element.x_mm);
  }

  function heightLimit(element) {
    if (editorLayout?.catalog_version !== V7_CATALOG_VERSION) {
      return PAPER_HEIGHT_MM - Number(element.y_mm);
    }
    if (["cutting_mode", "rack_location", "custom_note"].includes(element.id)) return 16.6 - Number(element.y_mm);
    if (["product_name", "customer_inventory_code", "mold_qr"].includes(element.id)) return 34.8 - Number(element.y_mm);
    return PAPER_HEIGHT_MM - Number(element.y_mm);
  }

  function updateField(field, rawValue) {
    const element = currentElement();
    if (!element) return;
    if (field === "text_align") {
      const fixedLeft = editorLayout?.catalog_version === V6_CATALOG_VERSION
        && ["mold_identity", "mold_chinese_short_name"].includes(element.id);
      if (!fixedLeft && ["left", "center", "right"].includes(rawValue)) element.text_align = rawValue;
    } else {
      const value = finite(rawValue);
      if (value === null) return;
      if (field === "x_mm") element.x_mm = rounded(clipped(value, 0, PAPER_WIDTH_MM - Number(element.width_mm)));
      if (field === "y_mm") {
        const [minimum, maximum] = verticalBounds(element);
        element.y_mm = rounded(clipped(value, minimum, maximum));
      }
      if (field === "width_mm" && element.kind === "text") element.width_mm = rounded(clipped(value, .5, widthLimit(element)));
      if (field === "height_mm" && element.kind === "text") element.height_mm = rounded(clipped(value, .5, heightLimit(element)));
      if (field === "font_size_mm" && element.kind === "text") element.font_size_mm = rounded(clipped(value, MIN_TEXT_SIZE_MM, 8));
    }
    mutationAttempt = null;
    showStatus("布局有未保存修改。保存后只影响之后新登记的打印任务。");
    renderEditor();
    notifyDraftChanged();
  }

  function newOperationKey(prefix) {
    const random = global.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${random}`.slice(0, 120);
  }

  function mutationKey(signature, prefix) {
    if (!mutationAttempt || mutationAttempt.signature !== signature) {
      mutationAttempt = {signature, operationKey: newOperationKey(prefix)};
    }
    return mutationAttempt.operationKey;
  }

  async function saveAndPublish() {
    if (!editorLayout || !adminState || operationBusy) return;
    try {
      validateEnvelope({version: adminState.published.version, layout: editorLayout});
    } catch (error) {
      showStatus(error.message, "error");
      return;
    }
    const overflowFields = preflightLayout(editorLayout);
    if (overflowFields.length) {
      showStatus(
        `${overflowFields.join("、")}在当前样例中无法完整显示，请增大文字框或调整字号后再保存。`,
        "error"
      );
      return;
    }
    if (!global.confirm("确认保存并用于以后新建的40×80模具标签打印吗？历史打印任务保持原版式。")) return;
    const signature = `save|${adminState.published.version}|${JSON.stringify(editorLayout)}`;
    setBusy(true);
    try {
      const result = await requestJson("/api/warehouse/molds/label-layout/admin/publish", {
        method: "POST",
        body: JSON.stringify({
          operation_key: mutationKey(signature, "p1103-mold-layout-save"),
          expected_release_version: adminState.published.version,
          layout: editorLayout,
        }),
      });
      mutationAttempt = null;
      adminState = result;
      editorLayout = clone(result.published.layout);
      global.document.dispatchEvent(new CustomEvent("tm-mold-label-draft", {detail: null}));
      await renderPublishedResult(
        result,
        `已保存并发布 v${result.published.version}；以后新登记的打印任务使用本版。`
      );
    } catch (error) {
      showStatus(error.message || "保存模具标签布局失败", "error");
    } finally {
      setBusy(false);
      renderEditor();
    }
  }

  async function releaseOperation(kind, endpoint, confirmation) {
    if (!adminState || operationBusy || !global.confirm(confirmation)) return;
    const signature = `${kind}|${adminState.published.version}`;
    setBusy(true);
    try {
      const result = await requestJson(endpoint, {
        method: "POST",
        body: JSON.stringify({
          operation_key: mutationKey(signature, `p1103-mold-layout-${kind}`),
          expected_release_version: adminState.published.version,
        }),
      });
      mutationAttempt = null;
      adminState = result;
      editorLayout = clone(result.published.layout);
      global.document.dispatchEvent(new CustomEvent("tm-mold-label-draft", {detail: null}));
      await renderPublishedResult(
        result,
        `已追加发布 v${result.published.version}；历史打印任务保持原版式。`
      );
    } catch (error) {
      showStatus(error.message || "模具标签布局操作失败", "error");
    } finally {
      setBusy(false);
      renderEditor();
    }
  }

  function openEditor() {
    if (!adminState || editorConfig?.printJobId) return;
    editorLayout = clone(adminState.published.layout);
    selectedElementId = editorLayout.elements.some((element) => element.id === selectedElementId)
      ? selectedElementId
      : editorLayout.elements[0].id;
    mutationAttempt = null;
    if (draftPreviewTimer !== null) global.clearTimeout(draftPreviewTimer);
    draftPreviewTimer = null;
    showStatus("按住元素拖动，或输入毫米坐标、宽高和字号；二维码尺寸固定为14.2mm。")
    renderEditor();
    byId("moldLayoutEditor").hidden = false;
  }

  function closeEditor() {
    if (operationBusy) return;
    byId("moldLayoutEditor").hidden = true;
    editorLayout = null;
    dragState = null;
    mutationAttempt = null;
    if (draftPreviewTimer !== null) global.clearTimeout(draftPreviewTimer);
    draftPreviewTimer = null;
    global.document.dispatchEvent(new CustomEvent("tm-mold-label-draft", {detail: null}));
    Promise.resolve(editorConfig?.onDraftCleared?.()).catch((error) => {
      console.warn("模具标签草稿预览清除失败", error);
    });
  }

  function bindEditorEvents() {
    byId("moldLayoutOpen").addEventListener("click", openEditor);
    byId("moldLayoutClose").addEventListener("click", closeEditor);
    byId("moldLayoutSave").addEventListener("click", () => void saveAndPublish());
    byId("moldLayoutDefault").addEventListener("click", () => void releaseOperation(
      "restore-default",
      "/api/warehouse/molds/label-layout/admin/restore-default",
      "确认恢复默认布局并发布吗？当前发布版会保留在历史中。"
    ));
    byId("moldLayoutRollback").addEventListener("click", () => void releaseOperation(
      "rollback",
      "/api/warehouse/molds/label-layout/admin/rollback",
      "确认复制上一发布版并作为新版本发布吗？"
    ));
    byId("moldLayoutElement").addEventListener("change", (event) => {
      selectedElementId = event.target.value;
      renderEditor();
    });
    document.querySelectorAll("[data-mold-layout-field]").forEach((control) => {
      control.addEventListener("input", (event) => updateField(
        event.currentTarget.dataset.moldLayoutField,
        event.currentTarget.value
      ));
      control.addEventListener("change", (event) => updateField(
        event.currentTarget.dataset.moldLayoutField,
        event.currentTarget.value
      ));
    });
    const stage = byId("moldLayoutStage");
    stage.addEventListener("pointerdown", (event) => {
      const target = event.target.closest("[data-editor-element]");
      if (!target || !editorLayout) return;
      selectedElementId = target.dataset.editorElement;
      const element = currentElement();
      if (!element || element.kind === "rule") return;
      dragState = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        xMm: Number(element.x_mm),
        yMm: Number(element.y_mm),
      };
      target.setPointerCapture?.(event.pointerId);
      renderEditor();
      event.preventDefault();
    });
    stage.addEventListener("pointermove", (event) => {
      if (!dragState || dragState.pointerId !== event.pointerId) return;
      const element = currentElement();
      if (!element) return;
      element.x_mm = rounded(clipped(
        dragState.xMm + (event.clientX - dragState.startX) / STAGE_SCALE,
        0,
        PAPER_WIDTH_MM - Number(element.width_mm)
      ));
      const [minimumY, maximumY] = verticalBounds(element);
      element.y_mm = rounded(clipped(
        dragState.yMm + (event.clientY - dragState.startY) / STAGE_SCALE,
        minimumY,
        maximumY
      ));
      mutationAttempt = null;
      showStatus("布局有未保存修改。保存后只影响之后新登记的打印任务。");
      renderEditor();
      notifyDraftChanged();
      event.preventDefault();
    });
    const endDrag = (event) => {
      if (dragState?.pointerId === event.pointerId) dragState = null;
    };
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);
  }

  async function initializeEditor(config) {
    editorConfig = config;
    if (!config?.wideTemplate || !config?.prototypeMode || config?.printJobId) return false;
    try {
      const state = await requestJson("/api/warehouse/molds/label-layout/admin", {method: "GET"});
      validateEnvelope(state?.published);
      adminState = state;
      bindEditorEvents();
      byId("moldLayoutOpen").hidden = false;
      return true;
    } catch (error) {
      if (error?.status !== 403) console.warn("模具标签布局管理状态不可用", error);
      return false;
    }
  }

  global.TmMoldLabelLayout = Object.freeze({
    ELEMENT_LABELS,
    fitAndValidate,
    initializeEditor,
    labelHtml,
    requestJson,
    validateEnvelope,
  });
})(window);
