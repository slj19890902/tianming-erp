// Shared desktop/mobile inventory cost display. The API enforces role and customer scope.
class WarehouseCosts extends HTMLElement {
  static observedAttributes = ["location-id", "revision"];
  constructor() { super(); this.attachShadow({mode:"open"}); }
  connectedCallback() { this.load(); }
  disconnectedCallback() { this.cancel(); this.shadowRoot.replaceChildren(); }
  attributeChangedCallback() { if(this.isConnected) this.load(); }
  cancel() { this.controller?.abort(); this.pageController?.abort(); clearTimeout(this.searchTimer); }
  async load() {
    this.cancel();
    const lifetime = this.controller = new AbortController();
    const root = this.shadowRoot;
    root.replaceChildren();
    const el=(tag,text)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;};
    const get=async(url,signal)=>{
      const r=await fetch(url,{credentials:"same-origin",cache:"no-store",signal});
      if(!r.ok){const e=new Error("读取失败，请重试");e.status=r.status;throw e;}return r.json();
    };
    try {
      const auth=await get("/api/auth/me",lifetime.signal);
      if(lifetime.signal.aborted||!["admin","boss"].includes(auth.user?.role))return;
      const location=this.getAttribute("location-id");
      if(location&&!/^[1-9]\d*$/.test(location))return;
      const style=el("style",`:host{display:block;color:#315454;font:13px/1.5 system-ui;margin:6px 0}*{box-sizing:border-box}details{border:1px solid #cfdfdb;border-radius:6px;background:#f3f9f7;padding:7px}summary{cursor:pointer;font-weight:600;min-height:30px;line-height:30px}a{color:#126f70}input{font:inherit;width:100%;padding:7px;border:1px solid #b8cdca;border-radius:4px;margin:6px 0}article{padding:7px 0;border-top:1px solid #dbe7e3;overflow-wrap:anywhere}.row,.pager{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:3px 10px}.pager{border-top:1px solid #dbe7e3;padding-top:7px}.muted{color:#647b78;font-size:12px}strong{font-variant-numeric:tabular-nums}.warning{color:#9a3412}button{font:inherit;cursor:pointer;min-height:30px}button:disabled{cursor:default;opacity:.5}`);
      const panel=el("details");panel.open=this.hasAttribute("expanded");
      const summary=el("summary","正在读取库存成本…");panel.append(summary);
      if(location){const link=el("a","查看全部库存成本");link.href="/factory-twin-assets/warehouse-costs.html";link.target="_blank";link.rel="noopener";panel.append(link);}
      const assistantLink=el("a","库存助手：查看现货用途与积压");assistantLink.href="/inventory-assistant.html";assistantLink.target="_blank";assistantLink.rel="noopener";panel.append(assistantLink);
      const basis=el("div");basis.className="muted";panel.append(basis);
      const search=el("input");search.placeholder="客户 / 存货编码 / 名称 / 位置";search.setAttribute("aria-label","筛选库存成本");
      if(!location)panel.append(search);
      const list=el("div");list.setAttribute("aria-live","polite");panel.append(list);
      const pager=el("div");pager.className="pager";
      const previous=el("button","上一页"),next=el("button","下一页"),pageLabel=el("span");
      previous.type=next.type="button";pager.append(previous,pageLabel,next);panel.append(pager);
      root.append(style,panel);
      let currentPage=1, hasMore=false, requestId=0, busy=false;
      const setBusy=value=>{busy=value;previous.disabled=value||currentPage<=1;next.disabled=value||!hasMore;list.setAttribute("aria-busy",String(value));};
      const renderRows=data=>{
        list.replaceChildren();
        for(const r of data.rows){
          const article=el("article");article.append(el("div",[r.customer_name,r.product_code,r.product_name].filter(Boolean).join(" · ")));
          const row=el("div");row.className="row";
          row.append(el("span",`${r.quantity}${r.unit==="sheets"?"张":"只"} · 单价 ${r.unit_cost==null?"待补价":`¥${r.unit_cost}`}`),el("strong",r.inventory_value==null?"成本待补":`¥${r.inventory_value}`));
          const info=el("div",`${r.stock_date} · ${r.lot_number}${location?"":` · ${r.location_name}`} · ${r.label}`);info.className="muted";
          article.append(row,info);list.append(article);
          if(r.cost_basis){const b=el("div",r.cost_basis);b.className="muted";article.append(b);}
          if(auth.user.role==="admin"&&r.product_id&&!location){const edit=el("button","成本依据");edit.type="button";edit.addEventListener("click",async()=>{const {editCostRule}=await import("./warehouse-cost-rule-editor.js?v=20260911-1");if(!lifetime.signal.aborted)await editCostRule(root,r,()=>this.load());});article.append(edit);}
          if(r.validation_issue){const warning=el("div",r.validation_issue);warning.className="warning";article.append(warning);}
        }
        if(!data.rows.length)list.append(el("div","没有符合条件的库存"));
      };
      const loadPage=async(page,keyword=search.value.trim())=>{
        this.pageController?.abort();
        const controller=this.pageController=new AbortController(), id=++requestId;
        setBusy(true);list.replaceChildren(el("div","正在读取…"));
        const params=new URLSearchParams({page:String(page),page_size:"50",keyword});
        if(location)params.set("location_id",location);
        try{
          const data=await get(`/api/warehouse/costs?${params}`,controller.signal);
          if(lifetime.signal.aborted||controller.signal.aborted||id!==requestId)return;
          const lastPage=Math.max(1,Math.ceil(data.total/data.page_size));
          if(page>lastPage){await loadPage(lastPage,keyword);return;}
          currentPage=data.page;hasMore=data.has_more;
          summary.textContent=`${location?"本货位":"仓库"}已定价金额 ¥${data.inventory_value}${data.missing_lots?` · ${data.missing_lots} 批待核价`:""}`;
          basis.textContent=`${data.basis}。上方为${location?"本货位":"全部可见库存"}合计，不随下面筛选或翻页改变。`;
          pageLabel.textContent=`第 ${currentPage} / ${lastPage} 页 · ${data.total} 批${keyword?"符合筛选":""}`;
          renderRows(data);
        }catch(error){
          if(lifetime.signal.aborted||controller.signal.aborted||id!==requestId)return;
          if(error.status===401||error.status===403){root.replaceChildren();return;}
          const retry=el("button","重试");retry.type="button";retry.addEventListener("click",()=>loadPage(page,keyword));
          list.replaceChildren(el("div","读取失败，请重试"),retry);
        }finally{
          if(!lifetime.signal.aborted&&!controller.signal.aborted&&id===requestId)setBusy(false);
        }
      };
      previous.addEventListener("click",()=>{if(!busy&&currentPage>1)loadPage(currentPage-1);});
      next.addEventListener("click",()=>{if(!busy&&hasMore)loadPage(currentPage+1);});
      search.addEventListener("input",()=>{
        clearTimeout(this.searchTimer);this.pageController?.abort();++requestId;
        setBusy(true);list.replaceChildren(el("div","正在筛选…"));
        this.searchTimer=setTimeout(()=>loadPage(1),250);
      });
      await loadPage(1);
    }catch(error){
      if(!lifetime.signal.aborted)root.replaceChildren();
    }
  }
}
if(!customElements.get("warehouse-costs"))customElements.define("warehouse-costs",WarehouseCosts);
