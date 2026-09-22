(function (global) {
  'use strict';
  global.ERPProductionMap = {install(app) { app.mixin({
    data() { return this.$parent ? {} : {productionMap: null}; },
    mounted() { if (!this.$parent) global.addEventListener('message', this.acceptProductionMapMessage); },
    beforeUnmount() { if (!this.$parent) global.removeEventListener('message', this.acceptProductionMapMessage); },
    watch: {authGeneration() { this.productionMap = null; }},
    methods: {
      productionPlacementLocationClickable(row) {
        return this.pageAllowed('warehouse') && Number(row?.inventory_lot_id) > 0 && Number(row?.warehouse_location_id) > 0;
      },
      async showProductionMap(row, placement = false) {
        if (this.productionMap || this.productionBusy || !this.pageAllowed('warehouse')) return;
        if (placement && (!row.can_place || !this.canWarehouseExecute || !this.canProductionExecute)) return;
        const token = this.stockOperationKey();
        this._productionMapFocus = document.activeElement;
        // Keep the production view mounted, including filters, page and table scroll.
        this._productionMapScroll = [...document.querySelectorAll('.main, .production-table-wrap, .table-wrap')].map(el => [el, el.scrollTop, el.scrollLeft]);
        this.productionMap = {token, row, placement, auth: this.authGeneration, url: '', error: '', target: null, checking: false, ready: false,
          context:{tab:this.productionTab, historyPage:this.pages.productionHistory, placementPage:this.pages.productionPlacement}};
        const session = this.productionMap;
        try {
          const lotId = row.current_inventory_lot_id || row.inventory_lot_id;
          const {data: lot} = await axios.get('/api/warehouse/lots/' + Number(lotId));
          if (this.productionMap !== session || session.auth !== this.authGeneration) return;
          const location = lot.location;
          if (!location?.id || ![1,3,4].includes(Number(location.warehouse_floor)) || Number(lot.quantity_available) + Number(lot.quantity_reserved) + Number(lot.quantity_damaged) <= 0) {
            throw new Error('这批成品已无当前实物库存或位置尚未确定，请返回刷新核对');
          }
          const params = new URLSearchParams({embedded:'1', source:placement ? 'production-location-picker' : 'production-map',
            tab:'map', floor:String(location.warehouse_floor)+'F', mode:'lookup', view:'2d',
            location_id:String(location.id), lot_id:String(lotId), picker_token:token, production_return:'1'});
          if (placement) params.set('readonly', '1');
          session.url = '/warehouse.html?' + params;
        } catch (e) { if (this.productionMap === session) session.error = this.errorMessage(e); }
      },
      requestProductionMapReturn() {
        const session = this.productionMap;
        if (!session || this.productionBusy || session.checking) return;
        if (session.placement || !session.url || !session.ready || session.error) { this.finishProductionMapReturn(); return; }
        this.$refs.productionMapFrame?.contentWindow.postMessage({type:'erp-production-map-return-request', token:session.token}, global.location.origin);
      },
      async finishProductionMapReturn() {
        const session = this.productionMap;
        if (!session || this.productionBusy) return;
        this.productionMap = null;
        if (session.auth !== this.authGeneration) return;
        this.productionTab = session.context.tab;
        this.pages.productionHistory = session.context.historyPage;
        this.pages.productionPlacement = session.context.placementPage;
        if (!session.placement && session.auth === this.authGeneration) {
          await Promise.all([this.loadProductionHistory(), this.loadProductionPlacement(), this.loadKpi()]);
        }
        await this.$nextTick();
        if (session.auth !== this.authGeneration) return;
        for (const [el, top, left] of this._productionMapScroll || []) if (el.isConnected) {el.scrollTop=top; el.scrollLeft=left;}
        this._productionMapFocus?.isConnected && this._productionMapFocus.focus({preventScroll:true});
      },
      async acceptProductionMapMessage(event) {
        const session = this.productionMap;
        if (!session || event.origin !== global.location.origin || event.source !== this.$refs.productionMapFrame?.contentWindow
          || event.data?.token !== session.token || session.auth !== this.authGeneration || !this.pageAllowed('warehouse')) return;
        if (event.data.type === 'erp-production-map-ready') { session.ready = true; return; }
        if (event.data.type === 'erp-production-location-preview' && session.placement) {
          session.selectedId = Number(event.data.location_id);
          if (session.target?.id !== session.selectedId) session.target = null;
          return;
        }
        if (event.data.type === 'erp-production-map-return') { this.finishProductionMapReturn(); return; }
        if (event.data.type !== 'erp-production-location' || !session.placement || session.checking || this.productionBusy
          || !this.canWarehouseExecute || !this.canProductionExecute || !session.row.can_place) return;
        const id = Number(event.data.location_id);
        if (!Number.isInteger(id) || id <= 0) return;
        session.selectedId = id;
        session.checking = true; session.error = ''; session.target = null;
        try {
          if (!await this.ensureProductionLocations({force:true})) throw new Error('货位核对失败，请重新选择');
          if (this.productionMap !== session || session.auth !== this.authGeneration) return;
          if (session.selectedId !== id) return;
          const location = this.productionLocation(id);
          if (!location || Number(location.id) === Number(session.row.warehouse_location_id)) throw new Error('此货位不能作为归位目标，请选择可用成品货位');
          if (!location.layout_version || Number(location.layout_version) !== Number(event.data.layout_version)) throw new Error('货位布局已变化，请刷新地图后重新选位');
          session.target = {...location};
        } catch (e) { if (this.productionMap === session) session.error = this.errorMessage(e); }
        finally { session.checking = false; }
      },
      async confirmProductionMapPlacement() {
        const session = this.productionMap;
        if (!session?.placement || !session.target || session.checking || this.productionBusy || session.auth !== this.authGeneration
          || !session.row.can_place || !this.canWarehouseExecute || !this.canProductionExecute) return;
        // Freeze the selected layout version; the existing endpoint validates it at commit.
        const row = {...session.row, transfer_location_id:session.target.id};
        const saved = await this.transferProductionCompletionToStock(row, session.target);
        if (saved && this.productionMap === session) await this.finishProductionMapReturn();
      },
    }
  }); }};
})(window);
