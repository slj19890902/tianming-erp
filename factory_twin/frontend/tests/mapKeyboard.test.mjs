import test from "node:test";
import assert from "node:assert/strict";
import { createMapKeyboard } from "../src/mapKeyboard.mjs";

function harness(allowed = true) {
  let time = 0, callback, total = 0, finished = 0, prevented = 0;
  const keys = [];
  const controller = createMapKeyboard({ now: () => time,
    requestFrame: fn => { callback = fn; return 1; }, cancelFrame: () => { callback = null; },
    begin: key => { if (!allowed) return; keys.push(key); return { move: n => total += n, finish: () => finished++ }; } });
  const event = (key = "ArrowRight", extra = {}) => ({ key, preventDefault: () => prevented++, ...extra });
  return { controller, event, keys, get total() { return total; }, get finished() { return finished; }, get prevented() { return prevented; },
    advance(to, interval = 10) { while (time < to) { time = Math.min(to, time + interval); const fn = callback; callback = null; fn?.(); } } };
}
test("short press moves 10mm exactly once; Shift moves 1mm", () => {
  for (const fine of [false, true]) { const h = harness(); h.controller.down(h.event(undefined, { shiftKey: fine })); h.advance(300); h.controller.up(h.event());
    assert.equal(h.total, fine ? 1 : 10); assert.equal(h.finished, 1); h.advance(1000); assert.equal(h.finished, 1); }
});
test("hold waits 350ms, then 100mm/s, accelerates after 1.5s", () => {
  const h = harness(); h.controller.down(h.event()); h.advance(350); assert.equal(h.total, 10);
  h.advance(1350); assert.equal(h.total, 110); h.advance(1500); assert.equal(h.total, 125);
  h.advance(2500); assert.equal(h.total, 425); h.controller.up(h.event()); assert.equal(h.finished, 1);
});
test("OS repeats cannot change speed or restart gesture", () => {
  const h = harness(); h.controller.down(h.event()); for (let i = 0; i < 40; i++) h.controller.down(h.event(undefined, { repeat: true }));
  assert.equal(h.total, 10); assert.equal(h.keys.length, 1); h.advance(1350); assert.equal(h.total, 110);
});
test("Shift hold stays slow even after acceleration threshold", () => {
  const h = harness(); h.controller.down(h.event(undefined, { shiftKey: true })); h.advance(2350); assert.equal(h.total, 41);
});
test("direction changes stay in one gesture; old keyup does not stop the latest direction", () => {
  const h = harness(); h.controller.down(h.event()); h.controller.down(h.event("ArrowUp")); assert.equal(h.finished, 0);
  h.controller.up(h.event()); h.advance(1350); assert.equal(h.finished, 0); assert.equal(h.total, 120);
  h.controller.up(h.event("ArrowUp")); assert.equal(h.finished, 1); assert.equal(h.keys.length, 1);
});
test("stop is idempotent and cancels frames; stale repeat cannot resume", () => {
  const h = harness(); h.controller.down(h.event()); h.controller.stop(); h.controller.stop(); h.advance(9000);
  h.controller.down(h.event(undefined, { repeat: true })); assert.equal(h.total, 10); assert.equal(h.finished, 1);
});
test("background frame delays do not create large jumps", () => {
  const h = harness(); h.controller.down(h.event()); h.advance(9000, 9000); assert.equal(h.total, 25);
});
test("input fields, dialogs, modifier shortcuts and unavailable selections remain untouched", () => {
  for (const extra of [{ ctrlKey: true }, { altKey: true }, { metaKey: true }, { target: { closest: () => ({}) } }]) {
    const h = harness(); h.controller.down(h.event(undefined, extra)); assert.equal(h.total, 0); assert.equal(h.prevented, 0);
  }
  const h = harness(false); h.controller.down(h.event()); assert.equal(h.prevented, 0);
});
