const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js', 'utf8'));
const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('<details v-for="row in supplierSettlements"');
const end = html.indexOf('<div v-if="canViewFinanceCosts"', start);
// The section ends with the supplier panel closure, outside the row template.
const section = html.slice(start, end);
const template = section.slice(0, section.lastIndexOf('</details>') + 10);
try { Vue.compile(template, {decodeEntities: value => value, onError(error) { throw error; }}); }
catch (error) { console.error(error.message, error.loc); process.exit(1); }
console.log('Supplier Vue template compiled');
