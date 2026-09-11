// Shared desktop/mobile inventory cost display. Authorization is also enforced by the API.
class WarehouseCosts extends HTMLElement {
  static observedAttributes = ["location-id", "revision"];
  constructor() { super(); this.attachShadow({mode:"open"}); }
  connectedCallback() { this.load(); }
  disconnectedCallback() { this.controller?.abort(); this.shadowRoot.replaceChildren(); }
  attributeChangedCallback() { if(this.isConnected) this.load(); }
  async load() {
    this.controller?.abort();
    const controller = this.controller = new AbortController();
    const root = this.shadowRoot;
    root.replaceChildren();
    const get = async url => { const r=await fetch(url,{credentials:"same-origin",cache:"no-store",signal:controller.signal}); if(!r.ok) throw new Error("读取失败，请刷新"); return r.json(); };
    try {
      const auth = await get("/api/auth/me");
      if(!["admin","boss"].includes(auth.user?.role)) return;
      const location = this.getAttribute("location-id");
      if(location && !/^[1-9]\d*$/.test(location)) return;
      const data = await get(`/api/warehouse/costs${location?`?location_id=${location}`:""}`);
      if(controller.signal.aborted) return;
      const el=(tag,text)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;};
      const style=el("style",`:host{display:block;color:#315454;font:13px/1.5 system-ui;margin:6px 0}*{box-sizing:border-box}details{border:1px solid #cfdfdb;border-radius:6px;background:#f3f9f7;padding:7px}summary{cursor:pointer;font-weight:600;min-height:30px;line-height:30px}a{color:#126f70}input{font:inherit;width:100%;padding:7px;border:1px solid #b8cdca;border-radius:4px;margin:6px 0}article{padding:7px 0;border-top:1px solid #dbe7e3;overflow-wrap:anywhere}.row{display:flex;flex-wrap:wrap;justify-content:space-between;gap:3px 10px}.muted{color:#647b78;font-size:12px}strong{font-variant-numeric:tabular-nums}.warning{color:#9a3412}button{font:inherit;cursor:pointer}`);
      const panel=el("details");panel.open=this.hasAttribute("expanded");
      panel.append(el("summary",`${location?"本货位":"仓库"}已定价金额 ¥${data.inventory_value}${data.missing_lots?` · ${data.missing_lots} 批待核价`:""}`));
      if(location){const link=el("a","查看全部库存成本");link.href="/factory-twin-assets/warehouse-costs.html";link.target="_blank";link.rel="noopener";panel.append(link);}
      const assistantLink=el("a","库存助手：查看现货用途与积压");assistantLink.href="/inventory-assistant.html";assistantLink.target="_blank";assistantLink.rel="noopener";panel.append(assistantLink);
      const basis=el("div",data.basis);basis.className="muted";panel.append(basis);
      const search=el("input");search.placeholder="客户 / 存货编码 / 名称 / 位置";search.setAttribute("aria-label","筛选库存成本");
      if(!location)panel.append(search);
      const list=el("div");panel.append(list);
      const render=()=>{list.replaceChildren();const q=search.value.trim().toLocaleLowerCase();
        for(const r of data.rows){if(q&&!`${r.customer_name||""} ${r.product_code||""} ${r.product_name||""} ${r.location_name||""}`.toLocaleLowerCase().includes(q))continue;
          const article=el("article");article.append(el("div",[r.customer_name,r.product_code,r.product_name].filter(Boolean).join(" · ")));
          const row=el("div");row.className="row";
          row.append(el("span",`${r.quantity}${r.unit==="sheets"?"张":"只"} · 单价 ${r.unit_cost==null?"待补价":`¥${r.unit_cost}`}`),el("strong",r.inventory_value==null?"成本待补":`¥${r.inventory_value}`));
          const info=el("div",`${r.stock_date} · ${r.lot_number}${location?"":` · ${r.location_name}`} · ${r.label}`);info.className="muted";
          article.append(row,info);list.append(article);
          if(r.validation_issue){const warning=el("div",r.validation_issue);warning.className="warning";article.append(warning);}
        }
        if(!list.childElementCount)list.append(el("div","没有符合条件的库存"));
      };search.addEventListener("input",render);render();root.append(style,panel);
    } catch(error) {
      if(controller.signal.aborted)return;
      // Do not disclose values after an authentication failure.
      root.replaceChildren();
    }
  }
}
if(!customElements.get("warehouse-costs"))customElements.define("warehouse-costs",WarehouseCosts);
