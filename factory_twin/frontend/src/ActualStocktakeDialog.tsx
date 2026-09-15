import { useEffect, useRef, useState } from "react";

/** The existing actual-count workflow owns snapshots, permissions and writes. */
export function ActualStocktakeDialog({ locationId, onClose }: { locationId: number; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
    const receive = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== frame.current?.contentWindow) return;
      if (event.data?.type === "warehouse-actual-stocktake-busy") setBusy(event.data.busy === true);
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, []);
  return <dialog ref={dialog} aria-label="本货位实际数量盘点" onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}
    style={{ width: "min(1000px, 96vw)", maxWidth: "96vw", height: "92dvh", maxHeight: "92dvh", padding: 0, border: "1px solid #cbd5e1", borderRadius: 12 }}>
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 16px", height: 48, boxSizing: "border-box" }}>
      <strong>实盘数量 · 可增可减</strong><button type="button" disabled={busy} onClick={onClose}>{busy ? "正在保存…" : "返回地图"}</button>
    </div>
    <iframe ref={frame} title="填写本货位各批次实际数量" src={`/mobile/stocktake.html?location_id=${encodeURIComponent(locationId)}&embedded=1`}
      style={{ width: "100%", height: "calc(100% - 48px)", border: 0, display: "block" }} />
  </dialog>;
}
