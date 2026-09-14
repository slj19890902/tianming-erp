(function(global){
 'use strict';
 function presentNumber(value){return value !== null && value !== undefined && String(value).trim() !== '' && Number.isFinite(Number(value));}
 function amount(item){return presentNumber(item.quantity) && presentNumber(item.unit_price) ? Number(item.quantity)*Number(item.unit_price) : null;}
 function install(app){app.mixin({
  data(){return this.$parent?{}:{pdfActiveDraftKey:'',pdfQueueVisible:true,pdfQueuePage:1,pdfFitCapacity:0};},
  watch:{'modal.type'(){if(this.$parent)return;cancelAnimationFrame(this._pdfFitFrame);this.pdfFitCapacity=0;this._pdfFitGeometry='';}},
  updated(){if(this.$parent||this.modal?.type!=='orderPdfImport')return;cancelAnimationFrame(this._pdfFitFrame);this._pdfFitFrame=requestAnimationFrame(()=>{this._pdfFitFrame=requestAnimationFrame(()=>{
   if(this.modal?.type!=='orderPdfImport')return;
   const card=[...document.querySelectorAll('.pdf-order-import-modal .order-group-detail-card')].find(c=>c.getBoundingClientRect().height>0);if(!card)return;
   const table=card.querySelector('.pdf-inventory-table'),footer=card.querySelector('.pdf-savebar');if(!table||!footer)return;
   const rows=[...table.querySelectorAll(':scope > tbody > tr')].filter(r=>r.cells.length>1);if(!rows.length)return;
   const geometry=[innerWidth,innerHeight,this.uiMode,this.pdfActiveDraftKey].join(':');
   // Expanded rows and overlays can move the table while Vue updates. Keep the
   // current page stable; only a real window/draft geometry change may repage.
   const detailOpen=card.querySelector('.order-item-sub-row, .pdf-stock-dialog');
   if(detailOpen && this.pdfFitCapacity && this._pdfFitGeometry===geometry)return;
   if(this._pdfFitGeometry!==geometry){this._pdfFitGeometry=geometry;this._pdfFitRowHeight=0;}
   // Keep the tallest measured row for this geometry so paging cannot oscillate.
   const height=Math.max(this._pdfFitRowHeight||0,...rows.map(r=>r.getBoundingClientRect().height));
   if(!Number.isFinite(height)||height<=0)return;
   this._pdfFitRowHeight=height;
   const next=Math.max(1,Math.min(8,Math.floor((innerHeight-table.getBoundingClientRect().top-(table.tHead?.offsetHeight||32)-footer.offsetHeight-56)/height)));
   if(next!==this.pdfFitCapacity)this.pdfFitCapacity=next;
  });});},
  beforeUnmount(){cancelAnimationFrame(this._pdfFitFrame);},
  methods:{
   pdfDraftKey(draft){return String(draft.file_hash || draft.source_name || '');},
   pdfQueueDrafts(){return this.orderImportDrafts.filter(d=>!this.emailQueueMode || d.email_queue_status===this.emailQueueFilter);},
   pdfCurrentDraft(draft){const all=this.pdfQueueDrafts();return this.pdfDraftKey(draft)===(all.some(d=>this.pdfDraftKey(d)===this.pdfActiveDraftKey)?this.pdfActiveDraftKey:this.pdfDraftKey(all[0]||{}));},
   pdfOpenDraft(draft){this.pdfActiveDraftKey=this.pdfDraftKey(draft);this.pdfQueueVisible=false;this.pdfFitCapacity=0;},
   pdfMoveDraft(step){const all=this.pdfQueueDrafts();const index=all.findIndex(d=>this.pdfCurrentDraft(d));if(all[index+step])this.pdfOpenDraft(all[index+step]);},
   pdfDraftPosition(){const all=this.pdfQueueDrafts();return Math.max(1,all.findIndex(d=>this.pdfCurrentDraft(d))+1);},
   pdfCustomerShort(draft){const customer=(this.customerOptions||[]).find(c=>Number(c.id)===Number(draft.matched_customer_id));return customer?.chinese_short_name || customer?.short_name || customer?.abbreviation || customer?.name || draft.customer_name || draft.customer_name_raw || '客户待确认';},
   pdfPageSize(){return this.pdfFitCapacity||Math.max(1,Math.min(8,Math.floor(((this.workspaceHeight||768)-(this.uiMode==='large'?500:440))/(this.uiMode==='large'?90:70))));},
   async confirmAndSavePdf(draft){if(this.loading||this.isImportDraftLocked(draft)||!this.canConfirmImportDraft(draft))return;draft.confirmed=true;await this.saveConfirmedImportDrafts(draft);},
   async confirmAndApplyPdf(draft){if(!this.canConfirmImportDraft(draft)||this.isImportDraftLocked(draft))return;draft.confirmed=true;await this.applyPdfDraftToOrderForm(draft);},
   pdfPageItems(draft){const size=this.pdfPageSize();const page=Math.min(draft._product_page||1,Math.max(1,Math.ceil((draft.items||[]).length/size)));return (draft.items||[]).slice((page-1)*size,page*size);},
   pdfQueueItems(){const rows=this.pdfQueueDrafts();const page=Math.min(this.pdfQueuePage,Math.max(1,Math.ceil(rows.length/this.pdfPageSize())));return rows.slice((page-1)*this.pdfPageSize(),page*this.pdfPageSize());},
   pdfItemAmount(item){const value=amount(item);return value===null?'待核对':this.money(value);},
   pdfDraftAmount(draft){const items=draft.items||[];if(!items.length||items.some(i=>amount(i)===null))return '金额待核对';return this.money(items.reduce((sum,i)=>sum+amount(i),0));},
   pdfDraftIssueCount(draft){return (draft.items||[]).filter(i=>!i.matched_product_id || i.is_new_product || i.price_conflict || !presentNumber(i.unit_price) || this.pdfInventoryIssueText(i)).length;},
   pdfOpenInventory(item){item._show_inventory_details=true;item._inventory_tab='summary';if(!(item._inventory?.finished?.candidates||[]).length){for(const kind of ['semi','raw']){if(Object.values(item._inventory?.semi||{}).some(part=>[...(part.candidates||[]),...(part.manual_candidates||[])].some(c=>this.pdfMaterialKind(c)===kind))){item._inventory_tab=kind;break;}}}item._inventory_page=1;this.$nextTick(()=>document.querySelector('.pdf-stock-dialog .workspace-dialog-close')?.focus());},
   pdfMaterialKind(candidate){return candidate.material_kind || (candidate.sheet_type==='raw_board'?'raw':'semi');},
   pdfMaterialCandidates(item,component,kind){const part=item._inventory?.semi?.[component]||{};const rows=[...part.candidates||[],...part.manual_candidates||[]];return [...new Map(rows.map(r=>[r.lot_id,r])).values()].filter(r=>this.pdfMaterialKind(r)===kind).sort((a,b)=>this.pdfCandidateExact(b)-this.pdfCandidateExact(a) || Number(b.deductible_requirement_quantity||0)-Number(a.deductible_requirement_quantity||0));},
   pdfCandidateExact(candidate){return !(candidate.signature_differences||[]).length && !(candidate.warning_codes||[]).length && candidate.source!=='manual' && !this.inventoryCandidateNeedsManualConfirmation(candidate);},
   pdfMaterialButton(item,kind){const rows=this.inventoryComponents(item).flatMap(c=>this.pdfMaterialCandidates(item,c,kind));return rows.some(c=>this.pdfCandidateExact(c))?{tone:'success',text:'完全符合'}:rows.length?{tone:'warning',text:'需核对适配'}:{tone:'',text:'无可用'};},
   pdfFinishedUse(item,candidate){return (item._inventory?.finished?.allocations||[]).filter(a=>a.candidate.lot_id===candidate.lot_id).reduce((sum,a)=>sum+Number(a.requested_qty||0),0);},
   pdfCandidateCrease(c){const type=c.crease_type || ({raw_board:'毛片',net_sheet:'净料',creased_sheet:'压线'})[c.sheet_type] || '待核对';return type==='压线'?type+' '+[c.crease_left_mm,c.crease_middle_mm,c.crease_right_mm].map(v=>v??'?').join('+'):type;},
  }
 });}
 global.ERPPdfWorkspace={install,amount};
})(typeof window==='undefined'?globalThis:window);
