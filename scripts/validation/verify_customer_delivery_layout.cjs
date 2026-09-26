// Isolated Chrome render. No ERP network requests or business writes.
const fs=require('fs'),path=require('path'),assert=require('assert');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../..');
const [layoutFile,out]=process.argv.slice(2);
assert(layoutFile && out, 'usage: node verify_customer_delivery_layout.cjs layouts.json output-dir');
fs.mkdirSync(out,{recursive:true});
for(const file of ['static/index.html','static/delivery-print.html','static/delivery-print-designer.html']) {
 const html=fs.readFileSync(path.join(root,file),'utf8');
 for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))if(match[1].trim()) new Function(match[1]);
}
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1150,height:800}});
 await page.route('**/*', route=>route.abort());
 await page.setContent('<style>html,body{margin:0;padding:0}</style><div id="sheets"></div>');
 await page.addStyleTag({content:fs.readFileSync(path.join(root,'static/customer-delivery-print.css'),'utf8')});
 await page.addScriptTag({content:fs.readFileSync(path.join(root,'static/customer-delivery-print.js'),'utf8')});
 const layouts=JSON.parse(fs.readFileSync(layoutFile,'utf8'));
 const results=[];
 for(const [preset,layout] of Object.entries(layouts))for(const prices of [true,false]){
   const data={id:1,print_template:{layout},price_display:{shown:prices,allowed:true},order_context:preset==='yke'?'海外订单':'',
    sender:{company_name:'苏州天明包装有限公司',address:'苏州市吴中区临湖镇浦庄大道工业区',phone:'0512-66530018'},
    customer:{name:preset==='yl'?'苏州工业园区驿力机车科技有限公司':preset==='kew'?'光洋':'研光',address:'客户交货地点（沿用冻结地址）',contact_person:'客户联系人'},
    delivery_number:'DH-20260922-TEST',delivery_date:'2026-09-22',vehicle_number:'苏E测试',total_amount:'7941.68',commercial_quantity:3700,commercial_quantities:{'只':3700},
    customer_document_rows:Array.from({length:37},(_,i)=>({sequence:i+1,customer_material_code:preset==='yl'?'Z.001.000205':'80010631',customer_drawing_number:'0632094-1',customer_category:'AT',customer_model:'TRD-N / KEW 长型号',customer_product_name:'305风机纸箱(含衬板）1:4',specification:'520×350×300',quantity:100,unit:'只',unit_price:'2.14642',amount:'214.64',customer_po:i%2?'POORD040860':'POORD040679',remarks:i===0?'按原始内容完整打印，无删减':'',pricing_included:true}))};
   const result=await page.evaluate(data=>{const container=document.getElementById('sheets');const pages=CustomerDeliveryPrint.render(data,container);return{pages,text:container.textContent,heights:[...container.children].map(s=>({client:s.firstChild.clientHeight,scroll:s.firstChild.scrollHeight,width:s.getBoundingClientRect().width})),quantity:[...container.querySelectorAll('.cd-totals')].map(n=>n.textContent)};},data);
   assert(result.pages>1);assert(result.heights.every(h=>h.scroll<=h.client+1));
   assert(result.text.includes('0632094-1')||preset==='yl');
   if(!prices)assert(!result.text.includes('2.146')&&!result.text.includes('7941.68'));
   results.push({preset,prices,pages:result.pages,heights:result.heights});
   if(prices===layout.show_prices){await page.screenshot({path:path.join(out,`${preset}-preview.png`),fullPage:true});await page.pdf({path:path.join(out,`${preset}-241x139.5.pdf`),width:'241mm',height:'139.5mm',margin:{top:'0',bottom:'0',left:'0',right:'0'},printBackground:true});}
 }
 const long='超长客户用途ABC123，保留原文。'.repeat(800);
 const longResult=await page.evaluate(({layout,long})=>{
  const row={sequence:1,customer_po:'POORD040679',customer_material_code:'Z.001.000139',customer_product_name:long,specification:'520×350×300',quantity:400,unit:'套',pricing_included:true};
  const data={print_template:{layout},price_display:{shown:false},sender:{company_name:'天明包装'},customer:{name:'驿力'},delivery_number:'LONG',commercial_quantity:400,customer_document_rows:[row]};
  const columns=CustomerDeliveryPrint.visibleColumns(layout,false),container=document.getElementById('sheets');container.replaceChildren();
  const pages=CustomerDeliveryPrint.paginate(data,columns,container),index=columns.findIndex(c=>c.key==='customer_product_name');
  return{pages:pages.length,restored:pages.flat().map(e=>e.values[index]).join(''),counted:pages.flat().filter(e=>!e.continued).reduce((s,e)=>s+e.row.quantity,0)};
 },{layout:layouts.yl,long});
 assert.strictEqual(longResult.restored,long);assert.strictEqual(longResult.counted,400);assert(longResult.pages>2);
 fs.writeFileSync(path.join(out,'print-verification.json'),JSON.stringify({syntax:'passed',results,long:{pages:longResult.pages,characters:long.length,preserved:true,counted:400}},null,2));
 await browser.close();process.stdout.write(JSON.stringify({syntax:'passed',scenarios:results.length,longPages:longResult.pages}));
})().catch(e=>{process.stderr.write(e.stack);process.exitCode=1});
