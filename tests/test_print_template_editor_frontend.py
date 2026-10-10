"""Frontend contract checks for reusable editable print templates."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDITOR_PATH = ROOT / "static" / "assets" / "print-template-editor.js"
EDITOR = EDITOR_PATH.read_text(encoding="utf-8")
PAGES = {
    "customer_quotation": (ROOT / "static" / "quotation-print.html").read_text(encoding="utf-8"),
    "legacy_requisition": (ROOT / "static" / "requisition-print.html").read_text(encoding="utf-8"),
    "mold_label": (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8"),
    "customer_list": (ROOT / "static" / "customers.html").read_text(encoding="utf-8"),
}


EXPECTED_FIELDS = {
    "customer_quotation": {
        "document_title", "customer_label", "quotation_number_label", "quotation_date_label",
        "column_sequence", "column_temporary_code", "column_product_name", "column_specification",
        "column_material", "column_quantity", "column_unit_price", "column_remarks", "remarks_label",
        "address_label", "phone_label", "contact_label",
    },
    "legacy_requisition": {
        "document_title", "supplier_label", "number_label", "date_label", "column_sequence",
        "column_board_size", "column_crease", "column_material", "column_quantity", "column_remarks",
        "address_label", "phone_label",
    },
    "mold_label": {
        "qr_prompt", "mold_number_label", "product_code_label", "specification_label",
        "additional_products_prefix", "additional_products_suffix",
    },
    "customer_list": {
        "document_title", "column_id", "column_customer_code", "column_customer_name",
        "column_contact", "column_phone", "column_address", "column_credit_terms",
        "column_product_count", "column_history_order_count", "column_file_count", "column_status",
    },
}


def _node() -> str:
    executable = shutil.which("node")
    if executable is None:
        raise AssertionError("Node.js is required for print-template frontend checks")
    return executable


def _declared_keys(source: str) -> set[str]:
    html_keys = re.findall(r'data-print-edit-key="([a-z0-9_]+)"', source)
    script_keys = re.findall(r'dataset\.printEditKey\s*=\s*"([a-z0-9_]+)"', source)
    return set(html_keys + script_keys)


def _inline_scripts(source: str) -> list[str]:
    return [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL)
        if script.strip()
    ]


def test_pages_use_exact_backend_field_catalogs_and_shared_asset() -> None:
    for template_key, source in PAGES.items():
        assert f'templateKey:"{template_key}"' in source or f'templateKey: "{template_key}"' in source
        assert '/static/assets/print-template-editor.js' in source
        assert _declared_keys(source) == EXPECTED_FIELDS[template_key]
        assert 'data-print-action="print"' in source


def test_dynamic_business_values_are_not_editable_template_fields() -> None:
    quotation = PAGES["customer_quotation"]
    requisition = PAGES["legacy_requisition"]
    mold = PAGES["mold_label"]
    customers = PAGES["customer_list"]
    for forbidden in ("company_name", "customer_name", "quotation_no", "quotation_date", "unit_price"):
        assert f'data-print-edit-key="{forbidden}"' not in quotation
    for forbidden in ("company_name", "supplier_name", "requisition_number", "requisition_date"):
        assert f'data-print-edit-key="{forbidden}"' not in requisition
    for forbidden in ("mold_name", "mold_code", "rack_location", "product_name", "lookup_url"):
        assert f'data-print-edit-key="{forbidden}"' not in mold
    assert 'data-print-edit-key="customer_row"' not in customers
    assert "print_action_label" not in "\n".join(PAGES.values())


def test_labels_own_their_punctuation_and_dynamic_nodes_are_separate() -> None:
    quotation = PAGES["customer_quotation"]
    requisition = PAGES["legacy_requisition"]
    mold = PAGES["mold_label"]
    for label in ("客户：", "报价单号：", "报价日期：", "备注：", "地址：", "电话：", "联系人："):
        assert label in quotation
    for label in ("TO：", "NO：", "日期：", "地址：", "电话："):
        assert label in requisition
    assert "模具编号：" in mold
    assert '<span id="customer"></span>' in quotation
    assert '<span id="number"></span>' in requisition
    assert '<span id="moldCode"></span>' in mold


def test_shared_editor_contract_has_revision_restore_plain_text_and_guards() -> None:
    for marker in (
        "PrintTemplateEditor",
        "applyTo(target = this.root)",
        "expected_revision: this.revision",
        '"/restore"',
        'getData("text/plain")',
        '"beforeunload"',
        '"beforeprint"',
        '"keydown"',
        "stopImmediatePropagation",
        "setAfterApply(callback)",
        "if (this.onApplied) await this.onApplied(this.value(), reason)",
        "nativeUnsavedPrint",
        "onBeforePrint",
        "printTemplateApplyCallbackFailed",
    ):
        assert marker in EDITOR
    assert ".innerHTML" not in EDITOR
    assert "_printAllowedOnce" not in EDITOR


def test_shared_editor_applies_server_fields_to_nodes_rendered_after_initial_get(tmp_path: Path) -> None:
    harness = tmp_path / "print-template-editor-harness.js"
    harness.write_text(
        f"""
