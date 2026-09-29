/* Keep an order draft alive while editing its product or inspecting real stock. */
(function(global){
 'use strict';
 const idOf=line=>Number(line?.matched_product_id || line?.product_id || line?.id || 0);
 const inventoryFields=['customer_id','material_id','flute_type','layer_count','box_type','supply_mode','length_mm','width_mm','height_mm','report_length_mm','report_width_mm','crease_type','crease_left_mm','crease_middle_mm','crease_right_mm','base_report_length_mm','base_report_width_mm','base_crease_type','base_crease_left_mm','base_crease_middle_mm','base_crease_right_mm','default_cutting_mode','base_default_cutting_mode','pieces_per_box','is_composite','combination_mode','composite_fulfillment_mode'];
 const inventoryBasis=product=>JSON.stringify(inventoryFields.map(key=>product?.[key]??null));
 function install(app){
  app.component('order-stock-location',{
   props:['candidate','draft','item'],
   template:`<button v-if="$root.pdfWarehouseLocatorCanOpen(candidate)" type="button" class="order-location-link" title="查看此货位与产品标签" @click.stop="$root.openPdfWarehouseLocator(draft,item,candidate)">{{$root.inventoryLocation(candidate)}}</button><span v-else>{{$root.inventoryLocation(candidate)}}</span>`
  });
  app.mixin({
   data(){return this.$parent?{}:{orderContextOpening:false};},
   watch:{authGeneration(){if(this.$parent)return;this.productEditReturnContext=null;this.orderContextOpening=false;if(this.pdfWarehouseLocator)this.pdfWarehouseLocator.visible=false;}},
   methods:{
    orderContextCurrent(context){
     return this.productEditReturnContext===context && context.authGeneration===this.authGeneration
      && context.userId===(this.user?.id??null)
      && (context.source==='pdf'?this.orderImportDrafts.includes(context.draft):context.source==='requisition'
       ? this.activePage==='requisition' && this.requisitionWorkspace==='board' && this.requisitionPending.includes(context.row)
       :this.orderForm===context.order);
    },
    captureOrderContextView(){
     return {focus:document.activeElement,scroll:['.modal-mask','.modal-body','.pdf-inventory-table-wrap','.order-common-box-table-wrap'].flatMap(selector=>[...document.querySelectorAll(selector)].map((el,index)=>({selector,index,top:el.scrollTop,left:el.scrollLeft})))};
    },
    restoreOrderContextView(view){this.$nextTick(()=>{for(const row of view?.scroll||[]){const el=document.querySelectorAll(row.selector)[row.index];if(el){el.scrollTop=row.top;el.scrollLeft=row.left;}}if(view?.focus?.isConnected)view.focus.focus({preventScroll:true});else document.querySelector('.modal-head button,.order-common-box-modal .modal-header button')?.focus({preventScroll:true});});},
    async openOrderContextProduct(item,source,draft=null){
     if(!this.canEditProducts || this.orderContextOpening || this.masterSavePending || !idOf(item))return false;
     if(source==='pdf' && (!this.orderImportDrafts.includes(draft)||this.isImportDraftLocked(draft)))return false;
     this.productEditReturnContext={source,productId:idOf(item),draft,order:this.orderForm,returnModal:{...this.modal},
      authGeneration:this.authGeneration,userId:this.user?.id??null,view:this.captureOrderContextView(),
      pickerVisible:this.orderCommonBoxPicker?.visible===true,saved:false};
     // Read back Vue's reactive context so async ownership checks compare the same proxy.
     const context=this.productEditReturnContext;this.orderContextOpening=true;
     if(context.pickerVisible)this.orderCommonBoxPicker.visible=false;
     try{
      const opened=await this.openProduct({id:context.productId});
      if(!this.orderContextCurrent(context))return false;
      if(!opened){await this.restoreOrderContextProduct(context.productId);return false;}
      this.modal.title=`编辑常用箱 · ${item.normalized_product_code||item.product_code||this.productForm.product_code||''}`;
      return true;
     }finally{if(this.productEditReturnContext===context || !this.productEditReturnContext)this.orderContextOpening=false;}
    },
    captureRequisitionContextView(row){
     return {workspace:this.requisitionWorkspace,tab:this.requisitionTab,supplierFilter:this.requisitionSupplierFilter,
      page:this.pages.requisitionPending,selectedKeys:[...(this.selectedPendingKeys||[])],selectedRows:{...(this.requisitionSelected||{})},
      rowKey:this.pendingRowKey(row),view:this.captureOrderContextView()};
    },
    async openRequisitionContextProduct(row){
     const productId=Number(row?.product_id||0);
     if(!this.canEditProducts || this.orderContextOpening || this.masterSavePending || row?.is_merge_group || !productId
      || this.activePage!=='requisition' || this.requisitionWorkspace!=='board')return false;
     const requisitionView=this.captureRequisitionContextView(row);
     this.productEditReturnContext={source:'requisition',productId,row,requisitionView,returnModal:{...this.modal},
      authGeneration:this.authGeneration,userId:this.user?.id??null,saved:false};
     const context=this.productEditReturnContext;this.orderContextOpening=true;
     try{
      const opened=await this.openProduct({id:productId});
      if(!this.orderContextCurrent(context))return false;
      if(!opened){await this.restoreOrderContextProduct(productId);return false;}
      this.modal.title=`补齐常用箱 · ${row.product_code||this.productForm.product_code||''}`;
      return true;
     }finally{if(this.productEditReturnContext===context || !this.productEditReturnContext)this.orderContextOpening=false;}
    },
    openCommonBoxEditorFromPicker(item){return this.openOrderContextProduct(item,'picker');},
    applyOrderContextProduct(line,product,isPdf){
     const old=line._inventory_product;
     const inventoryChanged=!old || inventoryBasis(old)!==inventoryBasis(product);
     const updateNotes=!line.production_notes || (old && line.production_notes===old.production_notes);
     Object.assign(line,{product_name:product.product_name||'',specification:this.orderSpecificationText(this.spec(product),''),
      layer_count:product.layer_count||null,flute_type:product.flute_type||null,_inventory_product:product,
      _common_box_readiness:this.commonBoxReadiness(product),supply_mode:product.supply_mode||'corrugated_production',
      external_packaging_order_quantity_basis:product.external_packaging_default_order_quantity_basis??null,
      external_packaging_purchase_quantity_basis:product.external_packaging_default_purchase_quantity_basis??null});
     if(updateNotes)line.production_notes=product.production_notes||'';
     if(isPdf){
      Object.assign(line,{matched_material_id:product.material_id||null,material_base_code:(product.material_code||'').split('-')[0],
       material_supplier_name:product.material_supplier_name||'',material_weight_structure:product.material_weight||'',
       product_manual_modified:!!product.manual_modified,product_manual_modified_at:product.manual_modified_at||null,
       product_drawing_file:product.drawings?.[0]?.image_path||null,product_default_price:product.sale_unit_price==null?null:String(product.sale_unit_price)});
      this.refreshPdfMaterialComparison(line,product);this.refreshPdfPriceConflict(line);
     }else{
      Object.assign(line,{product_code:product.product_code||'',material_id:product.material_id||null,
       material:product.material_code||this.materialName(product.material_id,product.legacy_material_text),
       _from_product:true,_manual_modified:!!product.manual_modified,_layer_filter:product.layer_count||null,
       _flute_filter:product.flute_type||null,_material_code:product.material_code||'',
       _material_supplier_name:product.material_supplier_name||'',_material_weight:product.material_weight||'',_product_drawings:product.drawings||[],
       _original_product:{specification:this.orderSpecificationText(this.spec(product),''),sale_unit_price:product.sale_unit_price,layer_count:product.layer_count,flute_type:product.flute_type,version:product.version}});
     }
     // Quantities, entered prices, notes, drawings, BOM overrides and line identities are owned by the order.
     // A changed product invalidates only this line's inventory decision; the existing planner rechecks eligibility.
     if(inventoryChanged)this.invalidateLineInventory(line);
     line._product_context_refreshed=true;
     return inventoryChanged;
    },
    async restoreOrderContextProduct(productId,{saved=false}={}){
     const context=this.productEditReturnContext;
     if(!context || !this.orderContextCurrent(context))return false;
     context.saved=context.saved||saved;
     let product=null;
     if(context.saved){
      try{
       product=(await axios.get(`/api/master/products/${context.productId}`)).data;
       if(!this.orderContextCurrent(context))return false;
       if(Number(product.id)!==context.productId)throw new Error('产品资料不一致');
      }catch(error){if(this.orderContextCurrent(context))this.showToast(`常用箱已保存，资料刷新失败：${this.errorMessage(error)}。请点击返回重试。`,true);return false;}
     }
     if(context.source==='requisition'){
      const savedView=context.requisitionView;
      this.productEditReturnContext=null;this.resetProductEditorState();this.modal=context.returnModal;
      this.requisitionWorkspace=savedView.workspace;this.requisitionTab=savedView.tab;
      this.requisitionSupplierFilter=savedView.supplierFilter;this.pages.requisitionPending=savedView.page;
      this.selectedPendingKeys=[...savedView.selectedKeys];this.requisitionSelected={...savedView.selectedRows};
      if(product){
       try{await this.loadRequisition({skipAutoRelease:true});}
       catch(error){this.showToast(`常用箱已保存，待报料刷新失败：${this.errorMessage(error)}。请刷新后核对。`,true);return false;}
       this.showToast(this.commonBoxReadiness(product).ready?'常用箱资料已完善，已返回原报料筛选':'已保存；仍有资料待完善，请按缺项补充');
      }
      this.restoreOrderContextView(savedView.view);
      return true;
     }
     const refreshed=[];
     if(product){
      if(context.source==='pdf'){
       for(const draft of this.orderImportDrafts){if(this.isImportDraftLocked(draft))continue;
        for(const line of draft.items||[]){if(idOf(line)!==context.productId)continue;const changed=this.applyOrderContextProduct(line,product,true);this.invalidateImportDraftConfirmation(draft);if(changed)refreshed.push([line,draft.matched_customer_id]);this.refreshOrderContextCost(line,context,true);}
       }
      }else{
       for(const line of context.order.items||[]){if(idOf(line)!==context.productId)continue;const changed=this.applyOrderContextProduct(line,product,false);if(changed)refreshed.push([line,context.order.customer_id]);this.refreshOrderContextCost(line,context,false);}
      }
      const picker=this.orderCommonBoxPicker;
      for(const row of picker?.items||[]){if(Number(row.id)===context.productId)Object.assign(row,product);}
      const selection=picker?.selected?.[String(context.productId)];
      if(selection)selection.product=product;
      for(const rows of Object.values(this.orderProductOptions||{})){for(const row of rows){if(Number(row.id)===context.productId)Object.assign(row,product);}}
     }
     this.productEditReturnContext=null;this.resetProductEditorState();this.modal=context.returnModal;
     if(context.pickerVisible)this.orderCommonBoxPicker.visible=true;
     this.restoreOrderContextView(context.view);
     if(product){
      this.showToast(this.commonBoxReadiness(product).ready?'常用箱资料已完善，原订单已保留':'已保存；仍有资料待完善，请按缺项补充');
      for(const [line,customerId] of refreshed)this.refreshOrderLineInventory(line,customerId);
     }
     return true;
    },
    async refreshOrderContextCost(line,context,isPdf){
     if(!this.canViewCosts)return;
     const product=line._inventory_product;
     line.cost_status='pending';line.estimated_cost=null;
     try{
      const {data}=await axios.post('/api/orders/cost-preview',{product_id:context.productId,material_id:product.material_id||null,flute_type:product.flute_type||null});
      if(context.authGeneration!==this.authGeneration || context.userId!==(this.user?.id??null) || line._inventory_product!==product)return;
      if(isPdf?!this.orderImportDrafts.some(d=>(d.items||[]).includes(line)):!this.orderForm.items.includes(line))return;
      for(const key of ['cost_status','estimated_cost','cost_detail','cost_reason'])if(key in data)line[key]=data[key];
     }catch(_error){/* Pending is visible; the normal order calculation can retry. */}
    },
   }
  });
 }
 global.ERPOrderContext={install};
})(typeof window==='undefined'?globalThis:window);
