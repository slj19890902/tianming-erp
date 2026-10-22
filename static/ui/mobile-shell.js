(function (global) {
  "use strict";
  const routes = {home:"/frontend-v2/mobile",query:"/frontend-v2/mobile/query",warehouse:"/frontend-v2/mobile/warehouse",more:"/frontend-v2/mobile/more"};
  const labels = {home:"首页",query:"查询",warehouse:"仓库",more:"更多"};
  const normalize = page => ({lookup:"query",search:"warehouse",incoming:"more",production:"more",pre_delivery:"more"}[page] || page);
  function safeReturn(value, origin = global.location?.origin) {
    if (!value || typeof value !== "string") return null;
    try {
      const url = new URL(value, origin);
      if (url.origin !== origin || url.username || url.password || !/^https?:$/.test(url.protocol)) return null;
      // Only known employee UI routes may be used as a return target. Never follow an API/action URL.
      if (!/^(?:\/frontend-v2\/mobile(?:\/|$)|\/mobile(?:\/|$)|\/[PMI]\/\d+(?:\/|$)|\/q\/\d+(?:\/|$)|\/m\/mold-(?:rack|cell)$|\/mobile_mold_(?:lookup|rack)\.html$)/.test(url.pathname)) return null;
      if ([...url.searchParams.keys()].some(key => /token|password|secret/i.test(key))) return null;
      return url.pathname + url.search + url.hash;
    } catch (_) { return null; }
  }
  function navigation(shell) {
    const ids = new Set((shell?.entries || []).map(entry => entry.id));
    return ["home", ...(ids.has("lookup") ? ["query"] : []), ...(ids.has("warehouse") ? ["warehouse"] : []), "more"].map(id => ({id,label:labels[id],href:routes[id]}));
  }
  function readEpoch() {
    let generation = 0, controller = null;
    return {
      begin() {
        controller?.abort(); controller = new AbortController(); const currentController=controller, token = ++generation;
        return {signal:currentController.signal,current:() => token === generation && !currentController.signal.aborted};
      },
      cancel() { generation++; controller?.abort(); controller = null; }
    };
  }
  let instance = null;
  function mount(options = {}) {
    if (instance) return instance;
    const doc = global.document, body = doc.body;
    if (!body) return null;
    let embedded = false;
    try { embedded = new URLSearchParams(global.location.search).get("embedded") === "1" && global.parent !== global && global.parent.location.origin === global.location.origin; } catch (_) {}
    // incoming.html remains a desktop page when it is not inside the mobile portal.
    if (body.dataset.mobileEmbeddedOnly === "1" && !embedded) return null;
    body.classList.add("tm-mobile-shell");
    body.classList.toggle("tm-mobile-embedded", embedded);
    const portal = body.dataset.mobilePortal === "1";
    body.classList.toggle("tm-mobile-portal", portal);
    const title = body.dataset.mobileTitle || doc.title.split(" - ")[0];
    let page = normalize(options.page || body.dataset.mobilePage || "more");
    let shell = null, generation = 0, controller = null;
    const header = doc.createElement("div"); header.className = "tm-shell-header";
    const back = doc.createElement("a"); back.className = "tm-shell-back"; back.textContent = "返回";
    const returnTarget = safeReturn(new URLSearchParams(global.location.search).get("return_to"));
    back.href = returnTarget || routes[page] || routes.home;
    const heading = doc.createElement("strong"); heading.textContent = "天明 ERP";
    header.append(back, heading);
    const status = doc.createElement("div"); status.className = "tm-shell-status"; status.hidden = true; status.setAttribute("role", "status");
    const nav = doc.createElement("nav"); nav.className = "tm-shell-nav"; nav.hidden = true; nav.setAttribute("aria-label", "手机主导航");
    if (!embedded) { body.prepend(header); body.append(status, nav); }
    function draw() {
      nav.replaceChildren();
      if (!shell) { nav.hidden = true; body.classList.remove("tm-mobile-has-nav"); return; }
      navigation(shell).forEach(item => {
        const link = doc.createElement("a"); link.href = item.href; link.textContent = item.label;
        if (item.id === page) link.setAttribute("aria-current", "page"); nav.append(link);
      });
      nav.hidden = embedded; body.classList.toggle("tm-mobile-has-nav", !embedded);
    }
    function update(value) { shell = value?.user?.id && Array.isArray(value.entries) ? value : null; status.hidden = true; draw(); }
    async function refresh() {
      const token = ++generation; controller?.abort(); controller = new AbortController();
      try {
        const response = await global.fetch("/api/mobile/erp/shell", {credentials:"same-origin",cache:"no-store",signal:controller.signal,headers:{Accept:"application/json"}});
        if (token !== generation) return;
        if (!response.ok) { update(null); return; } // Existing page owns login/token authorization.
        const value = await response.json(); if (token === generation) update(value);
      } catch (error) {
        if (token !== generation || error.name === "AbortError") return;
        update(null);
        if (!embedded) { status.textContent = "导航读取失败，当前业务页面仍可继续使用。"; const retry=doc.createElement("button"); retry.type="button"; retry.textContent="重新读取导航"; retry.onclick=refresh; status.append(retry); status.hidden=false; }
      }
    }
    function beforeNavigate(event) {
      const link = event.target.closest?.("a"); if (!link || (!nav.contains(link) && !header.contains(link))) return;
      // Page executors retain ownership of busy/unknown-request guards and beforeunload handlers.
      const request = new CustomEvent("tm-mobile-shell-navigate", {cancelable:true,detail:{href:link.href}});
      if (!global.dispatchEvent(request)) { event.preventDefault(); return; }
      if (header.contains(link) && body.dataset.mobileReturnControl) {
        const control=doc.getElementById(body.dataset.mobileReturnControl);
        if(control){event.preventDefault();if(!control.disabled)control.click();}
      }
    }
    header.addEventListener("click", beforeNavigate); nav.addEventListener("click", beforeNavigate);
    instance = {update,refresh,setPage(value){page=normalize(value);draw();},destroy(){generation++;controller?.abort();header.remove();nav.remove();status.remove();body.classList.remove("tm-mobile-shell","tm-mobile-embedded","tm-mobile-portal","tm-mobile-has-nav");instance=null;}};
    if (options.shell) update(options.shell); else if (!portal && !embedded) void refresh();
    global.addEventListener("pageshow", event => { if (event.persisted && !portal && !embedded) void refresh(); });
    global.addEventListener("tm-mobile-auth-changed", () => { update(null); if (!portal && !embedded) void refresh(); });
    global.dispatchEvent(new CustomEvent("tm-mobile-shell-ready"));
    return instance;
  }
  global.TmMobileShell = {mount,navigation,safeReturn,readEpoch,routes,update(value){instance?.update(value);},setPage(value){instance?.setPage(value);},refresh(){return instance?.refresh();}};
  if (global.document) {
    if (global.document.readyState === "loading") global.document.addEventListener("DOMContentLoaded", () => mount(), {once:true}); else mount();
  }
})(window);
