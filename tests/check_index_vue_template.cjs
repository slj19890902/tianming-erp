// Compile the complete page, including hidden routes and dialogs, with shipped Vue.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const start = html.indexOf('<div id="app"');
const end = html.indexOf('<script', start);
if (start < 0 || end <= start) throw new Error('Application template not found');
const context = vm.createContext({ console });
vm.runInContext(fs.readFileSync(path.join(root, 'static/vendor/vue-3.5.40.global.prod.js'), 'utf8'), context);
context.Vue.compile(html.slice(start, end), {
  decodeEntities: value => value,
  onError(error) { throw error; },
});
console.log('PASS complete index Vue template compiles');
