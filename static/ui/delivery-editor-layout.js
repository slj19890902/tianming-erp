/* Delivery editor presentation. Existing save/print commands remain authoritative. */
(function(global) {
  'use strict';
  function install(app) {
    app.component('delivery-customer-select', {
      props:{modelValue:[String,Number],options:{type:Array,default:()=>[]},disabled:Boolean},
      emits:['update:modelValue'],
      data(){return {query:'',open:false};},
      computed:{
        selected(){return !!this.modelValue;},
        selectedLabel(){return this.nameLabel(this.options.find(row=>String(row.id)===String(this.modelValue)));},
        widthLabel(){return this.selectedLabel || this.options.map(row=>this.nameLabel(row)).sort((a,b)=>this.labelWidth(b)-this.labelWidth(a))[0] || '选择客户或输入关键字';},
        matches(){const q=this.query.trim().toLowerCase();return this.options.filter(row=>!q || String(row._delivery_label || '').toLowerCase().includes(q));},
      },
      watch:{modelValue(){this.open=false;this.query='';},disabled(value){if(value)this.open=false;}},
      methods:{
        nameLabel(row){return row?.name ? [row.customer_code,row.name].filter(Boolean).join(' · ') : String(row?._delivery_label || '').split('｜')[0];},
        labelWidth(text){return Array.from(text).reduce((n,c)=>n+(/[^\x00-\xff]/.test(c)?2:1),0);},
        show(){if(!this.disabled && !this.selected)this.open=true;},
        choose(row){if(this.disabled || this.selected)return;this.open=false;this.query='';this.$emit('update:modelValue',row.id);},
        clear(){if(this.disabled)return;this.open=false;this.query='';this.$emit('update:modelValue','');},
      },
      template:`<div class="delivery-customer-select search-select"><span class="delivery-customer-measure" aria-hidden="true">{{ widthLabel }}</span>
        <input class="input" aria-label="选择客户或输入关键字" :value="selected?selectedLabel:query" :readonly="selected" :disabled="disabled" placeholder="选择客户或输入关键字" @focus="show" @click="show" @input="query=$event.target.value;show()" @blur="open=false" @keydown.esc="open=false" @keydown.down.prevent="show" @keydown.enter.prevent="open && matches.length && choose(matches[0])" />
        <button v-if="selected" type="button" class="delivery-customer-control" aria-label="清除已选客户" :disabled="disabled" @click="clear">×</button>
        <button v-else type="button" class="delivery-customer-control" aria-label="展开客户列表" :aria-expanded="open" :disabled="disabled" @mousedown.prevent @click="open=!open">▾</button>
        <div v-if="open && !selected && !disabled" class="search-select-list" role="listbox" aria-label="可送货客户">
          <button v-for="row in matches" :key="row.id" type="button" class="search-select-option" role="option" @mousedown.prevent @click="choose(row)">{{ nameLabel(row) }}</button>
          <div v-if="!matches.length" class="empty">没有匹配客户</div>
        </div>
      </div>`,
    });
    app.mixin({
      data() { return this.$parent ? {} : {deliveryLocationClock:Date.now(), deliveryEditorPage:1, deliveryEditorPageSize:8, deliveryEditorExitVisible:false, deliveryEditorExitApproved:false}; },
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
            // Candidate page size is fixed for an open picker session. Loading and
            // heterogeneous row contents must never initiate another API request.
            if(!secondary) {
              this.deliveryEditorPageSize=size;
              this.deliveryEditorPage=Math.min(this.deliveryEditorPage,this.deliveryEditorPages);
            }
          };
          fit();
          if(global.ResizeObserver) { this._deliveryEditorObserver=new global.ResizeObserver(fit);this._deliveryEditorObserver.observe(area); }
        },
        prepareDeliveryBatchPagination() {
          const area=this.$refs.businessModal?.querySelector('.delivery-rows-viewport');
          if(!area || !Number.isFinite(area.clientHeight) || area.clientHeight<=0) return;
          // Use a stable row allowance before loading, never a transient data row.
          const size=Math.min(50,Math.max(1,Math.floor((area.clientHeight-112)/48)));
          const picker=this.deliveryBatchPicker;
          if(size===picker.page_size) return;
          const first=(Math.max(1,Number(picker.page || 1))-1)*picker.page_size;
          picker.page_size=size;
          picker.page=Math.floor(first/size)+1;
          picker.items=[];
        },
        receiveDeliveryLocationPolicy(row) {
          if(row?.location_retention) row.location_retention._receivedAt=Date.now();
          return row;
        },
        deliveryLocationRemaining(row) {
          const policy=row?.location_retention;
          if(!policy?.expires_at || !policy.visible) return 0;
          const initial=(Date.parse(policy.expires_at)-Date.parse(policy.evaluated_at))/1000;
          const elapsed=Math.max(0,(this.deliveryLocationClock-(policy._receivedAt ?? this.deliveryLocationClock))/1000);
          return Math.max(0,Math.ceil(initial-elapsed));
        },
        deliveryVisibleLocations(row,line) {
          const policy=row?.location_retention;
          if(policy && (!policy.visible || (policy.expires_at && !this.deliveryLocationRemaining(row)))) return [];
          return this.deliveryLineLocations(line);
        },
        deliveryLocationCountdown(row) {
          const seconds=this.deliveryLocationRemaining(row);
          if(!seconds) return '';
          const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60;
          return '货位保留 '+[h,m,s].map(value=>String(value).padStart(2,'0')).join(':');
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
        async deliveryEditorWithdraw() {
          if(this.deliveryEditorBusy) return false;
          if(this.deliveryFormIsDirty()) { this.showToast('请先保存当前修改，再撤回发货',true);return false; }
          const form=this.deliveryForm, modal=this.modal;
          const row=this.currentDeliveryDraftRow();
          if(!await this.cancelDelivery(row)) return false;
          if(this.deliveryForm!==form || this.modal!==modal) return true;
          return this.editDelivery(row);
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
      mounted() { if(!this.$parent && global.setInterval) this._deliveryLocationTimer=global.setInterval(()=>{this.deliveryLocationClock=Date.now();},1000); },
      beforeUnmount() { this._deliveryEditorObserver?.disconnect();if(this._deliveryLocationTimer) global.clearInterval(this._deliveryLocationTimer); },
    });
  }
  global.TmDeliveryEditorLayout={install};
})(window);
