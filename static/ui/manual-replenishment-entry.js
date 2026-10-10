(function (root) {
  'use strict';
  const idsKey = 'manual_replenishment_products', customerKey = 'manual_replenishment_customer';
  const validId = value => Number.isSafeInteger(Number(value)) && Number(value) > 0;
  function products(lines) {
    const result = new Map();
    for (const line of lines || []) {
      const id = Number(line.matched_product_id || line.product_id);
      if (!validId(id) || line.is_new_product || line.manual_size_entry) continue;
      if (!result.has(id)) result.set(id, {id,
        code: line.product_code || line.raw_product_code || line._inventory_product?.product_code || '',
        name: line.product_name || line.raw_product_name || line._inventory_product?.product_name || ''});
    }
    return [...result.values()];
  }
  function link(customerId, ids) {
    const unique = [...new Set(ids.map(Number))];
    if (!validId(customerId) || !unique.length || unique.length > 100 || !unique.every(validId)) return '';
    return '/frontend-v2/formal-workspace?' + new URLSearchParams({page:'requisition',
      [customerKey]:String(customerId), [idsKey]:unique.join(',')});
  }
  const picker = {
    props:['customerId', 'lines'],
    data:() => ({selected:[]}),
    computed:{
      rows() {return products(this.lines);},
      signature() {return JSON.stringify([this.customerId,this.rows.map(row=>row.id)]);},
      href() {return link(this.customerId,this.selected);}
    },
    watch:{signature() {this.selected=[];}},
    template:`<details class="panel" style="margin:8px 0" data-manual-replenishment-picker>
      <summary>选择产品补库（可多选）</summary>
      <p class="muted">未到库存预警也可主动补库。补库录入在新窗口打开，当前订单和选款会保留；补库数量另行填写。</p>
      <div v-if="!rows.length" class="muted">请先选择或匹配常用箱；手工尺寸和未匹配产品请先登记常用箱。</div>
      <div v-else>
        <button type="button" class="btn small" @click="selected=rows.slice(0,100).map(row=>row.id)">选择{{rows.length>100?'前100款':'全部'}}</button>
        <button type="button" class="btn small" @click="selected=[]">清空选择</button>
        <div v-for="row in rows" :key="row.id" style="padding:5px 0">
          <label><input type="checkbox" :value="row.id" v-model="selected" :disabled="selected.length>=100&&!selected.includes(row.id)"> {{row.code || '已匹配常用箱'}} · {{row.name}}</label>
        </div>
        <a v-if="href" class="btn primary" :href="href" target="_blank" rel="noopener">进入补库录入（{{selected.length}}款）</a>
        <span v-else class="muted">勾选需要补库的产品，每次最多100款。</span>
      </div>
    </details>`
  };
  async function openFromLocation(vm, location=root.location, history=root.history, http=root.axios) {
    const url=new URL(location.href), raw=url.searchParams.get(idsKey);
    if (raw===null || !vm.user?.id || vm.user.must_change_password) return;
    const customerId=Number(url.searchParams.get(customerKey)), parts=raw.split(',');
    // Consume only the navigation intent. No draft or inventory write occurs here.
    url.searchParams.delete(idsKey);url.searchParams.delete(customerKey);
    history.replaceState(history.state,'',url.pathname+url.search+url.hash);
    if (!vm.canRequisition || vm.activePage!=='requisition') {vm.showToast('当前账号没有手动补库权限，请沿原业务申请流程办理',true);return;}
    if (!validId(customerId)||parts.length>100||!parts.length||!parts.every(p=>/^\d+$/.test(p)&&validId(p))) {
      vm.showToast('补库产品选择无效，请返回原订单重新勾选',true);return;
    }
    const ids=[...new Set(parts.map(Number))], actor=vm.user.id, generation=vm.authGeneration, pageSequence=vm.pageLoadSequence;
    let modal=vm.modal, form=null;
    const current=()=>vm.user?.id===actor&&vm.authGeneration===generation&&vm.activePage==='requisition'
      &&vm.pageLoadSequence===pageSequence&&vm.modal===modal&&(!form||vm.stockReplenishmentForm===form)&&vm.canRequisition;
    if (modal) {vm.showToast('请关闭当前窗口后，从原订单重新进入补库',true);return;}
    const context={current,bind(f,m){form=f;modal=m;},get:(...args)=>http.get(...args)};
    try {
      // Exact IDs prevent same-code products or search limits from selecting a different product.
      const params=new URLSearchParams({customer_id:String(customerId),limit:String(ids.length)});
      ids.forEach(id=>params.append('product_ids',String(id)));
      const {data}=await http.get('/api/requisition/stock-replenishment/products?'+params);
      if (!current()) return;
      const found=new Map((data.items||[]).map(row=>[Number(row.id),row]));
      if(ids.some(id=>!found.has(id)||Number(found.get(id).customer_id)!==customerId))
        throw Error('部分产品已停用、变更或无权访问，请返回订单重新核对；未生成补库');
      await vm.openStockReplenishment({productAction:context});
      if(!current()||!form) return;
      vm.stockReplenishmentProducts=ids.map(id=>found.get(id));
      form.customer_id=customerId;
      for(const id of ids) {
        vm.addBlankStockReplenishmentLine();
        const line=form.items.at(-1);
        line.reference_product_id=id;
        vm.applyStockProduct(line);
        line.quantity=null;
      }
      vm.showToast(`已带入${ids.length}款，请填写补库数量后保存；原订单仍在原窗口`);
    } catch(error) {
      if(current()) {
        // Do not leave a partially initialized form that can be mistaken for the selected batch.
        if(form&&vm.modal===modal) vm.modal=null;
        vm.showToast('补库资料读取失败：'+vm.errorMessage(error),true);
      }
    }
  }
  root.TMManualReplenishment={picker,products,link,openFromLocation};
  if(typeof module!=='undefined') module.exports=root.TMManualReplenishment;
})(typeof window!=='undefined'?window:globalThis);
