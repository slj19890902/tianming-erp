from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LABEL = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
UTILITY_PATH = ROOT / "static" / "assets" / "print-recovery.js"
UTILITY = UTILITY_PATH.read_text(encoding="utf-8")


def _inline_script(page: str) -> str:
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", page, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    return scripts[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_mold_label_disables_print_until_recoverable_load_succeeds() -> None:
    for marker in (
        'id="printButton" disabled',
        'id="retryButton"',
        'id="loadState"',
        'id="errorBox"',
        'id="previewContent"',
        "print-recovery.js",
        "TmPrintRecovery.createPrintPage",
        "async function loadMoldLabel",
        "正在读取模具标签",
        "onUnauthorized:loginNext",
    ):
        assert marker in LABEL
    assert 'onclick="window.print()"' not in LABEL
    assert "document.body.innerHTML" not in LABEL


def test_mold_label_keeps_formal_and_prototype_sources() -> None:
    assert 'prototypeMode?"/api/auth/me"' in LABEL
    assert "/api/warehouse/molds/${id}/label" in LABEL
    assert "sourceRows=prototypeRows()" in LABEL
    assert "sourceRows=batchMode?(data.items||[]):[data]" in LABEL
    assert "打印匿名测试标签" in LABEL
    assert "正式标签二维码仍进入登录后的模具查询" in LABEL


def test_shared_recovery_redirects_unauthorized_and_labels_render_failures(
    tmp_path: Path,
) -> None:
    harness = f"""
const vm=require("vm");global.window=global;
class FakeAbortController{{constructor(){{this.signal={{aborted:false}}}}abort(){{this.signal.aborted=true}}}};global.AbortController=FakeAbortController;
const classList=()=>({{add(){{}},remove(){{}},toggle(){{}},contains(){{return false}}}});
const node=()=>({{textContent:"",disabled:false,hidden:false,classList:classList(),listeners:{{}},addEventListener(t,f){{this.listeners[t]=f}}}});
const nodes=Object.fromEntries(["print","retry","load","error","content"].map(id=>[id,node()]));
vm.runInThisContext({json.dumps(UTILITY, ensure_ascii=False)});
let redirected=false;global.fetch=async()=>({{ok:false,status:401,headers:{{get:()=>"REQ-AUTH"}},text:async()=>""}});
const page=TmPrintRecovery.createPrintPage({{id:"7",url:id=>`/${{id}}`,printButton:nodes.print,retryButton:nodes.retry,loadState:nodes.load,errorBox:nodes.error,content:nodes.content,loadingMessage:"读取",invalidMessage:"无效",authMessage:"登录失效",failureMessage:"失败",render(){{}},print(){{}},onUnauthorized:()=>{{redirected=true}}}});
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  await page.load();expect(redirected,"401 did not redirect");expect(nodes.print.disabled,"401 unlocked print");
  redirected=false;global.fetch=async()=>({{ok:true,status:200,headers:{{get:()=>null}},text:async()=>JSON.stringify({{id:7}})}});
  const broken=TmPrintRecovery.createPrintPage({{id:"7",url:id=>`/${{id}}`,printButton:nodes.print,retryButton:nodes.retry,loadState:nodes.load,errorBox:nodes.error,content:nodes.content,loadingMessage:"读取",invalidMessage:"无效",authMessage:"登录失效",failureMessage:"失败",render(){{throw new TypeError("坏标签")}},print(){{}}}});
  await broken.load();expect(nodes.error.textContent.includes("打印内容无法显示"),"render error misclassified");expect(!nodes.error.textContent.includes("无法连接 ERP"),"render error shown as network failure");expect(nodes.print.disabled,"render failure unlocked print");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "mold-label-recovery-behavior.js")


def test_mold_label_and_shared_utility_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    for name, source in (
        ("print-recovery.js", UTILITY),
        ("mold-label.js", _inline_script(LABEL)),
    ):
        target = tmp_path / name
        target.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stderr
