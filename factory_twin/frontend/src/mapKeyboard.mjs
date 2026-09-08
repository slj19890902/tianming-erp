export const MAP_KEYBOARD_HINT = "方向键短按10mm；按住连续移动并加速；Shift+方向键精调1mm；松键停止";

// Movement is clock-driven, never multiplied by the operating system repeat rate.
export function createMapKeyboard({ begin, now = () => performance.now(), requestFrame = requestAnimationFrame, cancelFrame = cancelAnimationFrame }) {
  let active = null;
  let frame = null;
  const stop = () => {
    if (frame !== null) cancelFrame(frame);
    frame = null;
    const previous = active;
    active = null;
    previous?.gesture.finish();
  };
  const tick = () => {
    frame = null;
    if (!active) return;
    const current = now();
    const from = Math.max(active.last, active.started + 350, current - 50);
    const acceleration = active.started + 1500;
    const slow = Math.max(0, Math.min(current, acceleration) - from);
    const fast = Math.max(0, current - Math.max(from, acceleration));
    active.last = current;
    active.carry += active.fine ? Math.max(0, current - from) * .02 : slow * .1 + fast * .3;
    const step = Math.floor(active.carry + 1e-8);
    active.carry -= step;
    if (step) active.gesture.move(step, active.key);
    if (active) frame = requestFrame(tick);
  };
  return {
    down(event) {
      if (!/^Arrow(Up|Down|Left|Right)$/.test(event.key)) return;
      if (event.altKey || event.ctrlKey || event.metaKey || event.target?.closest?.("input,select,textarea,[contenteditable]:not([contenteditable=false]),[role=dialog],[role=menu],button,a,summary")) { stop(); return; }
      if (active?.key === event.key) { event.preventDefault(); return; }
      if (event.repeat) return;
      // Changing direction stays in the same edit transaction; only the latest
      // direction's release commits, so an older keyup cannot save mid-move.
      const gesture = active?.gesture || begin(event.key);
      if (!gesture) return;
      if (frame !== null) cancelFrame(frame);
      event.preventDefault();
      const started = now();
      active = { key: event.key, gesture, started, last: started, carry: 0, fine: event.shiftKey };
      gesture.move(event.shiftKey ? 1 : 10, event.key);
      frame = requestFrame(tick);
    },
    up(event) { if (active?.key === event.key) { event.preventDefault(); stop(); } },
    stop,
  };
}
