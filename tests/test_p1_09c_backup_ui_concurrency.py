from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def test_backup_ui_has_one_shared_action_lock_and_retry_state() -> None:
    for marker in (
        'backupListLoading: false',
        'backupListError: ""',
        'backupAction: ""',
        'beginBackupAction(action) {',
        'finishBackupAction(action) {',
        'closeBackupCleanupPreview() {',
        '@click="loadBackups()"',
        '@click.self="closeBackupCleanupPreview"',
        '重新加载',
        'backupAction || backupListLoading || loading',
    ):
        assert marker in INDEX


def test_backup_list_only_latest_request_can_render(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the backup list race regression")
    body = _method_body("async loadBackups({throwOnError=false} = {}) {", "permissionCatalogSeed() {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.latestRequestControllers = new Map();
const pending = [];
globalThis.axios = {{ get(url, options) {{ return new Promise((resolve, reject) => pending.push({{resolve, reject, options}})); }} }};
const vm = {{
  backups: [{{filename:"stale.sqlite3"}}], backupStats: {{}}, backupListLoading:false, backupListError:"",
  beginLatestRequest(key) {{ latestRequestControllers.get(key)?.abort(); const controller=new AbortController(); latestRequestControllers.set(key,controller); return controller; }},
  finishLatestRequest(key, controller) {{ if (latestRequestControllers.get(key)===controller) latestRequestControllers.delete(key); }},
  isCancelledRequest(error) {{ return error?.name === "AbortError" || error?.code === "ERR_CANCELED"; }},
  errorMessage(error) {{ return error?.message || String(error); }}
}};
vm.loadBackups = new AsyncFunction("{{throwOnError=false}}={{}}", {json.dumps(body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const first=vm.loadBackups();
  const second=vm.loadBackups();
  pending[1].resolve({{data:{{items:[{{filename:"latest.sqlite3"}}],total_count:1,total_size:8,protected_count:1,deletable_count:0}}}});
  await second;
  pending[0].resolve({{data:{{items:[{{filename:"old.sqlite3"}}],total_count:1,total_size:4,protected_count:1,deletable_count:0}}}});
  await first;
  if (vm.backups[0]?.filename !== "latest.sqlite3") throw new Error("old backup response overwrote latest list");
  if (vm.backupListLoading) throw new Error("latest list loading did not finish");
  const failed=vm.loadBackups();
  pending[2].reject(new Error("网络断开"));
  await failed;
  if (vm.backups.length) throw new Error("failed current refresh retained stale list");
  if (!vm.backupListError.includes("网络断开")) throw new Error("refresh failure was not surfaced");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    target = tmp_path / "backup-list-race.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_create_backup_rejects_repeat_click_and_reports_refresh_failure(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the backup action regression")
    begin_body = _method_body("beginBackupAction(action) {", "finishBackupAction(action) {")
    finish_body = _method_body("finishBackupAction(action) {", "closeBackupCleanupPreview() {")
    create_body = _method_body("async createBackup() {", "async deleteBackup(row) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
let resolvePost;
let postCount=0;
globalThis.axios={{post(){{postCount+=1;return new Promise(resolve=>{{resolvePost=resolve;}});}}}};
const messages=[];
const vm={{
  loading:false, backupAction:"",
  loadBackups:async()=>false,
  showToast:(message,error=false)=>messages.push({{message,error}}),
  errorMessage:error=>error?.message || String(error)
}};
vm.beginBackupAction=new Function("action", {json.dumps(begin_body, ensure_ascii=False)}).bind(vm);
vm.finishBackupAction=new Function("action", {json.dumps(finish_body, ensure_ascii=False)}).bind(vm);
vm.createBackup=new AsyncFunction({json.dumps(create_body, ensure_ascii=False)}).bind(vm);
(async()=>{{
  const first=vm.createBackup();
  const second=await vm.createBackup();
  if (postCount !== 1 || second !== false) throw new Error("repeat backup click was not blocked");
  resolvePost({{data:{{filename:"created.sqlite3"}}}});
  const result=await first;
  if (!result || postCount !== 1) throw new Error("first backup did not finish exactly once");
  if (vm.backupAction || vm.loading) throw new Error("backup action lock was not released");
  if (!messages.some(row=>row.message.includes("已创建") && row.message.includes("列表刷新失败"))) throw new Error("successful backup with failed refresh was misreported");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    target = tmp_path / "backup-action-lock.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_every_backup_file_action_uses_the_shared_guard() -> None:
    for name, next_name, action in (
        ("createBackup", "deleteBackup", "create"),
        ("deleteBackup", "previewCleanup", "delete"),
        ("previewCleanup", "confirmCleanup", "preview"),
        ("confirmCleanup", "loadMaterialMapping", "cleanup"),
    ):
        block = INDEX.split(f"async {name}(", 1)[1].split(f"async {next_name}(", 1)[0]
        assert f'beginBackupAction("{action}")' in block
        assert f'finishBackupAction("{action}")' in block


def test_backup_frontend_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
