const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const text = fs.readFileSync('static/shelf-camera.js','utf8').split('let cameraStream')[0];
const context = {URL, location:{origin:'http://172.16.1.26:8000',hostname:'172.16.1.26'}};
vm.createContext(context);vm.runInContext(text,context);
for(const host of ['tianmingerp0909.share.zrok.io','192.168.3.80','172.16.1.26']){
  assert.equal(context.shelfIdentity(`http://${host}:8000/q/1203`).id,'1203');
  assert.equal(context.shelfIdentity(`https://${host}/q/1203/${'a'.repeat(24)}`).product,'a'.repeat(24));
  assert.equal(context.shelfIdentity(`http://${host}/warehouse.html?tab=locations&location_id=1203`).id,'1203');
}
for(const url of ['https://evil.test/q/1203','javascript:alert(1)','http://user:password@172.16.1.26/q/1203','http://172.16.1.26/q/0','http://172.16.1.26/q/1203/invalid'])assert.equal(context.shelfIdentity(url),null);
console.log('14 identity assertions passed');
