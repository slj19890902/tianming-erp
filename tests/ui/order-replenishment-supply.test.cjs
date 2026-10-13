const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.resolve(__dirname,'../../static/index.html'),'utf8');
function method(name) {
  const match=html.match(new RegExp('^\\s*'+name+'\\([^\\n]*\\) \\{','m'));assert.ok(match,name);
  const end=html.indexOf('\n          },',match.index);
  return vm.runInNewContext('({'+html.slice(match.index,end+13).trim().replace(/,$/,'')+'})')[name];
}
const text=method('orderReplenishmentSupplyText'),stage=method('orderReplenishmentSupplyStage');
test('采用服务端父套覆盖，绝不把BOM子件片数相加当套数',()=>{
  assert.equal(text({replenishment_supply:{covered_finished_quantity:20,covered_piece_quantity:140,
    remaining_purchase_finished_quantity:0,unit_label:'套',sources:[{},{}]}}),'补库覆盖 20套');
});
test('部分覆盖明确剩余报料用量，无供应不展示虚构数字',()=>{
  assert.equal(text({replenishment_supply:{covered_finished_quantity:20,remaining_purchase_finished_quantity:60,
    unit_label:'片',sources:[{}]}}),'补库覆盖 20片 · 仍需报料 60片用量');
  assert.equal(text({replenishment_supply:{covered_finished_quantity:0,remaining_purchase_finished_quantity:20,
    unit_label:'套',sources:[{}]}}),'部分组件已有补库安排 · 仍需报料 20套用量');
  assert.equal(text({}),'');
});
test('供应各阶段中文展示，来源默认折叠且保持真实实存字段',()=>{
  for(const [key,label] of Object.entries({pending_material:'补库待报料',pending_incoming:'补库待收料',
    pending_processing:'补库待加工',finished:'已有实物待安排'})) assert.equal(stage(key),label);
  const match=html.match(/<details[^>]*class="order-replenishment-supply"[\s\S]*?<\/details>/);
  assert.ok(match);assert.doesNotMatch(match[0],/\bopen(?:=|\s|>)/);
  assert.match(match[0],/total_coverable_finished_quantity/);assert.match(match[0],/含补库可覆盖合计/);
  assert.doesNotMatch(match[0],/可送库存|finished_inventory_reserved_qty\s*=/);
});
