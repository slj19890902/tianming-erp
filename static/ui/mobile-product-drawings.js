(() => {
  "use strict";
  const mounted = new Map();
  let nextId = 0;
  function el(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text) result.textContent = text;
    return result;
  }
  function safeUrl(value, productId, key, endpoint) {
    if (typeof value !== "string" || !value.startsWith("/api/mobile/erp/products/")) return null;
    const expected = `/api/mobile/erp/products/${encodeURIComponent(productId)}/drawings/${encodeURIComponent(key)}/${endpoint}`;
    return value === expected ? value : null;
  }
  function disposeWithin(container) {
    for (const [root, dispose] of mounted) {
      if (root === container || container.contains(root)) { dispose(); mounted.delete(root); }
    }
  }
  function append(container, product, {label = "工程图纸"} = {}) {
    const meta = product?.drawings;
    if (!meta || !["available", "none", "forbidden"].includes(meta.status)) return null;
    if (meta.status !== "available") {
      const state = el("p", "product-drawing-state", meta.status === "forbidden" ? "无图纸查看权限" : "无工程图纸");
      container.append(state);
      return state;
    }
    const productId = product.product_id || product.id;
    const items = (Array.isArray(meta.items) ? meta.items : []).filter(item =>
      ["image", "pdf"].includes(item.kind) && safeUrl(item.preview_url, productId, item.id, "preview")
      && safeUrl(item.original_url, productId, item.id, "original"));
    const root = el("details", "product-drawings");
    const summary = el("summary", "product-drawings-toggle", label);
    const action = el("span", "product-drawings-action", "展开");
    summary.append(action);
    const panel = el("div", "product-drawings-panel");
    panel.id = `product-drawings-${++nextId}`;
    panel.setAttribute("aria-live", "polite");
    summary.setAttribute("aria-controls", panel.id);
    summary.setAttribute("aria-expanded", "false");
    root.append(summary, panel);
    container.append(root);
    let serial = 0, requests = [], objectUrls = [], timers = [];
    const release = () => {
      serial++;
      requests.forEach(controller => controller.abort()); requests = [];
      timers.forEach(timer => clearTimeout(timer)); timers = [];
      objectUrls.forEach(url => URL.revokeObjectURL(url)); objectUrls = [];
      panel.replaceChildren();
    };
    const dispose = () => { release(); root.open = false; };
    mounted.set(root, dispose);
    // Stopping event bubbling prevents the drawing control selecting stock or opening a take form.
    root.addEventListener("click", event => event.stopPropagation());
    async function load() {
      release();
      if (!root.open) return;
      const token = serial;
      if (!items.length) { panel.append(el("p", "product-drawing-state", "图纸资料暂不可用，请重新查询")); return; }
      for (const item of items) {
        const figure = el("div", "product-drawing-item");
        const status = el("p", "product-drawing-state", "图纸加载中…");
        status.setAttribute("role", "status");
        figure.append(status); panel.append(figure);
        const controller = new AbortController(); requests.push(controller);
        let timedOut = false;
        const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15000); timers.push(timer);
        try {
          const response = await fetch(item.preview_url, {credentials: "same-origin", cache: "no-store", headers: {Accept: "image/webp,image/*"}, signal: controller.signal});
          if (!response.ok) {
            const messages = {401: "登录已失效，请重新登录", 403: "无图纸查看权限", 404: "图纸文件缺失或当前不可查看", 410: "图纸已更新，请重新查询"};
            let detail = "";
            if ([413, 422].includes(response.status)) {
              try { const body = await response.json(); if (typeof body?.detail === "string") detail = body.detail.slice(0, 180); } catch (_error) { /* unavailable error body */ }
            }
            throw new Error(messages[response.status] || detail || "图纸加载失败");
          }
          const blob = await response.blob();
          if (!blob.type.startsWith("image/")) throw new Error("图纸预览暂不可用");
          if (token !== serial || !root.open || !root.isConnected) return;
          const url = URL.createObjectURL(blob); objectUrls.push(url);
          const image = el("img", "product-drawing-preview");
          image.alt = `${item.name || "工程图纸"}${item.kind === "pdf" ? "（PDF首页）" : ""}`;
          const original = el("a", "product-drawing-original", item.kind === "pdf" ? "查看原图 / 完整 PDF" : "查看原图");
          original.href = item.original_url; original.target = "_blank"; original.rel = "noopener noreferrer";
          const showError = message => {
            status.textContent = message; status.hidden = false;
            image.hidden = true;
            const retry = el("button", "small-btn product-drawing-retry", "重新加载"); retry.type = "button";
            retry.addEventListener("click", load); figure.append(retry);
          };
          image.addEventListener("load", () => { if (token === serial) status.hidden = true; });
          image.addEventListener("error", () => { if (token === serial) showError("图纸预览无法显示"); });
          image.src = url;
          figure.append(image, el("p", "product-drawing-name", image.alt), original);
        } catch (error) {
          if (token !== serial || !root.open || !root.isConnected) return;
          status.textContent = timedOut ? "网络较慢，图纸加载超时" : error.name === "AbortError" ? "图纸加载已取消" : error.message === "Failed to fetch" ? "无法连接，请检查网络" : error.message || "图纸加载失败";
          const retry = el("button", "small-btn product-drawing-retry", "重新加载"); retry.type = "button";
          retry.addEventListener("click", load); figure.append(retry);
        } finally { clearTimeout(timer); }
      }
    }
    root.addEventListener("toggle", () => {
      action.textContent = root.open ? "收起" : "展开";
      summary.setAttribute("aria-expanded", String(root.open));
      if (root.open) { mounted.set(root, dispose); load(); } else release();
    });
    return root;
  }
  window.TmProductDrawings = {append, disposeWithin};
})();
