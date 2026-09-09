import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
const html=readFileSync(new URL('../../../static/production-packaging-label.html',import.meta.url),'utf8');
test('Chrome fits complete long code and Chinese name into two unclipped lines, including custom layouts',()=>{
  const css=html.match(/<style>([\s\S]*?)<\/style>/)[1];
  const funcs=html.slice(html.indexOf('      function fitElement('),html.indexOf('      function validateRenderedLabels('));
  const dir=mkdtempSync(join(tmpdir(),'tm-label-wrap-')),file=join(dir,'test.html');
  writeFileSync(file,`<meta charset="utf-8"><style>${css}</style><pre id="result"></pre><script>${funcs}
  const checks=[];
  for(const value of ['Z.001.000138-LONG-CUSTOMER-CODE-20260909','超长产品名称水泵电机包装防震内衬纸箱配套产品名称','ABC123'.repeat(30),'短名称']){
    for(const custom of [true,false]){
      const box=document.createElement('div');box.className=custom?'layout-print-element':'fit-cell';box.style.cssText='position:relative;width:36mm;height:5mm;font-size:3.6mm';
      const text=document.createElement('strong');text.className=custom?'':'fit-value';text.textContent=value;box.append(text);document.body.append(box);
      checks.push(fitElement(text));const r=text.getBoundingClientRect(),b=box.getBoundingClientRect();
      checks.push(text.textContent===value,r.top>=b.top-.1,r.bottom<=b.bottom+.1,text.scrollWidth<=box.clientWidth,r.height<=parseFloat(getComputedStyle(text).fontSize)*1.15*2+.1);
      if(value.length===37)checks.push(r.height>parseFloat(getComputedStyle(text).fontSize)*1.2);
    }
  }
  document.querySelector('#result').textContent=checks.every(Boolean)?'PASS':JSON.stringify(checks);
  </script>`);
  const result=spawnSync('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--user-data-dir='+join(dir,'profile'),'--dump-dom',pathToFileURL(file).href],{encoding:'utf8',timeout:30000});
  assert.equal(result.status,0,result.stderr);assert.ok(result.stdout.includes('<pre id="result">PASS</pre>'),result.stdout.match(/<pre id="result">.*?<\/pre>/)?.[0] || result.stdout.slice(-1800));
});
