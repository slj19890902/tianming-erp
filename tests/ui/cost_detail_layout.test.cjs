const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const Vue=require('vue'),{compile}=require('@vue/compiler-dom'),{renderToString}=require('@vue/server-renderer');
const html=fs.readFileSync(path.resolve(__dirname,'../../static/index.html'),'utf8');
const start=html.indexOf('<table class="order-read-detail-table">');
assert.ok(start>0,'order detail needs its own table layout');
const template=html.slice(start,html.indexOf('</table>',start)+8);
const render=new Function('Vue',compile(template,{prefixIdentifiers:true}).code)(Vue);
for(const cost of [true,false])for(const sales of [true,false])test(`cost evidence has its own row; cost=${cost}, sales=${sales}`,async()=>{
 const items=[1,2].map(id=>({id,snapshot_product_name:`产品${id}`,snapshot_product_code:`CODE${id}`,quantity:10,estimated_total_cost_status:'calculated',estimated_order_total_cost:100+id,estimated_total_cost_snapshot_version:1,estimated_cost_missing_items:[`缺口${id}`],bom_components:id===2?[{id:22}]:[]}));
 const methods=Object.fromEntries(['itemBusinessStatusKey','combinationGroupLabel','orderSpecificationText','orderItemMaterialText','formatReportDims','formatCrease','orderMaterialCostSummary','orderMaterialCostComponentText','orderProcessingCostText','bomPreviewComponentLabel'].map(k=>[k,()=>'-']));
 const app=Vue.createSSRApp({data:()=>({orderDetail:{items},canViewCosts:cost,canViewSalesAmounts:sales,canAdmin:false,costReviewFocusItemId:1}),methods:{...methods,money:String,hasBomComponents:item=>item.bom_components.length>0},render});
 app.component('status-tag',{render:()=>null});
 const out=await renderToString(app);
 if(cost&&sales&&process.env.COST_LAYOUT_PREVIEW)fs.writeFileSync(process.env.COST_LAYOUT_PREVIEW,html.slice(0,html.indexOf('</head>')+7)+'<body class="erp-enterprise-ui"><p>隔离匿名样例 · 不连接正式数据库</p><div class="modal" style="width:95vw;margin:20px auto"><div class="modal-body"><div class="table-wrap order-read-detail-wrap">'+out+'</div></div></div></body></html>');
 const rows=[...out.matchAll(/<tr\b[^>]*>[\s\S]*?<\/tr>/g)].map(m=>m[0]);
 assert.equal((rows[0].match(/<th\b/g)||[]).length,sales?14:12);
 for(let id=1;id<=2;id++){
  const i=rows.findIndex(r=>r.includes(`id="cost-review-item-${id}"`)),cells=[...rows[i].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)];
  assert.equal(cells.length,sales?14:12);assert.match(cells[2][1],new RegExp(`产品${id}`));assert.doesNotMatch(cells[2][1],/material-cost|bom-preview|预计总成本|缺口/);
  if(cost||id===2){assert.match(rows[i+1],/order-read-basis-row/);assert.match(rows[i+1],new RegExp(`colspan="${sales?14:12}"`));assert.match(rows[i+1],new RegExp(`CODE${id} 的依据`));}
  if(cost){assert.match(rows[i+1],new RegExp(`缺口${id}`));assert.match(rows[i+1],/material-cost-compact/);if(id===1)assert.match(rows[i+1],/<details open/);}
 }
 if(!cost)assert.doesNotMatch(out,/预计总成本|缺口|material-cost-compact/);
});
