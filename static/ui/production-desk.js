(function(global){
 'use strict';
 global.ERPProductionDesk={install(app){app.mixin({
  data(){return this.$parent?{}:{productionQuery:'',productionMaterialsSelected:{},productionMaterials:null,productionEntry:null,productionPlacementState:'all',productionMaterialsBusy:false};},
  watch:{authGeneration(){this.productionMaterials=null;this.productionEntry=null;this.productionMaterialsSelected={};this.productionQuery='';}},
  methods:{
   materialSources(row){return row.customer_board_preparation_sources||[];},
   materialSpec(s){return [s.board_length_mm&&s.board_width_mm?`${s.board_length_mm} × ${s.board_width_mm} mm`:'尺寸待核',s.flute_type||'楞型待核'].join(' · ');},
   materialMap(s){return '/warehouse.html?'+new URLSearchParams({readonly:'1',source:'order-context',tab:'map',floor:String(s.warehouse_floor)+'F',mode:'lookup',view:'2d',location_id:String(s.location_id),lot_id:String(s.inventory_lot_id)});},
   async showProductionMaterials(rows){
    if(this.productionMaterialsBusy)return;const auth=this.authGeneration;this.productionMaterialsBusy=true;this._productionFocus=document.activeElement;
    try{const {data}=await axios.post('/api/production/material-list',{task_ids:rows.map(r=>r.id)});if(auth===this.authGeneration){this.productionMaterials=data;this.focusProductionDialog();}}
    catch(e){if(auth===this.authGeneration)this.showToast(this.errorMessage(e),true);}finally{if(auth===this.authGeneration)this.productionMaterialsBusy=false;}
   },
   async printProductionMaterials(){
    const d=this.productionMaterials;if(!d||this.productionMaterialsBusy)return;
    if(d.print_count>0&&!window.confirm(`这组选定任务已有 ${d.print_count} 次打印记录，确认再次打印当前用料清单？`))return;
    this.productionMaterialsBusy=true;
    try{const auth=this.authGeneration;const {data}=await axios.post('/api/production/material-list/print',{task_ids:d.tasks.map(t=>t.id)});if(auth!==this.authGeneration)return;this.productionMaterials=data;await this.$nextTick();document.body.classList.add('printing-material-list');window.print();}
    catch(e){this.showToast(this.errorMessage(e),true);}finally{document.body.classList.remove('printing-material-list');this.productionMaterialsBusy=false;}
   },
   async registerProductionOutput(row){
    if(!this.canConfirmProductionRow(row)||this.productionBusy)return;
    await this.refreshProductionPreview(row);if(this.productionBusy||this.productionEntry!==row||!row.output_preview?.allowed||row.preview_error)return;
    if(row.completion_mode==='direct')await this.confirmProductionDirectRow(row);
    else {Object.keys(this.productionSelected).forEach(k=>this.productionSelected[k]=false);this.productionSelected[row.id]=true;await this.batchConfirmProduction();}
    if(this.productionTab!=='pending')this.productionEntry=null;
   },
   productionRecordLabel(row){return ({manual:'手工加工',receipt_auto:'收料自动形成',stock_preparation:'备库生产',stock_assembly:'备库组套',bom_assembly:'订单组套'})[row.origin]||'历史记录';},
   openProductionEntry(row){this._productionFocus=document.activeElement;this.productionEntry=row;this.refreshProductionPreview(row);this.focusProductionDialog();},
   focusProductionDialog(){this.$nextTick(()=>document.querySelector('.production-desk-dialog button,.production-desk-dialog input')?.focus());},
   closeProductionDialog(){if(this.productionBusy||this.productionMaterialsBusy)return;this.productionEntry=null;this.productionMaterials=null;this._productionFocus?.focus();},
   trapProductionFocus(event){const nodes=[...event.currentTarget.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href]')].filter(n=>n.getClientRects().length);if(!nodes.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}},
   async refreshProductionPreview(row){
    const auth=this.authGeneration,key=JSON.stringify([row.actual_input_quantity,row.actual_output_quantity,row.version]);row._preview_key=key;row.output_preview=null;row.preview_error='';row.preview_loading=true;
    const input=Number(row.actual_input_quantity),output=Number(row.actual_output_quantity);if(!Number.isInteger(input)||input<0||!Number.isInteger(output)||output<0){row.preview_error='请填写完整的整数数量';row.preview_loading=false;return;}
    try{const {data}=await axios.post('/api/production/output-preview',{task_id:row.id,expected_version:row.version,input_quantity:input,output_quantity:output});if(auth===this.authGeneration&&row._preview_key===key)row.output_preview=data;}
    catch(e){if(auth===this.authGeneration&&row._preview_key===key)row.preview_error=this.errorMessage(e);}
    finally{if(auth===this.authGeneration&&row._preview_key===key)row.preview_loading=false;}
   },
   async choosePreparationState(state){this.stockPrepState=state;await this.loadStockPreparation(1);},
   async searchProduction(){this.stockPrepQuery=this.productionQuery;this.productionMaterialsSelected={};await Promise.all([this.loadProductionPage(1),this.loadStockPreparation(1)]);},
  }
 });}};
})(window);
