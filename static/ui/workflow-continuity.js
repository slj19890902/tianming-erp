/* Presentation-only continuity. Never submits, restores form data, or changes filters. */
(function (global) {
  'use strict';
  const forms = {
    order:['orderForm','orderCreateSaveState'], orderEdit:['orderEditForm','orderGroupSaveState'],
    orderItem:['orderItemForm','orderItemSaveState'], delivery:['deliveryForm','deliverySaveState'],
    supplierRequisitionDraft:['supplierRequisitionDraft','supplierRequisitionSaveState'],
    requisition:['requisitionForm','compositeRequisitionSaveState'],
    stockReplenishment:['stockReplenishmentForm','stockReplenishmentSaveState'],
  };
  const readOnly = new Set(['orderDetail','orderGroupDetail','orderTrace']);
  const pages = {orders:'订单',requisition:'报料采购',incoming:'来料',deliveries:'送货',delivery:'送货'};
  // Only editable business inputs: asynchronous cost/stock lookups do not dirty a form.
  const fields = new Set(['customer_id','customer_po','delivery_date','remark','quantity','unit_price','product_id',
    'supplier_groups','_material_supplier','box_style','customer_id','customer_po','delivery_date','flute_type','height_mm','historical_backfill','layer_count','length_mm','material_id','order_date','print_content','product_code','product_name','product_remark','production_notes','production_process','quantity','remark','requisition_strategy','snapshot_crease_left_mm','snapshot_crease_middle_mm','snapshot_crease_right_mm','snapshot_crease_type','snapshot_flap_mm','snapshot_report_length_mm','snapshot_report_notes','snapshot_report_width_mm','snapshot_splice_mode','specification','supplier_name','sync_product','unit_price','width_mm','material_id','items','order_ids','order_item_id','supplier_id','supplier_name','purchase_quantity',
    'requested_qty','delivery_address','address','contact_person','phone','vehicle_number','driver_name',
    'lines','allocations','inventory_lot_id','delivered_quantity','remarks','source_type',
    'external_purchase_quantity','reference_product_id','internal_name','target_inventory_type',
    'report_length_mm','report_width_mm','crease_type','crease_left_mm','crease_middle_mm','crease_right_mm',
    'purchase_total_sheet_qty','order_purpose_sheet_qty','stock_purpose_sheet_qty','requisition_qty',
    'cardboard_len','cardboard_width','dimension_override_acknowledged','quantity_override_acknowledged']);
  function fingerprint(value) {
    function pick(v) {
      if (Array.isArray(v)) return v.map(pick);
      if (!v || typeof v !== 'object') return v;
      return Object.fromEntries(Object.keys(v).sort().filter(k=>fields.has(k)).map(k=>[k,pick(v[k])]));
    }
    return JSON.stringify(pick(value || {}));
  }
  function status(type, state={}, dirty=false) {
    if (readOnly.has(type)) return {tone:'read',label:'只读查看',detail:'打印记录与发货、回签状态分别保留。'};
    if (!forms[type]) return null;
    const result=state.result || {};
    const success=result.succeeded_ids?.length || 0, failed=result.failed_ids?.length || 0;
    const counts=success || failed ? `本批成功 ${success} 单，未完成 ${failed} 单。` : '';
    if (state.saving) return {tone:'busy',label:'正在保存',detail:'请稍候，不要重复提交。'};
    if (state.outcomeUncertain || state.uncertain) return {tone:'warn',label:'结果待确认',detail:counts+'请先核对原单据结果，不要新建或重复提交。'};
    if (state.committed) return {tone:failed?'warn':'done',label:failed?'部分已保存':'已保存',detail:counts+(failed?'已成功部分不会重复处理；请按原提示核对剩余项。':'正式结果已保存；列表刷新失败时只需重新读取。')};
    return {tone:dirty?'warn':'edit',label:dirty?'有未保存修改':'编辑中',detail:'关闭不会自动保存；以原保存按钮的校验结果为准。'};
  }
  function scope(root) { return [root.activePage,root.requisitionTab,root.incomingTab].join(':'); }
  function install(app) {
    app.mixin({
      data(){return this.$parent?{}:{workflowSelection:{},workflowReturn:null,workflowBaseline:null};},
      computed:{
        workflowStatus(){
          const type=this.modal?.type, pair=forms[type];
          return status(type,pair?this[pair[1]]:{},pair && this.workflowBaseline!==null && fingerprint(this[pair[0]])!==this.workflowBaseline);
        },
        workflowReturnLabel(){return this.workflowReturn && pages[this.activePage] ? `返回${pages[this.activePage]}列表` : '关闭';},
        workflowSourceHint(){
          if(this.modal?.type==='orderItem' && this.orderItemForm?.sync_product) return '已勾选同步常用箱；将按原有权限和确认规则同步所选资料。';
          return ['order','orderItem'].includes(this.modal?.type) ? '本次保存作用于订单；常用箱资料需通过“编辑常用箱”或明确勾选同步后维护。' : '';
        },
      },
      watch:{
        'user.id'(){this.workflowSelection={};this.workflowReturn=null;this.workflowBaseline=null;},
        'modal.type'(type,previous){
          if(this.$parent)return;
          if(type && !previous){
            const main=document.querySelector('.main');
            this.workflowReturn=pages[this.activePage]?{scope:scope(this),top:main?.scrollTop || 0,actor:this.user?.id}:null;
          }
          const pair=forms[type];
          this.workflowBaseline=pair?fingerprint(this[pair[0]]):null;
          if(!type && previous){
            const context=this.workflowReturn;this.workflowReturn=null;
            if(!context || context.scope!==scope(this) || context.actor!==this.user?.id)return;
            this.$nextTick(()=>{
              const main=document.querySelector('.main');if(main)main.scrollTop=context.top;
              const key=this.workflowSelection[context.scope];
              const row=[...document.querySelectorAll('[data-workflow-row]')].find(r=>r.dataset.workflowRow===key);
              // Existing dialog focus restoration wins when its opener still exists.
              if(row && (!document.activeElement || document.activeElement===document.body))row.focus({preventScroll:true});
            });
          }
        },
      },
      methods:{
        workflowRowSelected(key){return this.workflowSelection[scope(this)]===String(key);},
        selectWorkflowRow(key){this.workflowSelection[scope(this)]=String(key);},
        workflowSelectEvent(event){
          if(!pages[this.activePage] || event.target.closest('.modal'))return;
          const row=event.target.closest('[data-workflow-row]');
          if(row)this.selectWorkflowRow(row.dataset.workflowRow);
        },
      },
      mounted(){
        if(this.$parent)return;
        this._workflowSelect=e=>this.workflowSelectEvent(e);
        document.addEventListener('click',this._workflowSelect,true);
        document.addEventListener('focusin',this._workflowSelect,true);
      },
      beforeUnmount(){
        if(this.$parent)return;
        document.removeEventListener('click',this._workflowSelect,true);
        document.removeEventListener('focusin',this._workflowSelect,true);
      },
    });
  }
  global.ERPWorkflowContinuity={install,fingerprint,status,scope};
})(typeof window==='undefined'?globalThis:window);
