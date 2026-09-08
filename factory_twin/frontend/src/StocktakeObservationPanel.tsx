import { useEffect, useRef, useState } from "react";

interface Observation {
  id: number; version: number; inventory_keyword: string; customer_keyword?: string | null;
  reported_quantity?: number | null; reported_unit?: string | null; reason: string;
  resolution_note?: string | null; resolved_at?: string | null;
}
const endpoint = "/api/mobile/erp/warehouse/unmatched-inventory-observations";
async function request(path: string, init?: RequestInit) {
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...init });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "处理失败，请刷新后重试");
  return data;
}
export function StocktakeObservationPanel({ locationId, observations, canResolve, onResolved }: {
  locationId: number; observations: Observation[]; canResolve: boolean; onResolved: () => Promise<unknown>;
}) {
  const [history, setHistory] = useState<Observation[]>([]);
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const keys = useRef<Record<number, string>>({});
  const historyPath = `${endpoint}?status=resolved&location_id=${locationId}&limit=200`;
  useEffect(() => {
    if (!canResolve) return;
    let active = true;
    request(historyPath).then(data => { if (active) setHistory(data.items); })
      .catch(error => { if (active) setMessage(`已处理记录读取失败：${error.message}`); });
    return () => { active = false; };
  }, [historyPath, canResolve]);
  const resolve = async (item: Observation) => {
    if (busy !== null || !canResolve) return;
    setBusy(item.id); setMessage("");
    keys.current[item.id] ||= `stocktake-resolve-${item.id}-${`${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
    try {
      await request(`${endpoint}/${item.id}/resolve`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_version:item.version,
          resolution_note:notes[item.id]?.trim() || "管理员已完成现场核对和处理",
          idempotency_key:keys.current[item.id] }) });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "处理失败，请刷新后重试");
      setBusy(null); return;
    }
    try {
      await onResolved();
      const data = await request(historyPath); setHistory(data.items);
      setMessage("已标记为已处理；该库位没有其他待处理异常时，红色标记自动取消。");
    } catch {
      setMessage("已处理保存成功，但地图或记录刷新失败，请刷新页面；无需重复处理。");
    } finally { setBusy(null); }
  };
  return <div aria-label="盘点异常处理">
    {observations.length > 0 && <div className="twin-unmatched-observation">
      <b>现场货物异常 · 待管理员处理</b>
      {observations.map(item => <div key={item.id} style={{marginTop:10}}>
        <strong>{item.inventory_keyword}</strong>{item.customer_keyword ? ` · ${item.customer_keyword}` : ""}
        {item.reported_quantity != null ? ` · ${item.reported_quantity}${item.reported_unit || ""}` : ""}
        <p>{item.reason}</p>
        {canResolve && <><input aria-label="处理说明（选填）" placeholder="处理说明（选填）" maxLength={500}
          value={notes[item.id] || ""} disabled={busy !== null}
          onChange={event => setNotes({...notes,[item.id]:event.target.value})} />
          <button type="button" disabled={busy !== null} onClick={() => void resolve(item)}>
            {busy === item.id ? "保存中…" : "标记已处理"}</button></>}
      </div>)}
    </div>}
    {message && <p role="status">{message}</p>}
    {history.length > 0 && <details style={{marginTop:10,color:"#166534"}}>
      <summary>已处理记录（{history.length}）</summary>
      {history.map(item => <div key={item.id} style={{marginTop:8}}>
        <strong>已处理 · {item.inventory_keyword}</strong>
        {item.reported_quantity != null ? ` · ${item.reported_quantity}${item.reported_unit || ""}` : ""}
        <p>{item.resolution_note}</p><small>{item.resolved_at ? new Date(item.resolved_at).toLocaleString() : ""}</small>
      </div>)}
    </details>}
  </div>;
}
