from pathlib import Path
import re,json
ROOT=Path(__file__).resolve().parents[1]


def test_hidden_label_preview_is_measured_before_print_unlocks(tmp_path):
 import subprocess
 page=(ROOT/'static/mold-label.html').read_text('utf-8')
 utility=(ROOT/'static/assets/print-recovery.js').read_text('utf-8')
 inline=re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',page,re.S)[-1]
 helper=re.search(r'async function measureMoldLabelsWhileLoading\(\).*?(?=    async function renderMoldLabel)',inline,re.S)
 # Execute the original render function and shared load gate, not a replacement gateway.
 render=inline[inline.index('async function renderMoldLabel'):inline.index('    const frozenJobQuery=')]
 pre=helper.group(0) if helper else ''
 harness=r'''
const vm=require('vm'),expect=(x,m)=>{if(!x)throw Error(m)};global.window=global;
const cls=()=>({add(){},remove(){},toggle(){}}),make=()=>({hidden:false,disabled:false,textContent:'',innerHTML:'',style:{visibility:''},classList:cls(),addEventListener(){}});
const nodes=Object.fromEntries(['printButton','retryButton','loadState','errorBox','previewContent','batchSort','copyCount','notice','prototypeLink'].map(x=>[x,make()]));
const $=id=>nodes[id];let measured=false,failMeasure=false,fontsSettled=false;
global.document={fonts:{ready:Promise.resolve().then(()=>{fontsSettled=true})}};
const TmMoldLabelLayout={validateEnvelope(){},initializeEditor(){}};
let labelDataReady=false,sourceRows=[],moldLabelLayoutEnvelope=null,layoutEditorInitialized=false;
const wideTemplate=true,prototypeMode=false,batchMode=true,printJobId='1',CURRENT_WIDE_CATALOG='mold-edge-v1',WIDE_PRINTER_QUEUE='fictional only',templateVersion='mold_80x40_v1',h=x=>x;
async function refreshRenderedLabels(){nodes.printButton.disabled=true;expect(nodes.previewContent.hidden===false,'LABEL_MEASURED_WHILE_DISPLAY_NONE');expect(nodes.previewContent.style.visibility==='hidden','unverified label flashed before measurement');expect(fontsSettled,'fonts not ready at measurement');measured=true;if(failMeasure)throw Error('虚构长编码无法完整显示');}
global.fetch=async()=>({ok:true,status:200,text:async()=>JSON.stringify({items:[{}],label_layout:{layout:{catalog_version:'mold-edge-v1'},version:0}})});
'''+pre+render+'\nvm.runInThisContext('+json.dumps(utility)+r''');
const page=TmPrintRecovery.createPrintPage({id:'1',url:()=>'/own-fictional-test',printButton:nodes.printButton,retryButton:nodes.retryButton,loadState:nodes.loadState,errorBox:nodes.errorBox,content:nodes.previewContent,render:renderMoldLabel});
(async()=>{expect(await page.load(),'readable label did not load: '+nodes.errorBox.textContent);expect(measured,'measurement skipped');expect(!nodes.previewContent.hidden,'verified label not shown');expect(nodes.previewContent.style.visibility==='','visibility not restored');expect(!nodes.printButton.disabled,'verified label print not enabled');
 failMeasure=true;measured=false;expect((await page.load())===false,'overflow accepted');expect(measured,'overflow measurement skipped');expect(nodes.previewContent.hidden,'overflow content shown as ready');expect(nodes.printButton.disabled,'overflow enabled print');expect(nodes.errorBox.textContent.includes('无法完整显示'),'overflow reason lost');expect(nodes.previewContent.style.visibility==='','failed visibility not restored');})().catch(e=>{console.error(e.message);process.exit(1)});
'''
 p=tmp_path/'real-label-load-contract.cjs';p.write_text(harness,'utf-8')
 result=subprocess.run(['C:/Program Files/nodejs/node.exe',str(p)],capture_output=True,text=True,encoding='utf-8')
 assert result.returncode==0,result.stderr
