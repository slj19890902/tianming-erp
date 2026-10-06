"""Execute the page's request helper to distinguish authentication from outages."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('status,detail,expected', [
    (401, None, '登录已失效'),
    (403, None, '没有查看权限'),
    (500, None, '库存数据读取失败（500）'),
    (503, None, '库存数据读取失败（503）'),
    (403, '当前库存助手仅对老板和管理员开放', '当前库存助手仅对老板和管理员开放'),
])
def test_inventory_assistant_request_errors(status, detail, expected):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for page helper verification')
    page = (Path(__file__).resolve().parents[1] / 'static/inventory-assistant.html').read_text(encoding='utf-8')
    helper = next(line for line in page.splitlines() if line.startswith('async function get('))
    script = helper + '\n' + f"""
    global.fetch = async () => ({{ok:false,status:{status},json:async()=>({json.dumps({'detail': detail}, ensure_ascii=False)})}});
    get('/api/inventory-assistant').then(()=>process.exit(1)).catch(error=>{{
        if(!error.message.includes({json.dumps(expected, ensure_ascii=False)})) throw error;
        if({status}>=500 && error.message.includes('登录')) throw error;
    }});
    """
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True, encoding='utf-8')