const assert = require('assert');
class FakeElement {{
  constructor(tag='div') {{
    this.nodeType=1; this.tagName=tag.toUpperCase(); this.dataset={{}}; this.textContent='';
    this.children=[]; this.attributes={{}}; this.style={{}}; this.hidden=false; this.ownerDocument=null;
    const values=new Set(); this.classList={{add:(v)=>values.add(v), [Symbol.iterator]:function*(){{yield* values;}}}};
  }}
  appendChild(node) {{ node.ownerDocument=this.ownerDocument; this.children.push(node); return node; }}
  addEventListener() {{}} removeEventListener() {{}}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }} removeAttribute(k) {{ delete this.attributes[k]; }}
  matches(selector) {{ return selector==='[data-print-edit-key]' && !!this.dataset.printEditKey; }}
  querySelectorAll(selector) {{
    const found=[]; const visit=(node)=>{{
      if(selector==='[data-print-edit-key]' && node.dataset && node.dataset.printEditKey) found.push(node);
      (node.children||[]).forEach(visit);
    }}; this.children.forEach(visit); return found;
  }}
  querySelector(selector) {{ return this.querySelectorAll(selector)[0] || null; }}
  contains(node) {{ return node===this || this.children.includes(node); }}
  closest() {{ return this; }} focus() {{}}
}}
class FakeDocument extends FakeElement {{
  constructor() {{ super('document'); this.nodeType=9; this.ownerDocument=null; this.head=new FakeElement('head'); this.head.ownerDocument=this; }}
  createElement(tag) {{ const node=new FakeElement(tag); node.ownerDocument=this; return node; }}
  createTextNode(text) {{ return {{nodeType:3,textContent:text,ownerDocument:this}}; }}
  getElementById(id) {{ return this.head.children.find((node)=>node.id===id) || null; }}
}}
const document=new FakeDocument();
let appliedCalls=0;
const window={{
  document,
  addEventListener() {{}}, removeEventListener() {{}}, print() {{}}, alert() {{}}, confirm() {{return true;}},
  fetch: async()=>({{ok:true,status:200,json:async()=>({{
    template_key:'late_template',revision:7,can_manage:true,fields:{{document_title:'服务端保存标题'}}
  }})}}),
}};
global.window=window;
require({json.dumps(str(EDITOR_PATH))});
(async()=>{{
  const editor=window.PrintTemplateEditor.create({{templateKey:'late_template',root:document,onApplied:()=>{{appliedCalls+=1;}}}});
  await editor.ready;
  assert.strictEqual(editor.value().fields.document_title,'服务端保存标题');
  assert.strictEqual(editor.value().revision,7);
  assert.strictEqual(appliedCalls,1);
  const lateNode=document.createElement('h1'); lateNode.dataset.printEditKey='document_title'; lateNode.textContent='源码默认标题';
  document.appendChild(lateNode);
  editor.applyTo(document);
  assert.strictEqual(lateNode.textContent,'服务端保存标题');
  assert.strictEqual(editor.value().fields.document_title,'服务端保存标题');
  assert.strictEqual(editor.value().revision,7);
  assert.strictEqual(editor.value().dirty,false);
  assert.strictEqual(appliedCalls,1);
  console.log('ok');
}})().catch((error)=>{{console.error(error);process.exit(1);}});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [_node(), str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_shared_editor_executes_print_guards_and_keeps_server_success_when_page_callback_fails(tmp_path: Path) -> None:
    harness = tmp_path / "print-template-editor-behavior-harness.js"
    harness.write_text(
        f"""
const assert = require('assert');
class FakeEventTarget {{
  constructor() {{ this.listeners={{}}; }}
  addEventListener(type,handler) {{ (this.listeners[type] ||= []).push(handler); }}
  removeEventListener(type,handler) {{ this.listeners[type]=(this.listeners[type]||[]).filter(item=>item!==handler); }}
  dispatchEvent(event) {{
    event.target ||= this;
    for (const handler of [...(this.listeners[event.type]||[])]) handler.call(this,event);
    return !event.defaultPrevented;
  }}
}}
class FakeElement extends FakeEventTarget {{
  constructor(tag='div') {{
    super(); this.nodeType=1; this.tagName=tag.toUpperCase(); this.dataset={{}}; this.textContent='';
    this.children=[]; this.parentNode=null; this.attributes={{}}; this.style={{}}; this.hidden=false;
    this.disabled=false; this.ownerDocument=null; this.id='';
    const values=new Set(); this.classList={{add:(v)=>values.add(v), [Symbol.iterator]:function*(){{yield* values;}}}};
  }}
  appendChild(node) {{ node.ownerDocument=this.ownerDocument; node.parentNode=this; this.children.push(node); return node; }}
  removeChild(node) {{ this.children=this.children.filter(item=>item!==node); node.parentNode=null; return node; }}
  setAttribute(key,value) {{ this.attributes[key]=String(value); }}
  getAttribute(key) {{ return Object.prototype.hasOwnProperty.call(this.attributes,key) ? this.attributes[key] : null; }}
  removeAttribute(key) {{ delete this.attributes[key]; }}
  matches(selector) {{ return selector==='[data-print-edit-key]' && !!this.dataset.printEditKey; }}
  querySelectorAll(selector) {{
    const found=[];
    const matches=(node)=>{{
      if (selector==='[data-print-edit-key]') return !!node.dataset?.printEditKey;
      if (selector==='[data-print-edit-key][contenteditable="true"]') return !!node.dataset?.printEditKey && node.getAttribute('contenteditable')==='true';
      return false;
    }};
    const visit=(node)=>{{ if(matches(node)) found.push(node); (node.children||[]).forEach(visit); }};
    this.children.forEach(visit); return found;
  }}
  querySelector(selector) {{ return this.querySelectorAll(selector)[0] || null; }}
  contains(node) {{ return node===this || this.children.some(child=>child.contains?.(node)); }}
  closest(selector) {{
    if (selector==='[data-print-edit-key]' && this.dataset.printEditKey) return this;
    return this.parentNode?.closest?.(selector) || null;
  }}
  focus() {{}}
}}
class FakeDocument extends FakeElement {{
  constructor() {{
    super('document'); this.nodeType=9; this.ownerDocument=null;
    this.head=new FakeElement('head'); this.head.ownerDocument=this;
  }}
  createElement(tag) {{ const node=new FakeElement(tag); node.ownerDocument=this; return node; }}
  createTextNode(text) {{ return {{nodeType:3,textContent:text,ownerDocument:this}}; }}
  getElementById(id) {{
    let result=null;
    const visit=(node)=>{{ if(node.id===id) result=node; if(!result) (node.children||[]).forEach(visit); }};
    visit(this.head); if(!result) visit(this); return result;
  }}
}}
const makeEvent=(type,extra={{}})=>({{
  type,defaultPrevented:false,immediateStopped:false,
  preventDefault(){{this.defaultPrevented=true;}},
  stopImmediatePropagation(){{this.immediateStopped=true;}},
  ...extra,
}});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const document=new FakeDocument();
const windowTarget=new FakeEventTarget();
let fetchMode='print';
let confirmAnswers=[];
let confirmCalls=0;
const alerts=[];
const window=Object.assign(windowTarget,{{
  document,
  alert(message){{alerts.push(String(message));}},
  confirm(){{confirmCalls+=1; return confirmAnswers.shift() ?? false;}},
  print(){{}},
  fetch:async(url,options={{}})=>{{
    const method=options.method || 'GET';
    const payload=method==='PUT'
      ? {{template_key:'apply_failure',revision:2,can_manage:true,fields:{{document_title:'服务端已保存'}}}}
      : {{template_key:fetchMode==='save' ? 'apply_failure' : 'print_guard',revision:1,can_manage:true,fields:{{document_title:'系统标题'}}}};
    return {{ok:true,status:200,json:async()=>payload}};
  }},
}});
global.window=window;
require({json.dumps(str(EDITOR_PATH))});

function buildSurface() {{
  document.children=[];
  const toolbar=document.createElement('div');
  const printButton=document.createElement('button');
  const field=document.createElement('h1');
  field.dataset.printEditKey='document_title'; field.textContent='系统标题';
  toolbar.appendChild(printButton); document.appendChild(toolbar); document.appendChild(field);
  return {{toolbar,printButton,field}};
}}

(async()=>{{
  const first=buildSurface();
  let printCalls=0;
  let prepareCalls=0;
  const editor=window.PrintTemplateEditor.create({{
    templateKey:'print_guard',root:document,toolbar:first.toolbar,printButton:first.printButton,
    onBeforePrint:()=>{{prepareCalls+=1;}},
    onPrint:()=>{{printCalls+=1; window.dispatchEvent(makeEvent('beforeprint'));}},
  }});
  await editor.ready;

  const cleanShortcut=makeEvent('keydown',{{key:'p',ctrlKey:true}});
  window.dispatchEvent(cleanShortcut); await flush(); await flush();
  assert.strictEqual(cleanShortcut.defaultPrevented,true);
  assert.strictEqual(cleanShortcut.immediateStopped,true);
  assert.strictEqual(printCalls,1);
  assert.strictEqual(prepareCalls,2); // routed print preparation + browser beforeprint
  assert.strictEqual(confirmCalls,0);
  printCalls=0;
  prepareCalls=0;

  assert.strictEqual(editor.startEditing(),true);
  first.field.textContent='未保存标题';
  document.dispatchEvent(makeEvent('input',{{target:first.field}}));
  assert.strictEqual(editor.value().dirty,true);

  confirmAnswers=[false,true,false];
  const cancelledShortcut=makeEvent('keydown',{{key:'p',ctrlKey:true}});
  window.dispatchEvent(cancelledShortcut); await flush();
  assert.strictEqual(cancelledShortcut.defaultPrevented,true);
  assert.strictEqual(printCalls,0);

  const acceptedShortcut=makeEvent('keydown',{{key:'P',ctrlKey:true}});
  window.dispatchEvent(acceptedShortcut); await flush(); await flush();
  assert.strictEqual(printCalls,1);
  assert.strictEqual(prepareCalls,2); // button preparation + the browser beforeprint event

  const nextShortcut=makeEvent('keydown',{{key:'p',metaKey:true}});
  window.dispatchEvent(nextShortcut); await flush();
  assert.strictEqual(printCalls,1); // prior approval cannot leak into the next print request
  assert.strictEqual(confirmCalls,3);

  const nativeMenuPrint=makeEvent('beforeprint');
  window.dispatchEvent(nativeMenuPrint); await flush();
  assert.strictEqual(nativeMenuPrint.defaultPrevented,false);
  assert.strictEqual(confirmCalls,3); // beforeprint never makes a misleading cancellable prompt
  assert.strictEqual(prepareCalls,3);
  assert.match(editor.toolbars().status.textContent,/原生打印无法由页面取消/);
  editor.destroy();

  document.children=[];
  const segmentToolbar=document.createElement('div');
  const segmentPrintButton=document.createElement('button');
  segmentToolbar.appendChild(segmentPrintButton); document.appendChild(segmentToolbar);
  const firstSegment=document.createElement('span');
  firstSegment.dataset.printEditKey='document_title'; firstSegment.dataset.printEditSegment='true'; firstSegment.textContent='系统';
  const secondSegment=document.createElement('span');
  secondSegment.dataset.printEditKey='document_title'; secondSegment.dataset.printEditSegment='true'; secondSegment.textContent='标题';
  document.appendChild(firstSegment); document.appendChild(secondSegment);
  const segmentEditor=window.PrintTemplateEditor.create({{
    templateKey:'print_guard',root:document,toolbar:segmentToolbar,printButton:segmentPrintButton,
  }});
  await segmentEditor.ready;
  segmentEditor.startEditing();
  firstSegment.textContent='系统新';
  document.dispatchEvent(makeEvent('input',{{target:firstSegment}}));
  assert.strictEqual(segmentEditor.value().fields.document_title,'系统新标题');
  assert.strictEqual(firstSegment.textContent,'系统新');
  assert.strictEqual(secondSegment.textContent,'标题');
  segmentEditor.destroy();

  fetchMode='save';
  const second=buildSurface();
  const saveEditor=window.PrintTemplateEditor.create({{
    templateKey:'apply_failure',root:document,toolbar:second.toolbar,printButton:second.printButton,
    onApplied:(_value,reason)=>{{if(reason==='save') throw new Error('页面回调故障');}},
  }});
  await saveEditor.ready;
  saveEditor.startEditing();
  second.field.textContent='准备保存';
  document.dispatchEvent(makeEvent('input',{{target:second.field}}));
  const saved=await saveEditor.save();
  assert.strictEqual(saved,true);
  assert.deepStrictEqual(saveEditor.value().fields,{{document_title:'服务端已保存'}});
  assert.strictEqual(saveEditor.value().revision,2);
  assert.strictEqual(saveEditor.value().dirty,false);
  assert.strictEqual(saveEditor.value().editing,false);
  assert.strictEqual(second.field.textContent,'服务端已保存');
  assert.match(saveEditor.toolbars().status.textContent,/已保存，但页面刷新失败/);
  assert.match(alerts.at(-1),/已保存，但页面刷新失败/);
  assert.doesNotMatch(alerts.at(-1),/请求失败/);
  console.log('ok');
}})().catch((error)=>{{console.error(error);process.exit(1);}});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [_node(), str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_shared_asset_and_page_inline_javascript_are_syntax_valid(tmp_path: Path) -> None:
    result = subprocess.run(
        [_node(), "--check", str(EDITOR_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    for template_key, source in PAGES.items():
        scripts = _inline_scripts(source)
        assert len(scripts) == 1
        target = tmp_path / f"{template_key}.js"
        target.write_text(scripts[0], encoding="utf-8")
        result = subprocess.run(
            [_node(), "--check", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
