const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'));
const html = fs.readFileSync('static/index.html','utf8');
const start = html.indexOf('<div id="app"');
const end = html.indexOf('<script>',start);
Vue.compile(html.slice(start,end), {decodeEntities: v=>v, onError(e){throw e;}});
for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new Function(match[1]);
console.log('Full Vue template and inline JavaScript compiled');
