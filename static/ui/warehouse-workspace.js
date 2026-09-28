/* Warehouse views share the ERP shell. Business writes remain inside their original view. */
(function (global) {
  'use strict';
  const paths = new Set(['/warehouse.html', '/warehouse-ledger.html']);
  function route(value, origin) {
    try {
      const url = new URL(value, origin);
      if (url.origin !== origin || !paths.has(url.pathname) || url.username || url.password) return null;
      url.searchParams.set('embedded', '1');
      return {view:url.pathname === '/warehouse.html' ? 'map' : 'ledger', url:url.pathname + url.search,
        tab:url.searchParams.get('tab') || 'finished'};
    } catch (_) { return null; }
  }
  function install(app) {
    app.mixin({
      data() { return this.$parent ? {} : {warehouseView:'map', warehouseLedgerUrl:'', warehouseLedgerTab:'finished', warehouseInventoryTab:'finished',
        warehouseContext:{q:'',search_floor:null}, warehouseNavigationError:'', warehouseNavigating:false}; },
      mounted() { if (!this.$parent) global.addEventListener('message', this.acceptWarehouseWorkspaceMessage); },
      beforeUnmount() { if (!this.$parent) { global.removeEventListener('message', this.acceptWarehouseWorkspaceMessage); this.resetWarehouseWorkspace(); } },
      watch:{
        authGeneration() { this.resetWarehouseWorkspace(); },
        uiMode() { this.sendWarehouseActivation('map', true); this.sendWarehouseActivation('ledger', true); },
      },
      methods:{
        warehouseStocktakeVisible() { return this.hasPermission('warehouse.stocktake.view') || this.hasPermission('warehouse.stocktake.review'); },
        leaveWarehouseWorkspaceUrl(page) {
          const url = new URL(global.location.href);
          url.searchParams.set('page',page);
          for (const key of [...url.searchParams.keys()]) if (key.startsWith('warehouse_')) url.searchParams.delete(key);
          global.history.replaceState(global.history.state,'',url.pathname + url.search + url.hash);
        },
        warehouseNavigationMenus(eligible) {
          const menus = this.applyEffectiveLayout('menus',eligible);
          const warehouse = eligible.find(item => item.key === 'warehouse');
          const configured = this.uiLayoutEffective?.layout?.menus;
          // Published older layouts predate the standalone warehouse menu.
          // Add only this new entry; never resurrect other hidden menu entries.
          if (warehouse && !menus.some(item => item.key === 'warehouse') && !configured?.some(item => item.id === 'warehouse')) {
            const result = [...menus]; const after = result.findIndex(item => item.key === 'production');
            result.splice(after < 0 ? Math.min(2,result.length) : after + 1,0,warehouse); return result;
          }
          return menus;
        },
        warehouseWindow(view = this.warehouseView) {
          return this.$refs[view === 'map' ? 'warehouseFrame' : 'warehouseLedgerFrame']?.contentWindow;
        },
        resetWarehouseWorkspace() {
          this._warehousePending?.finish(false);
          this._warehousePending = null; this._warehouseReady = {};
          this._warehouseActivation = {}; this.warehouseLedgerUrl = ''; this.warehouseFrameUrl = '';
          this.warehouseContext = {q:'',search_floor:null}; this.warehouseNavigationError = ''; this.warehouseView = 'map'; this.warehouseInventoryTab = 'finished';
        },
        initializeWarehouseWorkspace(target = '') {
          if (!this.pageAllowed('warehouse')) return;
          if (!target && (this.warehouseFrameUrl || this.warehouseLedgerUrl)) return;
          const query = new URLSearchParams(global.location.search);
          let initial = target || query.get('warehouse_target');
          if (!initial) {
            let saved = 'map';
            try { saved = global.localStorage.getItem('tm-warehouse-view:' + this.user?.id) || 'map'; } catch (_) {}
            const requested = query.get('warehouse_view');
            const legacyMapIntent = ['2d','25d'].includes(requested) || ['warehouse_floor','warehouse_mode','warehouse_action'].some(key => query.has(key));
            const display = query.get('warehouse_display') || requested;
            const view = legacyMapIntent ? 'map' : ['map','list'].includes(display) ? display : saved;
            if (view === 'list') initial = '/warehouse-ledger.html?tab=finished';
            else {
              const floor = String(query.get('warehouse_floor') || '3F').toUpperCase();
              const params = new URLSearchParams({floor:this.isWarehouseTwinFloorCode(floor) ? floor : '3F', view:requested === '25d' ? '25d' : '2d'});
              if (['lookup','move','planning'].includes(query.get('warehouse_mode'))) params.set('mode', query.get('warehouse_mode'));
              if (['relocate','stocktake','merge','ground'].includes(query.get('warehouse_action'))) params.set('action', query.get('warehouse_action'));
              initial = '/warehouse.html?' + params;
            }
          }
          this.activateWarehouseRoute(initial);
        },
        sendWarehouseActivation(view = this.warehouseView, appearanceOnly = false) {
          const url = this._warehouseActivation?.[view] || (view === 'map' ? this.warehouseFrameUrl : this.warehouseLedgerUrl);
          if (!url || !this._warehouseReady?.[view]) return;
          this.warehouseWindow(view)?.postMessage({source:'tianming-erp-shell',type:'warehouse-workspace-command',command:'activate',url:appearanceOnly ? (view === 'map' ? '/warehouse.html?embedded=1' : '/warehouse-ledger.html?embedded=1') : url,ui_mode:this.uiMode},global.location.origin);
        },
        activateWarehouseRoute(value) {
          const next = route(value, global.location.origin);
          if (!next || !this.pageAllowed('warehouse')) return false;
          if (next.view === 'ledger' && next.tab === 'stocktake_review' && !this.warehouseStocktakeVisible()) {
            this.warehouseNavigationError = '当前账号没有盘点记录查看权限';
            if (!this.warehouseFrameUrl && !this.warehouseLedgerUrl) this.warehouseFrameUrl = '/warehouse.html?embedded=1';
            return false;
          }
          this.warehouseNavigationError = ''; this.warehouseView = next.view;
          if (next.view === 'ledger') { this.warehouseLedgerTab = next.tab; if (['finished','semi_finished'].includes(next.tab)) this.warehouseInventoryTab = next.tab; }
          this._warehouseActivation = {...this._warehouseActivation,[next.view]:next.url};
          if (next.view === 'map' && !this.warehouseFrameUrl) this.warehouseFrameUrl = next.url;
          if (next.view === 'ledger' && !this.warehouseLedgerUrl) this.warehouseLedgerUrl = next.url;
          this.activePage = 'warehouse';
          this.$nextTick(() => this.sendWarehouseActivation(next.view));
          try {
            if (next.view === 'map' || next.tab === 'finished') global.localStorage.setItem('tm-warehouse-view:' + this.user?.id, next.view === 'map' ? 'map' : 'list');
            const url = new URL(global.location.href);
            url.searchParams.set('page','warehouse'); url.searchParams.set('warehouse_target',next.url);
            global.history.replaceState(global.history.state,'',url.pathname + url.search + url.hash);
          } catch (_) {}
          return true;
        },
        async chooseWarehouseView(view, tab = null) {
          tab = tab || this.warehouseInventoryTab || 'finished';
          const params = new URLSearchParams(view === 'map' ? {} : {tab});
          if ((view === 'map' || ['finished','semi_finished'].includes(tab)) && this.warehouseContext.q !== undefined) params.set('q',this.warehouseContext.q);
          if ((view === 'map' || ['finished','semi_finished'].includes(tab)) && this.warehouseContext.search_floor) params.set('search_floor',this.warehouseContext.search_floor);
          const target = (view === 'map' ? '/warehouse.html' : '/warehouse-ledger.html') + '?' + params;
          if (await this.checkWarehouseNavigation(target)) this.activateWarehouseRoute(target);
        },
        checkWarehouseNavigation(target = '') {
          if (this.activePage !== 'warehouse' || !this.warehouseWindow()) return Promise.resolve(true);
          if (this._warehousePending) return Promise.resolve(false);
          const source = this.warehouseWindow();
          this._warehouseSequence = (this._warehouseSequence || 0) + 1;
          const requestId = 'warehouse-' + this._warehouseSequence;
          const auth = this.authGeneration;
          const url = target || (this.warehouseView === 'map' ? '/warehouse.html' : '/warehouse-ledger.html');
          this.warehouseNavigating = true; this.warehouseNavigationError = '';
          return new Promise(resolve => {
            const finish = allowed => {
              if (this._warehousePending?.id !== requestId) return;
              global.clearTimeout(this._warehousePending.timer);
              this._warehousePending = null; this.warehouseNavigating = false;
              resolve(Boolean(allowed && auth === this.authGeneration));
            };
            const timer = global.setTimeout(() => {
              this.warehouseNavigationError = '当前页面尚未确认可以离开，请等待加载或处理未完成的操作后重试';
              finish(false);
            },8000);
            this._warehousePending = {id:requestId,source,auth,url,finish,timer};
            source.postMessage({source:'tianming-erp-shell',type:'warehouse-workspace-command',command:'navigate-request',request_id:requestId,url},global.location.origin);
          });
        },
        async refreshWarehouseWorkspace() {
          if (!await this.checkWarehouseNavigation()) return;
          this.warehouseWindow()?.postMessage({source:'tianming-erp-shell',type:'warehouse-workspace-command',command:'refresh',ui_mode:this.uiMode},global.location.origin);
        },
        acceptWarehouseWorkspaceMessage(event) {
          if (!this.user || !this.pageAllowed('warehouse') || event.origin !== global.location.origin) return;
          const view = event.source === this.warehouseWindow('map') ? 'map' : event.source === this.warehouseWindow('ledger') ? 'ledger' : null;
          if (!view) return;
          const data = event.data || {};
          if (data.source !== 'tianming-warehouse') return;
          if (data.type === 'warehouse-workspace-context') {
            const first = data.ready && !this._warehouseReady?.[view];
            this._warehouseReady = {...this._warehouseReady,[view]:Boolean(data.ready || this._warehouseReady?.[view])};
            if (view === this.warehouseView) {
              if (view === 'map' || ['finished','semi_finished'].includes(data.tab)) {
                if (typeof data.q === 'string') this.warehouseContext = {...this.warehouseContext,q:data.q.slice(0,500)};
                if (['ALL','1F','3F','4F','UNLOCATED'].includes(data.search_floor) && (data.scope_changed || !this.warehouseContext.search_floor)) {
                  this.warehouseContext = {...this.warehouseContext,search_floor:data.search_floor};
                }
              }
              if (view === 'ledger' && typeof data.tab === 'string') { this.warehouseLedgerTab = data.tab; if (['finished','semi_finished'].includes(data.tab)) this.warehouseInventoryTab = data.tab; }
            }
            if (first) this.sendWarehouseActivation(view);
            return;
          }
          if (data.request_id) {
            const pending = this._warehousePending;
            if (!pending || pending.id !== data.request_id || pending.source !== event.source || pending.auth !== this.authGeneration) return;
            if (data.type === 'warehouse-workspace-blocked') {
              this.warehouseNavigationError = String(data.message || '请先处理当前未完成的操作'); pending.finish(false);
            } else if (data.type === 'warehouse-workspace-navigate' && data.url === pending.url) pending.finish(true);
            return;
          }
          if (view !== this.warehouseView || this.activePage !== 'warehouse' || this._warehousePending) return;
          if (data.type === 'warehouse-workspace-navigate') this.activateWarehouseRoute(data.url);
          if (data.type === 'warehouse-workspace-blocked') this.warehouseNavigationError = String(data.message || '请先处理当前未完成的操作');
        },
      },
    });
  }
  global.ERPWarehouseWorkspace = {install,route};
})(window);
