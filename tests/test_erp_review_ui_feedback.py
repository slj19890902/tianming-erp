from pathlib import Path
import shutil
import subprocess


INDEX = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")


def method(start: str, end: str) -> str:
    return INDEX[INDEX.index(start):INDEX.index(end, INDEX.index(start))].strip().rstrip(",")


def test_errors_survive_success_timeout_and_can_be_closed() -> None:
    script = r"""
const assert = require('node:assert/strict');
let next = 0; const timers = new Map();
const setTimeout = fn => { timers.set(++next, fn); return next; };
const clearTimeout = id => timers.delete(id);
const flush = () => { for (const [id, fn] of [...timers]) { timers.delete(id); fn(); } };
const ui = {toast: {message:'', error:false}, toastTimer:null, METHODS};
ui.showToast('保存成功'); assert.equal(timers.size, 1);
flush(); assert.equal(ui.toast.message, '');
ui.showToast('保存成功'); ui.showToast('已写入但回读失败', true);
flush(); assert.equal(ui.toast.message, '已写入但回读失败');
assert.equal(ui.toast.error, true); assert.equal(timers.size, 0);
ui.dismissToast(); assert.equal(ui.toast.message, '');
ui.showToast('再次保存成功'); ui.dismissToast();
assert.equal(timers.size, 0);
""".replace("METHODS", method("showToast(message, error=false)", "toggleVersionGroup(groupKey)"))
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_frame_load_is_not_confused_with_application_response() -> None:
    script = r"""
const assert = require('node:assert/strict');
const window = {location: {origin:'http://localhost:12345'}};
const frameWindow = {};
const ui = {warehouseFrameState:'loading', $refs:{warehouseFrame:{contentWindow:frameWindow}},
 isWarehouseTwinFloorCode: value => value === '3F', METHODS};
ui.warehouseFrameLoaded(); assert.equal(ui.warehouseFrameState, 'loaded');
const data = {source:'tianming-warehouse', type:'warehouse-state', floor_code:'3F'};
ui.handleWarehouseFrameMessage({origin:'http://other.example', source:frameWindow, data});
assert.equal(ui.warehouseFrameState, 'loaded');
ui.handleWarehouseFrameMessage({origin:window.location.origin, source:{}, data});
assert.equal(ui.warehouseFrameState, 'loaded');
ui.handleWarehouseFrameMessage({origin:window.location.origin, source:frameWindow, data:{...data, type:'other'}});
assert.equal(ui.warehouseFrameState, 'loaded');
ui.handleWarehouseFrameMessage({origin:window.location.origin, source:frameWindow, data});
assert.equal(ui.warehouseFrameState, 'responsive');
assert.equal(ui.warehouseTwinFloor, '3F');
ui.warehouseFrameLoaded(); assert.equal(ui.warehouseFrameState, 'responsive');
""".replace("METHODS", method("warehouseFrameLoaded() {", "selectWarehouseFloor(floorCode)"))
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
