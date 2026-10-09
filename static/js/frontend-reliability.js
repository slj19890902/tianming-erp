(() => {
  'use strict';
  const forms = {
    customer:'customerForm', material:'materialForm', supplier:'supplierForm',
    order:'orderForm', orderEdit:'orderEditForm', orderItem:'orderItemForm',
    delivery:'deliveryForm', receipt:'receiptForm', requisition:'requisitionForm',
    supplierRequisitionDraft:'supplierRequisitionDraft', stockReplenishment:'stockReplenishmentForm',
    quotationConvert:'quotationConvertForm',
  };
  const serialize = value => JSON.stringify(value, (key, val) =>
    key.startsWith('_') || ['loading','error','saving'].includes(key) ? undefined : val);
  window.ERPFrontendReliability = {
    install(vm) {
      // Bound reads only; write retries remain owned by each business operation.
      window.axios?.interceptors?.request?.use(config => {
        if (['get','head'].includes(String(config.method || 'get').toLowerCase()) && !config.timeout) config.timeout=30000;
        return config;
      });
      const baselines = new Map(), identities = new Map();
      let session = '';
      const states = () => [
        vm.orderCreateSaveState, vm.orderGroupSaveState, vm.orderItemSaveState, vm.deliverySaveState,
        vm.supplierRequisitionSaveState, vm.compositeRequisitionSaveState, vm.stockReplenishmentSaveState,
        vm.quotationConversionState,
      ].filter(Boolean);
      const surfaces = () => {
        const values = {};
        const type = vm.modal?.type;
        if (forms[type]) values.modal = {identity:vm.modal, value:vm[forms[type]]};
        if (vm.contractCustomer) values.contract = {identity:vm.contractDraft, value:vm.contractDraft};
        if (vm.quotationCustomer) values.quotation = {identity:vm.quotationDraft, value:vm.quotationDraft};
        if (vm.productionEntry) values.production = {identity:vm.productionEntry, value:vm.productionEntry};
        for (const key of ['stockPrepDialog','stockAssemblyDialog']) {
          const d=vm[key];
          if (d && !d.loading) values[key] = {identity:d, value:{
            sets:d.sets,disposition:d.disposition,location:d.location,keepKind:d.keepKind,
            inputQuantity:d.inputQuantity,actualOutput:d.actualOutput,quantity:d.quantity,
            actual:d.job?._actual,jobs:(d.jobs || []).map(job=>({id:job.id,actual:job._actual}))
          }};
        }
        return values;
      };
      const synchronize = () => {
        const currentSession = String(vm.user?.id || '') + ':' + String(vm.authGeneration || 0);
        if (session !== currentSession) { session=currentSession;baselines.clear();identities.clear(); }
        const current = surfaces();
        for (const key of [...identities.keys()]) if (!current[key]) { identities.delete(key); baselines.delete(key); }
        for (const [key, item] of Object.entries(current)) {
          if (identities.get(key) !== item.identity) { identities.set(key,item.identity); baselines.set(key,serialize(item.value)); }
        }
      };
      const state = () => {
        synchronize();
        if (!vm.user) return {dirty:false,saving:false,uncertain:false};
        const pending = states();
        const uncertain = pending.some(s => s.outcomeUncertain);
        const saving = !!vm.masterSavePending || pending.some(s => s.saving)
          || (['contracts','quotations'].includes(vm.activePage) && !!vm.loading)
          || !!vm.contractAction?.action || !!vm.receiptOperationState?.action
          || !!vm.stockPrepBusy || !!vm.stockPrepDialog?.saving || !!vm.stockAssemblyDialog?.saving;
        let dirty = Object.entries(surfaces()).some(([key,item]) => serialize(item.value) !== baselines.get(key));
        if (vm.modal?.type === 'product') dirty ||= !!vm._productFormDirty?.() || !!vm._productBomDirty?.();
        // Uploaded/imported content is work even before the first typed edit.
        if (vm.modal?.type === 'orderPdfImport') dirty ||= (vm.orderImportDrafts || []).some(d => !d._saved && !d.saved_order_id);
        return {dirty:!!dirty,saving:!!saving,uncertain:!!uncertain};
      };
      this.state = state;
      this.acceptDraft = key => { const item=surfaces()[key]; if (item) {identities.set(key,item.identity); baselines.set(key,serialize(item.value));} };
      synchronize();
      // Capture each form after its open/hydration batch, before the next input.
      const stop = vm.$watch(() => Object.entries(surfaces()).map(([key,item]) => [key,item.identity]), synchronize, {flush:'post'});
      const protect = event => {
        const current = state();
        if (!current.dirty && !current.saving && !current.uncertain) return;
        event.preventDefault(); event.returnValue = '';
      };
      window.addEventListener('beforeunload', protect);
      window.addEventListener('pagehide', () => {stop();window.removeEventListener('beforeunload',protect);}, {once:true});
    }
  };
  const style = document.createElement('style');
  style.textContent = '.save-recovery{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 12px;margin:8px 0;border:1px solid #f2c46d;border-radius:6px;background:#fff8e8;color:#7c4400}.save-recovery summary{cursor:pointer}.save-recovery span{overflow-wrap:anywhere}';
  document.head.appendChild(style);
})();
