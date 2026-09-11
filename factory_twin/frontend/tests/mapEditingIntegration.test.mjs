import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import * as THREE from "three";
import * as visuals from "../src/factoryVisuals.mjs";
import { createMapKeyboard } from "../src/mapKeyboard.mjs";
const source = readFileSync(new URL("../src/EditorCanvas.tsx", import.meta.url), "utf8");
const syntax = ts.createSourceFile("EditorCanvas.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let keyboardEffect;
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(syntax) === "useEffect" && node.arguments[0].getText(syntax).includes("createMapKeyboard")) keyboardEffect = node.arguments[0].getText(syntax);
  ts.forEachChild(node, visit);
}
visit(syntax);
function mount(kind = "rack", draggable = true, readOnly = false) {
  const listeners = {}, calls = [], node = new THREE.Group(); node.userData.draggable = draggable;
  const runtime = { camera: { quaternion: new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2) },
    entityNodes: new Map([[`${kind}:a`, node]]), requestRender() {} };
  const track = name => (...args) => calls.push([name, ...args]);
  const context = { THREE, readOnly, viewMode: "2d", selectedRef: { current: { kind, id: "a" } }, runtimeRef: { current: runtime },
    layoutRef: { current: { racks: [{ id: "a", x_mm: 2000, y_mm: 2000 }], bounds_mm: { min_x: 0, max_x: 10000, min_y: 0, max_y: 10000 } } },
    focusTargetRef: { current: null }, moveStatesRef: { current: undefined }, syncEntityHighlights() {},
    nudgeHandlers: { current: { onMoveRack: track("rack"), onNudgeFeature: track("feature"), onFinishFeatureNudge: track("finishFeature"),
      onNudgePallet: track("pallet"), onFinishPalletNudge: track("finishPallet"), featureEditingEnabled: true } },
    createMapKeyboard: options => createMapKeyboard({ ...options, now: () => 0, requestFrame: () => 1, cancelFrame() {} }),
    requestAnimationFrame: fn => fn(),
    window: { addEventListener: (name, fn) => listeners[name] = fn, removeEventListener: name => delete listeners[name] },
    document: { hidden: false, addEventListener() {}, removeEventListener() {} } };
  const code = ts.transpileModule(`globalThis.cleanup = (${keyboardEffect})();`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(code, context);
  return { context, calls, node, listeners, event: { key: "ArrowRight", preventDefault() {} } };
}
test("rack keyboard previews and commits absolute coordinates only once at release", () => {
  const h = mount(); h.listeners.keydown(h.event); assert.equal(h.node.position.x, -2990); assert.equal(h.calls.length, 0);
  h.listeners.keyup(h.event); assert.deepEqual(h.calls, [["rack", "a", 2010, 2000]]); h.context.cleanup(); assert.equal(h.calls.length, 1);
});
test("areas and pallets retain their authorized nudge and finish pipelines", () => {
  for (const kind of ["feature", "pallet"]) {
    const h = mount(kind); h.listeners.keydown(h.event); h.listeners.blur();
    assert.equal(h.calls[0][0], kind); assert.equal(h.calls[0][1], "a"); assert.equal(h.calls[0][2], 10);
    assert.equal(h.calls[1][0], kind === "feature" ? "finishFeature" : "finishPallet");
    if (kind === "feature") assert.equal(h.calls[1][1], "a"); h.context.cleanup();
  }
});
test("locked entities and lookup/read-only mode cannot be nudged", () => {
  const locked = mount("rack", false); locked.listeners.keydown(locked.event); assert.equal(locked.calls.length, 0); locked.context.cleanup();
  const lookup = mount("rack", true, true); assert.equal(lookup.listeners.keydown, undefined);
});
test("selection/mode cleanup and pointer interactions finish without a runaway gesture", () => {
  for (const action of ["cleanup", "pointerdown"]) {
    const h = mount(); h.listeners.keydown(h.event);
    if (action === "cleanup") h.context.cleanup(); else { h.listeners.pointerdown(); h.listeners.keyup(h.event); h.context.cleanup(); }
    assert.equal(h.calls.length, 1);
  }
});
test("rack visuals have four orange rails inside footprint in 2D and 2.5D, preserving red conflict body", () => {
  const scene = readFileSync(new URL("../src/industrialScene.ts", import.meta.url), "utf8");
  const exports = {};
  const code = ts.transpileModule(scene, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  vm.runInNewContext(code, { exports, require: name => name === "three" ? THREE : name.includes("factoryVisuals") ? visuals : {} });
  for (const mode of ["2d", "25d"]) for (const theme of [true, false]) {
    const group = exports.buildRackVisual({ width_mm: 2600, depth_mm: 1200, height_mm: 2600, levels: 3, bays: 3, access_side: "north" }, mode, true, false, theme);
    const rails = group.children.filter(node => node.material?.color?.getHexString() === "f97316");
    assert.equal(rails.length, 4);
    for (const rail of rails) { const box = new THREE.Box3().setFromObject(rail); assert.ok(box.min.x >= -1300 && box.max.x <= 1300); assert.ok(box.min.z >= -600 && box.max.z <= 600); }
    assert.ok(group.children.some(node => ["dc2626", "fecaca"].includes(node.material?.color?.getHexString())));
  }
});
