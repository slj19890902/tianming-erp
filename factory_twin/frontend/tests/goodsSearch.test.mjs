import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {goodsSearchText,matchesGoodsSearch} from '../src/goodsSearch.mjs';

test('customer names match Chinese, full pinyin and initials using deployed runtime',()=>{
  const sandbox={};vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(new URL('../../../static/vendor/pinyin-pro-3.26.0.js',import.meta.url),'utf8'),sandbox);
  const text=goodsSearchText('天华 无锡市天华超净科技有限公司 TH001',sandbox.pinyinPro);
  for(const q of ['天华','tianhua','TH','wuxi tianhua','th001'])assert.ok(matchesGoodsSearch(text,q),q);
  assert.ok(!matchesGoodsSearch(text,'苏州'));
});
