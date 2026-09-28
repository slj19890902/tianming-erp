import json
from tests.test_p1_09c_71_order_item_editor_guard import _method_body, _run_node


def test_quantity_save_retries_exact_request_and_blocks_double_click(tmp_path):
    body = _method_body('async saveOrderQuantityEditor() {', 'async openOrderItem(order, item) {')
    script = '''
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const calls=[];let resolve,reject;
global.axios={put:(url,body)=>{calls.push({url,body});return new Promise((a,b)=>{resolve=a;reject=b})}};
const form={item_id:10147,quantity:300,expected_quantity:600,expected_revision:0,
 minimum_quantity:300,key:'quantity-test',saving:false,submitted:null};
const vm={orderQuantityEditor:form,showToast(){},loadOrders:async()=>{vm.refreshed=true}};
const expect=(x,s)=>{if(!x)throw Error(s)};
''' + f'vm.save=new AsyncFunction({json.dumps(body,ensure_ascii=False)}).bind(vm);' + '''
(async()=>{
 const first=vm.save();await vm.save();expect(calls.length===1,'double click submitted twice');
 reject(Error('connection lost'));await first;expect(form.submitted,'uncertain request was discarded');
 form.quantity=400;const retry=vm.save();
 expect(calls[1].body.quantity===300 && calls[1].body===calls[0].body,'retry changed payload');
 resolve({data:{quantity:300}});await retry;
 expect(vm.orderQuantityEditor===null && vm.refreshed,'save did not close and reload orders');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    _run_node(script,tmp_path,'quantity-save.js')


def test_quantity_editor_latest_response_wins(tmp_path):
    body = _method_body('async openOrderQuantityEditor(order, item) {', 'async saveOrderQuantityEditor() {')
    script = '''
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
global.latestRequestControllers=new Map();const calls=[];
global.axios={get:()=>new Promise((resolve,reject)=>calls.push({resolve,reject}))};
const vm={beginLatestRequest(key){const c={signal:{}};latestRequestControllers.set(key,c);return c},
 displayOrderNumber:o=>o.id,isCancelledRequest:()=>false,showToast(){throw Error('unexpected error')}};
''' + f'vm.open=new AsyncFunction("order","item",{json.dumps(body,ensure_ascii=False)}).bind(vm);' + '''
(async()=>{
 const a=vm.open({id:1},{id:1});const b=vm.open({id:2},{id:2});
 calls[1].resolve({data:{item_id:2,quantity:600}});await b;
 calls[0].resolve({data:{item_id:1,quantity:600}});await a;
 if(vm.orderQuantityEditor.item_id!==2)throw Error('stale response replaced editor');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    _run_node(script,tmp_path,'quantity-open.js')
