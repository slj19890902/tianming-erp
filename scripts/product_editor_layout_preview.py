"""Loopback-only, no-database component preview of the real product editor.

Uses production template, styles, component and methods. Authentication and
save are deliberately mocked; this is layout evidence, NOT API acceptance.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def preview():
    html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
    head = html.split('</head>', 1)[0] + '</head>'
    block = html.split('<div v-else-if="modal.type === \'product\'">', 1)[1].split(
        '<div v-else-if="modal.type === \'finishedStockPolicy\'"', 1)[0]
    actions = html.split('<div v-if="modal?.type === \'product\'" class="modal-foot product-editor-actions">', 1)[1].split('</div>', 1)[0]
    scripts = ''.join(re.findall(r'<script\b[^>]*>.*?</script>', html.split('</head>', 1)[1], re.S))
    scripts = scripts.replace('app.mount("#app");', '''
      const previewData = app._component.data;
      app._component.data = function() {
        const state = previewData.call(this);
        state.user = {id:1,role:'admin',ui_mode:'standard'};
        state.modal = {type:'product',title:'编辑产品 · 隔离布局预览'};
        state.productForm = {...state.productForm, id:1, customer_id:1,
          product_code:'DEMO-00148',product_name:'布局测试内衬',box_style:'衬板',
          supply_mode:'corrugated_production',combination_mode:'parent_priced_set',
          composite_fulfillment_mode:'parent_delivery',_material_supplier:'全部供应商',
          _production_processes:['无需结合'],_printing_situation:'无印刷'};
        state.customers = [{id:1,name:'布局测试客户',is_active:true}];
        state.bomEditor.enabled = true;
        state.bomEditor.components = [{_key:'a',component_product_id:2,quantity_per_set:2,is_required:true},
          {_key:'b',component_product_id:3,quantity_per_set:6,is_required:true}];
        state.bomEditor.componentOptions = [{id:2,_label:'DEMO-L · 长片'}, {id:3,_label:'DEMO-S · 短片'}];
        return state;
      };
      app._component.mounted = function() {};
      app._component.watch = {};
      app._component.methods.hasPermission = () => true;
      app._component.methods.saveModal = function() { this.bomEditor.notice='布局预览：未写入数据库'; };
      axios.get = async () => ({data:[]});
      for (const method of ['post','put','patch','delete']) axios[method] = async () => { throw new Error('Preview: API writes disabled'); };
      app.mount("#app");
    ''')
    body = '''<body><div id="app"><div class="modal-mask">
      <div class="modal product-editor-modal"><div class="modal-head"><strong>编辑产品 · 隔离布局预览（不写数据库）</strong></div>
      <div class="modal-body"><div>''' + block + '''</div><div class="modal-foot product-editor-actions">''' + actions + '''</div></div></div></div>'''
    return (head + body + scripts + '</body></html>').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/preview':
            body, kind = preview(), 'text/html; charset=utf-8'
        elif self.path.startswith('/static/'):
            path = (ROOT / self.path.split('?', 1)[0].lstrip('/')).resolve()
            if not path.is_relative_to(ROOT / 'static') or not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            kind = 'text/css' if path.suffix == '.css' else 'application/javascript'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    print('Layout-only preview: http://127.0.0.1:18219/preview', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 18219), Handler).serve_forever()
