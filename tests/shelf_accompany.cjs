const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={};
const context={document:{getElementById:id=>nodes[id]||=( {})},URLSearchParams,
  location:{pathname:'/mobile/scan',search:''},window:{addEventListener(){}},AbortSignal};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/shelf-scan.js','utf8'),context);
assert.equal(context.bomRelations([]),'');
const text=context.bomRelations([{direction:'parent',product_code:'00139',product_name:'<纸箱>',
  quantity_per_set:4,relation:'accompany',locations:[{location_name:'三楼 北G货2',quantity:30,unit:'boxes',reservations:[{order_number:'PO-139',quantity:30}]}]}]);
assert(text.includes('父件 00139') && text.includes('每套 4') && text.includes('随货配套'));
assert(text.includes('三楼 北G货2') && text.includes('30 箱') && text.includes('PO-139'));
assert(!text.includes('<纸箱>') && text.includes('&lt;纸箱&gt;'));
assert(text.includes('位置不代表本批已预占'));
console.log('Scan relationship display, Chinese units and escaping: passed');
