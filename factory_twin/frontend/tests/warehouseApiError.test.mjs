import test from 'node:test';
import assert from 'node:assert/strict';
import {apiErrorMessage} from '../src/warehouseApiError.mjs';
test('422 reveals row and field without echoing request input',()=>{
 assert.equal(apiErrorMessage({detail:[{loc:['body','items',0,'stock_date'],type:'date_from_datetime_parsing',input:'private-input',msg:'invalid date'}]},422),'第1项·库存日期：请填写有效日期');
 assert.equal(apiErrorMessage({detail:[{loc:['body','idempotency_key'],type:'string_too_long'}]},422),'提交凭证：内容超过允许长度');
});
test('business rejection and authentication/network fallback remain readable',()=>{
 assert.equal(apiErrorMessage({detail:'货位版本已变化'},409),'货位版本已变化');
 assert.equal(apiErrorMessage({},401),'登录状态已失效');
 assert.equal(apiErrorMessage({},503),'请求失败（503）');
});
