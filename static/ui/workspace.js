/* Shared desktop workspace behavior. Never changes business quantities or selections. */
(function (global) {
  'use strict';
  function capacity({height, top, rowHeight, footer = 32, headHeight = 36}) {
    return Math.max(1, Math.min(50, Math.floor((height - top - footer - headHeight) / Math.max(24, rowHeight))));
  }
  function key(root) {
    return [root.activePage, root.uiMode, root.requisitionTab, root.incomingTab, root.productionTab, root.productionPendingSource, root.financeView, root.financeExpensePane, root.orderWorkspace].join(':');
  }
  function install(app) {
    app.mixin({
      data() { return this.$parent ? {} : {workspaceCapacities:{}, workspaceHeight:window.innerHeight}; },
      methods: {
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
          return this.workspaceCapacities?.[key(this)] || Math.max(1, Math.min(fallback,
            capacity({height:this.workspaceHeight || 768, top:large ? 290 : 250, rowHeight:large ? 92 : 64, footer:48})));
        },
        async measureWorkspace(panel) {
          if (this.$parent || this.modal || document.querySelector('.workspace-dialog') || window.innerWidth < 1000 || !panel?.closest?.('.main')) return;
          const tables = [...document.querySelectorAll('.main .panel table')].filter(t => t.getBoundingClientRect().height > 0 && !t.closest('.modal') && !t.dataset.workspaceList);
          const table = panel.querySelector('table');
          if (!table || table !== tables[0] || !['orders','requisition','incoming','production','finance'].includes(this.activePage)) return;
          const rows = [...table.querySelectorAll(':scope > tbody > tr')];
          const records = rows.filter(r => r.cells.length > 1);
          if (!records.length) return;
          const currentKey = key(this);
          // Order contents can have different heights on each page. Freeze the
          // measured capacity for this viewport/mode so paging cannot trigger
          // the resize reload (which intentionally starts at page one).
          // The resize handler clears capacities; uiMode is part of the key.
          if (this.activePage === 'orders' && this.orderWorkspace === 'queue'
              && this.workspaceCapacities[currentKey]) return;
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
          const groupHeight = rows.filter(r=>r.cells.length<=1).reduce((sum,r)=>sum+r.getBoundingClientRect().height,0);
          const next = capacity({height:bottom,top:top + main.scrollTop, rowHeight:rowHeight + groupHeight/records.length,
            footer:after+24, headHeight:table.tHead?.getBoundingClientRect().height || 36});
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
        this.measureLocalTables();
        const panel=[...document.querySelectorAll('.main .panel')].find(p=>p.getBoundingClientRect().height && p.querySelector('table:not([data-workspace-list])'));
        if(panel)this.measureWorkspace(panel);
      });},
      mounted(){if(this.$parent)return;this._workspaceResize=()=>{clearTimeout(this._workspaceResizeTimer);this._workspaceResizeTimer=setTimeout(()=>{this.workspaceHeight=window.innerHeight;this.workspaceCapacities={};this.workspaceLocalSizes={};this.pdfFitCapacity=0;this.$forceUpdate();},200);};window.addEventListener('resize',this._workspaceResize);},
      beforeUnmount(){if(this.$parent)return;window.removeEventListener('resize',this._workspaceResize);clearTimeout(this._workspaceResizeTimer);cancelAnimationFrame(this._workspaceFrame);},
    });
  }
  global.ERPWorkspace = {install,capacity};
})(typeof window === 'undefined' ? globalThis : window);
