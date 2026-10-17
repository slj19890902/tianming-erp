/* Delivery editor presentation. Existing save/print commands remain authoritative. */
(function(global) {
  'use strict';
  function install(app) {
    app.mixin({
      data() { return this.$parent ? {} : {deliveryEditorPage:1, deliveryEditorPageSize:8, deliveryEditorExitVisible:false, deliveryEditorExitApproved:false}; },
      computed: {
        deliveryEditorLines() { return this.$parent ? [] : this.deliveryUnifiedLines(); },
        deliveryEditorPages() { return Math.max(1, Math.ceil(this.deliveryEditorLines.length / this.deliveryEditorPageSize)); },
        deliveryEditorPageLines() { return this.deliveryEditorLines.slice((this.deliveryEditorPage-1)*this.deliveryEditorPageSize,this.deliveryEditorPage*this.deliveryEditorPageSize); },
        deliveryEditorBusy() { return !!(this.deliverySaveState.saving || this.deliverySaveState.outcomeUncertain || this.deliveryDraftRecoveryState.checking || this.deliveryOperationState.action); },
      },
      watch: {
        'modal.type'(value) {
          if(this.$parent) return;
          this.deliveryEditorExitVisible=false;
          this.deliveryEditorExitApproved=false;
          this.deliveryEditorPage=1;
          this.$nextTick(()=>this.observeDeliveryEditor());
        },
        'deliveryBatchPicker.visible'() { if(!this.$parent) this.$nextTick(()=>this.observeDeliveryEditor()); },
        'deliveryEditorLines.length'() { if(this.$parent) return; this.deliveryEditorPage=Math.min(this.deliveryEditorPage,this.deliveryEditorPages); },
      },
      methods: {
        observeDeliveryEditor() {
          this._deliveryEditorObserver?.disconnect();
          if(this.modal?.type!=='delivery') return;
          const area=this.$refs.businessModal?.querySelector('.delivery-rows-viewport');
          if(!area) return;
          const fit=()=>{
            if(this.modal?.type!=='delivery' || !area.isConnected || !Number.isFinite(area.clientHeight) || area.clientHeight<=0) return;
            const secondary=!!this.deliveryBatchPicker.visible;
            const row=area.querySelector('tbody tr[data-product-row]');
            const rowHeight=Math.max(40,row?.getBoundingClientRect().height || 44);
            const overhead=secondary?112:62;
            const size=Math.min(50,Math.max(1,Math.floor((area.clientHeight-overhead)/rowHeight)));
            if(secondary) {
              if(size!==this.deliveryBatchPicker.page_size) {
                this.deliveryBatchPicker.page_size=size;
                this.loadDeliveryBatchItems(1);
              }
            } else {
              this.deliveryEditorPageSize=size;
              this.deliveryEditorPage=Math.min(this.deliveryEditorPage,this.deliveryEditorPages);
            }
          };
          fit();
          if(global.ResizeObserver) { this._deliveryEditorObserver=new global.ResizeObserver(fit);this._deliveryEditorObserver.observe(area); }
        },
        deliveryCustomerName(id) {
          const customer=(this.customers || []).find(row=>Number(row.id)===Number(id));
          const candidate=(this.deliveryCustomerCandidates || []).find(row=>Number(row.customer_id)===Number(id));
          return customer?.name || candidate?.customer_name || '';
        },
        deliveryEditorSequence(index) { return (this.deliveryEditorPage-1)*this.deliveryEditorPageSize+index+1; },
        deliverySelectionSequence(index) { return (this.deliveryBatchPicker.page-1)*this.deliveryBatchPicker.page_size+index+1; },
        deliveryDetailColumnCount(row) { return 8+(this.deliveryShowsCategory(row)?1:0)+(this.canViewSalesAmounts?2:0); },
        deliverySelectionPrice(item) { return item.source_type==='unordered_finished'?this.unorderedFinishedSelection(item):item; },
        deliverySelectionStock(item) {
          if(item.source_type==='unordered_finished') return this.unorderedFinishedAvailableQuantity(item);
          const sources=item.inventory_sources || [];
          // deliverable_quantity remains the existing authoritative customer-unit limit.
          return item.available_customer_quantity ?? item.deliverable_quantity ?? '';
        },
        deliverySelectedQuantityTotal() {
          const orders=Object.values(this.deliveryBatchPicker.selected || {});
          const stock=Object.values(this.unorderedFinishedPicker.selected || {}).filter(row=>row._selected);
          return [...orders,...stock].reduce((sum,row)=>sum+Number(row._quantity || 0),0);
        },
        deliveryEditorReturn() {
          this.deliveryBatchPicker.visible=false;
          this.deliveryEditorExitVisible=false;
        },
        async deliveryEditorPrint() {
          if(this.deliveryEditorBusy || this.deliveryValidationError) return false;
          if(!this.deliveryForm.editingId || this.deliveryFormIsDirty()) {
            await this.saveModal();
            if(this.modal?.type!=='delivery' || !this.deliveryForm.editingId || this.deliveryFormIsDirty() || this.deliverySaveState.outcomeUncertain) return false;
          }
          return this.printCurrentDeliveryDraft();
        },
        async deliveryEditorSaveAndExit() {
          if(this.deliveryEditorBusy) return;
          await this.saveModal();
          if(this.modal?.type==='delivery' && this.deliveryForm.editingId && !this.deliveryFormIsDirty() && !this.deliverySaveState.outcomeUncertain) this.deliveryEditorDiscardAndExit();
        },
        deliveryEditorDiscardAndExit() {
          if(this.deliveryEditorBusy) return;
          this.deliveryEditorExitApproved=true;
          this.deliveryEditorExitVisible=false;
          this.closeModal();
          this.deliveryEditorExitApproved=false;
        },
      },
      beforeUnmount() { this._deliveryEditorObserver?.disconnect(); },
    });
  }
  global.TmDeliveryEditorLayout={install};
})(window);
