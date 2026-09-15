const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('static/delivery-pick-print.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].split('    async function boot()')[0];
const context=vm.createContext({URLSearchParams,location:{search:'?task_id=5'},Intl,Date});
vm.runInContext(script+';globalThis.buildRows=buildRows;globalThis.rowHtml=rowHtml;globalThis.pageHtml=pageHtml;',context);
const task={items:[],location_groups:[{label:'北H2-10',recommended_sequence:1,lines:[
    {pick_item_id:1,component_snapshot_id:11,product_code:'205',product_name:'长片',pick_quantity:900,unit:'片'},
    {pick_item_id:1,component_snapshot_id:12,product_code:'205',product_name:'短片',pick_quantity:1200,unit:'片'},
    {pick_item_id:2,component_snapshot_id:13,product_code:'205',product_name:'套件',pick_quantity:300,unit:'套'},
    {pick_item_id:3,product_code:'152',product_name:'护角',pick_quantity:800,unit:'根',source_type:'unassigned',requires_attention:true},
]}]};
const rows=context.buildRows(task);
assert.deepStrictEqual(Array.from(rows,r=>r.productTotal),[800,900,1200,300]);
assert(context.rowHtml(rows[0]).includes('不代表可拿货'));
const page=context.pageHtml(task,{},rows,0,1);
assert(page.includes('800根、2100片、300套'));
assert(page.includes('请勿按本单默认拿齐'));
assert(!page.includes('2900'));
console.log('Print identity, separate units, missing-line warning: passed');
