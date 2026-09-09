const fs=require('fs'),vm=require('vm');
vm.runInThisContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'));
const html=fs.readFileSync('static/index.html','utf8');
const template=html.slice(html.indexOf('<div id="app"'),html.indexOf('<script>',html.indexOf('<div id="app"')));
try { Vue.compile(template,{decodeEntities:value=>value,onError(error){throw error;}}); }
catch(error){console.error(error.message,error.loc);process.exit(1);}
console.log('Full app Vue template compiled');
