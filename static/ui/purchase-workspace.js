(function(global){
 'use strict';
 function install(app){app.mixin({
  data(){return this.$parent?{}:{purchaseReview:null,purchaseReviewSequence:0};},
  methods:{
   async openPurchaseReview(row,page=1){
    const seq=++this.purchaseReviewSequence,auth=this.authGeneration;
    this.purchaseReview={row,page,items:[],total:0,loading:true,error:''};
    try{
     const {data}=await axios.get('/api/requisition/reported-items',{params:{source_type:row.source_type,document_id:row.document_id,page,page_size:Math.min(8,this.screenPageSize(8))}});
     if(seq!==this.purchaseReviewSequence || auth!==this.authGeneration || !this.purchaseReview)return;
     this.purchaseReview={row,page,items:data.items||[],total:data.total||0,loading:false,error:''};
    }catch(e){if(seq===this.purchaseReviewSequence && auth===this.authGeneration && this.purchaseReview){this.purchaseReview.loading=false;this.purchaseReview.error=this.errorMessage(e);}}
   },
   async voidPurchaseReviewItem(row){const context=this.purchaseReview;if(await this.voidReportedItem(row)){if(this.purchaseReview===context)await this.openPurchaseReview(context.row,context.page);}},
  }
 });}
 global.ERPPurchaseWorkspace={install};
})(typeof window==='undefined'?globalThis:window);
