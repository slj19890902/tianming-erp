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
  function uploadTime(value) {
    if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return null;
    return `上传 ${new Date(value).toLocaleString("zh-CN", {year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false})}`;
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
    const summary = el("summary", "product-drawings-toggle", `${label} · ${items.length} 张`);
    const action = el("span", "product-drawings-action", "展开");
    summary.append(action);
    const panel = el("div", "product-drawings-panel");
    panel.id = `product-drawings-${++nextId}`;
    panel.setAttribute("aria-live", "polite");
    summary.setAttribute("aria-controls", panel.id);
    summary.setAttribute("aria-expanded", "false");
    root.append(summary, panel);
    container.append(root);
    let serial = 0, cards = [], queue = [], active = 0;
    const release = () => {
      serial++;
      cards.forEach(card => {
        card.controller?.abort();
        if (card.timer) clearTimeout(card.timer);
        if (card.url) URL.revokeObjectURL(card.url);
      });
      cards = []; queue = []; active = 0;
      panel.replaceChildren();
    };
    const dispose = () => { release(); root.open = false; };
    mounted.set(root, dispose);
    // Stopping event bubbling prevents the drawing control selecting stock or opening a take form.
    root.addEventListener("click", event => event.stopPropagation());
    const current = token => token === serial && root.open && root.isConnected;
    const fail = (card, message) => {
      card.status.textContent = message;
      card.status.hidden = false;
      card.image.hidden = true;
      card.retry.hidden = false;
      if (card.url) { URL.revokeObjectURL(card.url); card.url = null; }
    };
    async function loadCard(card, token) {
      card.queued = false;
      const controller = new AbortController();
      card.controller = controller;
      let timedOut = false;
      card.timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
      card.status.textContent = "图纸加载中…";
      card.status.hidden = false;
      card.retry.hidden = true;
      active++;
      try {
        const response = await fetch(card.item.preview_url, {credentials: "same-origin", cache: "no-store", headers: {Accept: "image/webp,image/*"}, signal: controller.signal});
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
        if (!current(token)) return;
        card.url = URL.createObjectURL(blob);
        card.image.src = card.url;
        card.image.hidden = false;
      } catch (error) {
        if (!current(token)) return;
        fail(card, timedOut ? "网络较慢，图纸加载超时" : error.name === "AbortError" ? "图纸加载已取消" : error.message === "Failed to fetch" ? "无法连接，请检查网络" : error.message || "图纸加载失败");
      } finally {
        if (card.timer) clearTimeout(card.timer);
        card.timer = null; card.controller = null;
        if (token === serial) { active--; pump(token); }
      }
    }
    function pump(token) {
      while (current(token) && active < 3 && queue.length) loadCard(queue.shift(), token);
    }
    function load() {
      release();
      if (!root.open) return;
      const token = serial;
      if (!items.length) { panel.append(el("p", "product-drawing-state", "图纸资料暂不可用，请重新查询")); return; }
      cards = items.map((item, index) => {
        const figure = el("figure", "product-drawing-item");
        const name = el("figcaption", "product-drawing-name", item.name || "工程图纸");
        const uploadedAt = uploadTime(item.uploaded_at);
        const time = el("small", "product-drawing-time", uploadedAt ? `${index === 0 ? "最新上传 · " : ""}${uploadedAt}` : `第 ${index + 1} 张`);
        const status = el("p", "product-drawing-state", "等待加载…");
        status.setAttribute("role", "status");
        const image = el("img", "product-drawing-preview");
        image.alt = `${item.name || "工程图纸"}${item.kind === "pdf" ? "（PDF首页）" : ""}`;
        image.hidden = true;
        const original = el("a", "product-drawing-original", item.kind === "pdf" ? "查看完整 PDF" : "查看原图");
        original.href = item.original_url; original.target = "_blank"; original.rel = "noopener noreferrer";
        const retry = el("button", "small-btn product-drawing-retry", "重新加载");
        retry.type = "button"; retry.hidden = true;
        const card = {item, status, image, retry, url: null, controller: null, timer: null, queued: true};
        retry.addEventListener("click", () => {
          if (!current(token) || card.controller || card.queued) return;
          card.queued = true; card.retry.hidden = true; card.status.textContent = "等待加载…";
          queue.push(card); pump(token);
        });
        image.addEventListener("load", () => { if (current(token)) status.hidden = true; });
        image.addEventListener("error", () => { if (current(token)) fail(card, "图纸预览无法显示"); });
        figure.append(name, time, status, image, original, retry);
        panel.append(figure);
        return card;
      });
      queue = cards.slice();
      pump(token);
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
