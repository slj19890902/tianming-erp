const fs=require('fs'),vm=require('vm');
vm.runInThisContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'));
const html=fs.readFileSync('static/index.html','utf8');
const start=html.indexOf('<data-panel :loading="incomingTab');
const template=html.slice(start,html.indexOf('</data-panel>',start)+13);
const row={item_id:'r194',material_status:'pending',requisition_status:'未报料',purpose_status:'frozen',
 product_code:'Z.001.000222',product_name:'纸箱',customer_name:'驿力',cardboard_len:'1880.00',
 cardboard_width:'1318.00',material:'GSNSV',flute_type:'AB',planned_quantity:20,remaining_quantity:20,incoming_quantity:20};
const state={incomingTab:'pending',incomingPending:[row],incomingPendingLoading:false,incomingPendingError:'',
 incomingSelected:{},incomingPriceRecovery:{},incomingReceiveAttempts:{},incomingProductionCardSelections:{},
 incomingBatchAttempt:null,pages:{incomingPending:1},user:{role:'admin'},$slots:{},$attrs:{},
 hasPermission:()=>true,screenPageSize:()=>12};
const ctx=new Proxy(state,{has:(_,key)=>!String(key).startsWith('_') && !['Math','Number','String','Boolean','Object','Array','undefined'].includes(key),get(target,key){
 if(key===Symbol.unscopables)return undefined;
 if(key in target)return target[key];
 const match=new RegExp('^          (?:async )?'+String(key)+'\\(','m').exec(html);
 if(match){const rest=html.slice(match.index);const end=/^          (?:async )?[A-Za-z_$][\w$]*\(/m.exec(rest.slice(12));
   const method=rest.slice(0,end.index+12);target[key]=vm.runInThisContext('({'+method+'})')[key].bind(ctx);return target[key];}
 return undefined;
}});
const vnode=Vue.compile(template,{decodeEntities:v=>v})(ctx,[]);
vnode.children?.default?.();
console.log('Incoming pending table render passed');
