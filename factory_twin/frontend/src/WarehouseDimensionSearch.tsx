import { useEffect, useRef, useState } from "react";
import "./warehouseDimensionSearch.css";

export interface DimensionStock {
  id: number; location_id: number; floor: number | string | null; area_code: string | null;
  placement_status: string; location_name: string; name: string; code: string; customer: string;
  dimensions: Array<number | null>; flute: string | null; unit: string;
  physical: number; available: number; reserved: number; damaged: number;
}
interface Result { total: number; items: DimensionStock[]; offset: number; limit: number }
type Operator = "near" | "eq" | "ge" | "le";
const axes = ["长", "宽", "高"];
const keys = ["length", "width", "height"];
const size = 15;
export function WarehouseDimensionSearch({ request, onLocate }: {
  request: <T>(url: string, signal?: AbortSignal) => Promise<T>;
  onLocate: (item: DimensionStock) => void;
}) {
  const [kind, setKind] = useState("board");
  const [values, setValues] = useState(["", "", ""]);
  const [ops, setOps] = useState<Operator[]>(["near", "near", "near"]);
  const [flute, setFlute] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  useEffect(() => () => { sequence.current++; active.current?.abort(); }, []);
  const invalidate = () => {
    sequence.current++; active.current?.abort(); setBusy(false); setResult(null); setError("");
  };
  const search = async (offset = 0) => {
    active.current?.abort(); const serial = ++sequence.current;
    const controller = new AbortController(); active.current = controller;
    const query = new URLSearchParams({ kind, offset: String(offset), limit: String(size) });
    const selected = values.slice(0, kind === "board" ? 2 : 3);
    if (!selected.some(Boolean) && !flute) { setError("请输入尺寸或选择楞型"); return; }
    if (selected.some(value => value !== "" && !/^[1-9]\d{0,4}$/.test(value))) {
      setError("尺寸填1至99999毫米"); return;
    }
    selected.forEach((value, index) => { if (value) { query.set(keys[index], value); query.set(keys[index] + "_op", ops[index]); } });
    if (flute) query.set("flute", flute);
    setBusy(true); setError("");
    try {
      const data = await request<Result>("/api/mobile/erp/warehouse/dimension-stock?" + query, controller.signal);
      if (serial === sequence.current) setResult(data);
    } catch (e) { if (serial === sequence.current && !controller.signal.aborted) { setError((e as Error).message); setResult(null); } }
    finally { if (serial === sequence.current) setBusy(false); }
  };
  return <section className="twin-dimension-search" aria-label="详细查找">
    <form onSubmit={event => { event.preventDefault(); void search(); }}>
      <div className="dimension-types">
        <label>类型<select value={kind} onChange={e => { invalidate(); setKind(e.target.value); }}><option value="board">纸板 / 半成品</option><option value="box">纸箱 / 成品</option></select></label>
        <label>楞型<select value={flute} onChange={e => { invalidate(); setFlute(e.target.value); }}><option value="">全部</option>{["A","B","C","E","F","AB","BC","BE","EB","AC","ABC","NONE"].map(v => <option key={v} value={v}>{v === "NONE" ? "卡纸 / 无楞" : v}</option>)}</select></label>
      </div>
      <div className="dimension-fields">{axes.slice(0, kind === "board" ? 2 : 3).map((axis, index) => <label key={axis}>
        <span>{axis} mm</span><select aria-label={`${axis}匹配条件`} value={ops[index]} onChange={e => { invalidate(); setOps(old => old.map((v, i) => i === index ? e.target.value as Operator : v)); }}>
          <option value="near">接近</option><option value="eq">等于</option><option value="ge">≥</option><option value="le">≤</option>
        </select><input aria-label={`${axis}尺寸`} inputMode="numeric" maxLength={5} value={values[index]} placeholder={axis} onChange={e => {
          invalidate(); const value = e.target.value.replace(/\D/g, "").slice(0, 5); setValues(old => old.map((v, i) => i === index ? value : v));
        }} />
      </label>)}</div>
      <div className="dimension-actions"><span>全楼层 · 越接近越靠前</span><button type="submit" disabled={busy}>{busy ? "查询中…" : "查询"}</button></div>
    </form>
    {error && <p role="alert">{error}</p>}
    {result && <><div className="dimension-summary" aria-live="polite">{result.total} 个库存批次</div>
      <div className="dimension-results" aria-busy={busy}>{result.items.map(item => <button type="button" key={item.id} disabled={busy} onClick={() => onLocate(item)}>
        <b>{item.code || item.name}</b><strong>{item.physical} {item.unit}</strong>
        <span>{item.customer} · {item.name}</span>
        <span>{item.dimensions.map(v => v ?? "—").join("×")} mm {item.flute === "NONE" ? "卡纸" : item.flute}</span>
        <span className="dimension-location">{item.location_name || "位置待确认"}{item.placement_status !== "placed" ? " · 待归位" : ""}</span>
        <small>可用 {item.available} · 预占 {item.reserved}{item.damaged ? ` · 损坏 ${item.damaged}` : ""}</small>
      </button>)}</div>
      {result.total === 0 && <p>没有符合条件的库存</p>}
      <nav className="dimension-actions" aria-label="详细查找分页"><button type="button" disabled={busy || !result.offset} onClick={() => void search(Math.max(0, result.offset - size))}>上一页</button><span>{Math.floor(result.offset / size) + 1} / {Math.max(1, Math.ceil(result.total / size))}</span><button type="button" disabled={busy || result.offset + size >= result.total} onClick={() => void search(result.offset + size)}>下一页</button></nav>
    </>}
  </section>;
}
