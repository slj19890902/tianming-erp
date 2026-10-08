/* Shared desktop workspace behavior. Never changes business quantities or selections. */
(function (global) {
  'use strict';
  function capacity({height, top, rowHeight, footer = 32, headHeight = 36}) {
    return Math.max(1, Math.min(50, Math.floor((height - top - footer - headHeight) / Math.max(24, rowHeight))));
  }
  function key(root) {
    return [root.activePage, root.uiMode, root.requisitionTab, root.incomingTab, root.productionTab, root.productionPendingSource, root.financeView, root.financeExpensePane, root.orderWorkspace, root.productTab].join(':');
  }
  function fitTarget(root) {
    if (root.activePage === 'customers') return 'customers';
    if (root.activePage === 'products' && root.productTab === 'products')
      return root.selectedProductCustomer ? 'products' : 'productCustomers';
    if (root.activePage === 'incoming' && root.incomingTab === 'history') return 'incomingHistory';
    return '';
  }
  function fitKey(root) {
    return [fitTarget(root), root.uiMode, root.activePage === 'products' ? root.selectedProductCustomer?.id : ''].join(':');
  }
  function install(app) {
    app.mixin({
      data() { return this.$parent ? {} : {workspaceCapacities:{}, workspaceFitSizes:{}, workspaceHeight:window.innerHeight}; },
      methods: {
        async measureViewportPage() {
          const target = fitTarget(this);
          if (!target || this.$parent || this.modal || this._viewportPageLoading || this.uiModeSaving
              || window.innerWidth < 1000 || document.querySelector('.workspace-dialog')) return;
          // Cached iframe pages remain mounted. Hidden frames cannot supply layout facts.
          if (window.frameElement && !window.frameElement.getClientRects().length) return;
          if ((target === 'products' && this.productsLoading) || (target === 'incomingHistory' && this.incomingHistoryLoading)) return;
          const table = document.querySelector(`[data-viewport-page="${target}"]`), main = table?.closest('.main');
          if (!main || !table.getBoundingClientRect().height || !table.getBoundingClientRect().width) return;
          const cards = target === 'productCustomers';
          const rows = cards ? [...table.querySelectorAll(':scope > .customer-master-row')]
            : [...table.querySelectorAll(':scope > tbody > tr')].filter(row => row.cells.length > 1);
          if (!rows.length) return;
          const tableRect = table.getBoundingClientRect(), mainRect = main.getBoundingClientRect();
          const top = tableRect.top + main.scrollTop;
          const bottom = Math.min(window.innerHeight, mainRect.bottom);
          // Count actual controls/padding, never unused panel height or the current page's row count.
          let footer = (parseFloat(getComputedStyle(main).paddingBottom) || 0) + 8;
          for (let node = table; node && node !== main; node = node.parentElement) {
            const style = getComputedStyle(node);
            footer += (parseFloat(style.paddingBottom) || 0) + (parseFloat(style.borderBottomWidth) || 0) + (parseFloat(style.marginBottom) || 0);
            for (let sibling = node.nextElementSibling; sibling; sibling = sibling.nextElementSibling) {
              const rect = sibling.getBoundingClientRect(), siblingStyle = getComputedStyle(sibling);
              if (rect.height && !['fixed','absolute'].includes(siblingStyle.position))
                footer += rect.height + (parseFloat(siblingStyle.marginTop) || 0) + (parseFloat(siblingStyle.marginBottom) || 0);
            }
          }
          const headHeight = cards ? 0 : (table.tHead?.getBoundingClientRect().height || 36);
          const id = fitKey(this);
          const layout = [top, bottom, tableRect.width, footer, headHeight].map(Math.round).join(':');
          const prior = this._viewportPageMeasurements?.[id];
          const measuredRow = Math.max(...rows.map(row => row.getBoundingClientRect().height));
          // A short last page cannot enlarge the limit. New geometry/mode can.
          const rowHeight = prior?.layout === layout ? Math.max(prior.rowHeight, measuredRow) : measuredRow;
          const next = capacity({height:bottom, top, rowHeight, footer, headHeight});
          const current = ['customers','productCustomers'].includes(target) ? this.desktopListPageSize()
            : target === 'products' ? this.productListPageSize() : this.incomingListPageSize();
          (this._viewportPageMeasurements ||= {})[id] = {layout, rowHeight};
          if (this.workspaceFitSizes[id] === next) return;
          this.workspaceFitSizes[id] = next;
          if (current === next) return;
          // Keep the first visible record's page when geometry changes while paging.
          const offset = (Math.max(1, Number(this.pages[target] || 1)) - 1) * current;
          this.pages[target] = Math.floor(offset / next) + 1;
          if (cards) return; // The customer selector already has its authorized list locally.
          this._viewportPageLoading = true;
          try {
            if (target === 'incomingHistory') await this.loadIncomingHistory();
            else await this.runExplicitPageListLoad(target);
          } finally {
            this._viewportPageLoading = false;
            this.queueViewportPageMeasure?.();
          }
        },
        queueViewportPageMeasure() {
          cancelAnimationFrame(this._viewportPageFrame);
          this._viewportPageFrame = requestAnimationFrame(() => void this.measureViewportPage());
        },
        measureLocalTables(){
          document.querySelectorAll('table[data-workspace-list]').forEach(table=>{
            if(!table.getBoundingClientRect().height)return;
            const rows=[...table.querySelectorAll(':scope > tbody > tr')].filter(r=>r.cells.length>1);
            if(!rows.length)return;
            const k=table.dataset.workspaceList, dialog=table.closest('.workspace-dialog');
            const after=dialog?Math.max(60,dialog.getBoundingClientRect().bottom-table.getBoundingClientRect().bottom):70;
            const next=capacity({height:innerHeight,top:table.getBoundingClientRect().top,rowHeight:Math.max(...rows.map(r=>r.offsetHeight)),footer:after+30,headHeight:table.tHead?.offsetHeight||36});
            if(!this.workspaceLocalSizes[k]||next<this.workspaceLocalSizes[k])this.workspaceLocalSizes[k]=next;
          });
        },
        screenPageSize(fallback) {
          const large = this.uiMode === 'large';
          if (fitTarget(this)) return this.workspaceFitSizes?.[fitKey(this)] || Math.max(1, Math.min(50, fallback));
          // Incoming rows grow while editing receipt drafts. Keep a fixed
          // mode capacity, never derive pagination from changing row heights.
          if(this.activePage==='incoming')
            return Math.max(4, Math.min(50, fallback));
          if(this.activePage==='products' && this.productTab==='products' && this.selectedProductCustomer)
            return this.workspaceCapacities?.[key(this)] || capacity({height:this.workspaceHeight || 768,top:230,rowHeight:large?64:44,footer:24});
          return this.workspaceCapacities?.[key(this)] || Math.max(1, Math.min(fallback,
            capacity({height:this.workspaceHeight || 768, top:large ? 290 : 250, rowHeight:large ? 92 : 64, footer:48})));
        },
        async measureWorkspace(panel) {
          if (fitTarget(this)) { await this.measureViewportPage(); return; }
          if (this.activePage === 'incoming') return;
          // Invoice tasks and expenses paginate locally. Measuring their stacked
          // tables must never overwrite the server-paged customer list's size.
          if (this.activePage === 'finance' && !['current','collections','settled_history','statements'].includes(this.financeView)) return;
          if (this.activePage === 'production' && this.productionTab === 'pending') return;
          if (this.$parent || this.modal || document.querySelector('.workspace-dialog') || window.innerWidth < 1000 || !panel?.closest?.('.main')) return;
          const tables = [...document.querySelectorAll('.main .panel table')].filter(t => t.getBoundingClientRect().height > 0 && !t.closest('.modal') && !t.dataset.workspaceList);
          const table = panel.querySelector('table');
          if (!table || table !== tables[0] || !['orders','requisition','incoming','production','finance','products'].includes(this.activePage)) return;
          if(this.activePage==='products' && (this.productTab!=='products' || !this.selectedProductCustomer)) return;
          const rows = [...table.querySelectorAll(':scope > tbody > tr')];
          const records = rows.filter(r => r.cells.length > 1);
          if (!records.length) return;
          const currentKey = key(this);
          const financeCustomers = this.activePage === 'finance' && ['current','collections','settled_history'].includes(this.financeView);
          if (financeCustomers && this.workspaceCapacities[currentKey]) return;
          // Order contents can have different heights on each page. Freeze the
          // measured capacity for this viewport/mode so paging cannot trigger
          // the resize reload (which intentionally starts at page one).
          // The resize handler clears capacities; uiMode is part of the key.
          if (this.activePage === 'orders' && this.orderWorkspace === 'queue'
              && this.workspaceCapacities[currentKey]) return;
          if (this.activePage === 'products' && this.workspaceCapacities[currentKey]) return;
          // Production pages have different row heights and expandable controls.
          // Only a real viewport/mode change may recalculate their capacity.
          if (this.activePage === 'production' && this.workspaceCapacities[currentKey]) return;
          const top = table.getBoundingClientRect().top;
          const main = panel.closest('.main');
          const mainRect = main.getBoundingClientRect();
          const bottom = Math.min(window.innerHeight, mainRect.bottom);
          // scrollHeight includes unused viewport space. Only reserve real controls after this table.
          let after=Math.max(0,panel.getBoundingClientRect().bottom-table.getBoundingClientRect().bottom);
          for(let node=panel;node && node!==main;node=node.parentElement){
            for(let sibling=node.nextElementSibling;sibling;sibling=sibling.nextElementSibling){
              const rect=sibling.getBoundingClientRect(),style=getComputedStyle(sibling);
              if(rect.height && !['fixed','absolute'].includes(style.position))after+=rect.height+(parseFloat(style.marginTop)||0)+(parseFloat(style.marginBottom)||0);
            }
          }
          const rowHeight = Math.max(...records.map(r=>r.getBoundingClientRect().height));
          const groupHeight = financeCustomers ? 0 : rows.filter(r=>r.cells.length<=1).reduce((sum,r)=>sum+r.getBoundingClientRect().height,0);
          const next = capacity({height:bottom,top:top + main.scrollTop, rowHeight:rowHeight + groupHeight/records.length,
            // Reserve pagination even when the first result fits on one page.
            footer:(financeCustomers ? Math.max(after,44) : after)+24, headHeight:table.tHead?.getBoundingClientRect().height || 36});
          const previous = this.workspaceCapacities[currentKey];
          // Underfilled last pages must not inflate capacity or repeatedly request themselves.
          if (next === previous || (previous && next > previous && !(this.activePage==='production' && this.productionTab==='history')) || this._workspaceSizing) return;
          this.workspaceCapacities[currentKey] = next;
          this.pageSize = next;
          this._workspaceSizing = true;
          try {
            if(this.activePage==='production' && this.productionTab==='preparation') await this.loadStockPreparation(1);
            else await this.reloadOrdersForUiModeChange('screen-before', 'screen-after');
          } finally { this._workspaceSizing=false; }
        },
      },
    });
    app.component('info-note', {
      props:{label:{type:String,default:'说明'}},
      data:()=>({open:false,position:{}}),
      methods:{
        close(event){if(!event || (!this.$el.contains(event.target) && !this.$refs.body?.contains(event.target)))this.open=false;},
        toggle(){const r=this.$el.getBoundingClientRect();this.position={left:Math.max(8,Math.min(r.left,window.innerWidth-336))+'px',top:Math.max(8,Math.min(r.bottom+6,window.innerHeight-180))+'px'};this.open=!this.open;},
        escape(event){if(event.key==='Escape' && this.open){event.stopImmediatePropagation();this.open=false;this.$refs.button.focus();}},
      },
      mounted(){document.addEventListener('pointerdown',this.close);document.addEventListener('keydown',this.escape,true);},
      beforeUnmount(){document.removeEventListener('pointerdown',this.close);document.removeEventListener('keydown',this.escape,true);},
      template:`<span class="info-note"><button ref="button" type="button" class="info-note-toggle" :aria-label="label" :aria-expanded="open" @click.stop="toggle">ⓘ</button><teleport to="body"><div v-if="open" ref="body" class="info-note-body" role="note" :style="position"><slot></slot></div></teleport></span>`,
    });
    app.component('data-panel', {
      props:['empty','loading','error','loadingText'], emits:['retry'],
      mounted(){this.queueMeasure();}, updated(){this.queueMeasure();},
      beforeUnmount(){cancelAnimationFrame(this._measureFrame);},
      methods:{queueMeasure(){cancelAnimationFrame(this._measureFrame);this._measureFrame=requestAnimationFrame(()=>this.$root.measureWorkspace(this.$el));}},
      template:`<div class="panel" :aria-busy="!!loading"><div v-if="loading" class="workspace-state" role="status"><span class="workspace-spinner"></span>{{loadingText || '正在读取…'}}</div><div v-else-if="error" class="workspace-state" role="alert">{{error}} <button class="btn small" @click="$emit('retry')">重试</button></div><div v-else-if="empty" class="empty">当前没有符合条件的数据</div><div v-else class="table-wrap"><slot></slot></div></div>`,
    });
    app.mixin({
      updated(){if(this.$parent)return;cancelAnimationFrame(this._workspaceFrame);this._workspaceFrame=requestAnimationFrame(()=>{
        this.queueViewportPageMeasure();
        this.measureLocalTables();
        const panel=[...document.querySelectorAll('.main .panel')].find(p=>p.getBoundingClientRect().height && p.querySelector('table:not([data-workspace-list])'));
        if(panel)this.measureWorkspace(panel);
      });},
      mounted(){if(this.$parent)return;this._workspaceResize=()=>{clearTimeout(this._workspaceResizeTimer);this._workspaceResizeTimer=setTimeout(()=>{this.workspaceHeight=window.innerHeight;this.workspaceCapacities={};this.workspaceLocalSizes={};this.pdfFitCapacity=0;this.$forceUpdate();this.queueViewportPageMeasure();},200);};window.addEventListener('resize',this._workspaceResize);
        this._viewportPageVisible = () => this.queueViewportPageMeasure();
        window.addEventListener('erp-workspace-visible',this._viewportPageVisible);
        if (typeof ResizeObserver !== 'undefined') {this._viewportPageObserver = new ResizeObserver(this._viewportPageVisible);this._viewportPageObserver.observe(document.documentElement);}
      },
      beforeUnmount(){if(this.$parent)return;window.removeEventListener('resize',this._workspaceResize);window.removeEventListener('erp-workspace-visible',this._viewportPageVisible);this._viewportPageObserver?.disconnect();clearTimeout(this._workspaceResizeTimer);cancelAnimationFrame(this._workspaceFrame);cancelAnimationFrame(this._viewportPageFrame);},
    });
  }
  global.ERPWorkspace = {install,capacity};
})(typeof window === 'undefined' ? globalThis : window);
