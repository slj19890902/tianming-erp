import { useEffect, useMemo, useRef, useState } from "react";
import type { Rack } from "./types";
import { moldLocationChoices, buildMoldRackView, moldBatchPayload, moldCellSummary, moldRackEmployeeName } from "./moldRackView.mjs";
import type { MoldLocationOption } from "./moldRackView.mjs";
import { filterShelfMolds } from "./shelfDisplay.mjs";
import { createMoldRackPrinter } from "./moldRackPrint.mjs";
import type { MoldPrintState } from "./moldRackPrint.mjs";
import "./moldRack.css";

interface Mold {
  id: number; mold_code: string; mold_name: string; display_name?: string; rack_location: string; location_version: number;
  is_active?: boolean; repair_status?: string; remarks?: string; product_count: number;
  location_guide?: {level?: number | null; grid?: number | null; row?: number | null; prompt?: string | null; alias?: string | null; short_label?: string | null} | null;
  products: Array<{id: number; product_code?: string | null; product_name?: string | null; customer_name?: string | null}>;
}
interface Cell {id: string; level: number; grid: number; alias: string; location_code?: string}
interface Response {floor_code: string; rack: {rack_id: string; cells?: Cell[]; blocked_levels: number[]}; items: Mold[]; total: number; truncated: boolean}
interface Props {
  rack: Rack; response: Response | null; loading: boolean; error: string; canMoveMolds: boolean; rackIndex: number; rackCount: number;
  highlightedMoldId?: number | null; initialCellId?: string | null;
  onPrevious: () => void; onNext: () => void; onClose: () => void; onMoldMoved: (message: string) => void; onNavigationGuardChange?: (message: string) => void;
}
async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {credentials: "same-origin", ...(body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) location.assign(`/?next=${encodeURIComponent(location.pathname + location.search)}`);
    throw Object.assign(new Error(typeof data.detail === "string" ? data.detail : data.detail?.message || data.message || `读取失败 (${response.status})`), {status: response.status});
  }
  return data;
}
const name = (mold: Mold) => mold.display_name || mold.mold_name || "名称待完善";
const position = (mold: Mold) => mold.location_guide?.prompt || "位置待完善";

export function MoldRackElevation({rack, response, loading, error, canMoveMolds, rackIndex, rackCount, highlightedMoldId, initialCellId, onPrevious, onNext, onClose, onMoldMoved, onNavigationGuardChange}: Props) {
  const view = useMemo(() => buildMoldRackView({...rack, cells: response?.rack.cells || rack.mold_cells}, response?.items || [], response?.rack.blocked_levels || []), [rack, response]);
  const cells = view.levels.flatMap(level => level.cells.map(cell => ({...cell, level: level.level, key: `L${level.level}-G${cell.grid}`})));
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [query, setQuery] = useState(new URLSearchParams(location.search).get("mold_query") || "");
  const [putaway, setPutaway] = useState(false);
  const [moveMode, setMoveMode] = useState(false);
  const [search, setSearch] = useState("");
  const [candidates, setCandidates] = useState<Mold[]>([]);
  const [selection, setSelection] = useState<Mold[]>([]);
  const [options, setOptions] = useState<MoldLocationOption[]>([]);
  const [moveTarget, setMoveTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [message, setMessage] = useState("");
  const [printState, setPrintState] = useState<MoldPrintState>({busy: false, pending: false});
  const [printTemplate, setPrintTemplate] = useState("mold_80x40_v1");
  const printer = useRef<ReturnType<typeof createMoldRackPrinter> | null>(null);
  if (!printer.current) printer.current = createMoldRackPrinter({
    request: api, openWindow: url => {const popup = window.open(url, "_blank"); if (popup) popup.opener = null; return popup;},
    confirmReprint: text => window.confirm(text),
    changed: state => setPrintState(previous => ({...state, message: state.message === undefined ? previous.message : state.message})),
    makeKey: () => `mold-rack-print-${globalThis.crypto?.randomUUID?.() || Date.now() + "-" + Math.random()}`,
  });
  const attempt = useRef<ReturnType<typeof moldBatchPayload> | null>(null);
  const searchSequence = useRef(0);
  const locatedIntent = useRef("");
  const selectedCell = cells.find(cell => cell.key === selectedKey);
  const selectedMold = response?.items.find(mold => mold.id === selectedId);
  const target = moveMode ? moveTarget : selectedCell?.location_code || (selectedCell?.id ? `MCELL-${selectedCell.id}` : "");
  const locked = busy || uncertain || printState.busy || printState.pending;
  const printReady = Boolean(response && response.rack.rack_id === rack.id && !loading && !error);
  const printScope = {floorCode: response?.floor_code || "", rackId: rack.id};
  const visible = filterShelfMolds(selectedCell?.items || response?.items || [], query);
  const returnUrl = new URL(location.href); returnUrl.searchParams.set("mold_rack_id", rack.id); returnUrl.searchParams.set("floor", response?.floor_code || "1F"); if (selectedCell?.id) returnUrl.searchParams.set("mold_cell_id", selectedCell.id); if(query) returnUrl.searchParams.set("mold_query", query);
  const returnTo = returnUrl.pathname + returnUrl.search;
  useEffect(() => {setSelectedKey(null); setSelectedId(null); setPutaway(false); setMoveMode(false); setSelection([]); setMoveTarget(""); setMessage(""); attempt.current = null;}, [rack.id]);
  useEffect(() => {
    const intent = `${rack.id}/${initialCellId || ""}/${highlightedMoldId || ""}`;
    if (locatedIntent.current === intent) return;
    const found = cells.find(cell => initialCellId ? cell.id === initialCellId : highlightedMoldId ? cell.items.some(mold => mold.id === highlightedMoldId) : false);
    if (found) {locatedIntent.current = intent; setSelectedKey(found.key); setSelectedId(highlightedMoldId || null);}
  }, [rack.id, response, highlightedMoldId, initialCellId]);
  useEffect(() => {onNavigationGuardChange?.(uncertain ? "模具归位结果尚未确认，请用原凭证重试。" : printState.pending ? "请先继续上次打印，核对打印结果。" : busy || printState.busy ? "模具操作正在处理，请稍候。" : ""); return () => onNavigationGuardChange?.("");}, [busy, uncertain, printState.busy, printState.pending, onNavigationGuardChange]);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (locked || /INPUT|SELECT|TEXTAREA/.test((event.target as HTMLElement)?.tagName)) return;
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft") onPrevious();
      if (event.key === "ArrowRight") onNext();
    };
    window.addEventListener("keydown", listener); return () => window.removeEventListener("keydown", listener);
  }, [locked, onClose, onPrevious, onNext]);
  async function findMolds() {
    const sequence = ++searchSequence.current; setBusy(true); setMessage("");
    try {
      const scanned = search.match(/(?:mold_id=|\/M\/|\/m\/mold\/|\/live\/)(\d+)/);
      const result = scanned ? {items: [await api<Mold>(`/api/warehouse/molds/${scanned[1]}/detail`)]} : await api<{items: Mold[]}>(`/api/warehouse/molds?q=${encodeURIComponent(search)}&limit=100`);
      if (sequence === searchSequence.current) setCandidates(result.items);
    } catch (reason) {if (sequence === searchSequence.current) setMessage((reason as Error).message);} finally {if (sequence === searchSequence.current) setBusy(false);}
  }
  function openPutaway() {setMoveMode(false); setPutaway(true); setOptions([]); setSelection([]); setMoveTarget(""); setCandidates([]); setSearch(""); setMessage(""); attempt.current = null;}
  async function openMove(mold: Mold) {
    setMoveMode(true); setPutaway(true); setSelection([mold]); setCandidates([]); setMoveTarget(""); setMessage(""); attempt.current = null; setBusy(true);
    try {setOptions((await api<{racks: MoldLocationOption[]}>("/api/warehouse/molds/location-options")).racks);} catch (reason) {setMessage((reason as Error).message);} finally {setBusy(false);}
  }
  async function save() {
    if (!selection.length || !target || busy) return;
    if (!attempt.current) attempt.current = moldBatchPayload(selection, target, `mold-putaway-${globalThis.crypto?.randomUUID?.() || Date.now() + "-" + Math.random()}`);
    setBusy(true); setMessage("");
    try {
      const result = await api<{message?: string}>("/api/warehouse/molds/location-movement/batch", attempt.current);
      setUncertain(false); attempt.current = null; setPutaway(false); setMoveMode(false); setMoveTarget(""); setSelection([]);
      onMoldMoved(result.message || "模具归位已保存"); setMessage("归位已保存");
    } catch (reason) {
      const failure = reason as Error & {status?: number};
      if (failure.status && failure.status < 500) {attempt.current = null; setUncertain(false); setMessage(`${failure.message}；请重新查询并核对位置。`);} else {setUncertain(true); setMessage(`${failure.message}；结果未确认，请用原凭证重试。`);}
    } finally {setBusy(false);}
  }
  const labelsUrl = `/static/mold-location-label.html?floor_code=${encodeURIComponent(response?.floor_code || "")}&rack_id=${encodeURIComponent(rack.id)}`;
  return <section className="twin-rack-focus-panel twin-rack-stage twin-mold-rack-stage" role="region" aria-label={`${moldRackEmployeeName(rack)}正视图`}>
    <header><div><h2>{moldRackEmployeeName(rack)}</h2><p>{rack.width_mm} × {rack.depth_mm} × {rack.height_mm} mm · {rack.levels} 层 · {rackIndex + 1}/{rackCount}</p></div>{canMoveMolds && <div className="mold-rack-print-actions"><select aria-label="模具标签纸型" value={printTemplate} disabled={locked} onChange={event => setPrintTemplate(event.target.value)}><option value="mold_80x40_v1">40×80 mm</option><option value="mold_40x30_v1">40×30 mm</option></select><button disabled={locked || !printReady || !response?.items.length} onClick={() => void printer.current?.run(printScope, printTemplate)}>打印整架模具标签</button></div>}<a href={labelsUrl} target="_blank" rel="noreferrer">整架/格位标签</a><button disabled={locked} onClick={onClose}>返回地图</button></header>
    {(printState.message || printState.pending) && <div className="mold-rack-print-status" role="status">{printState.message}{printState.pending && <button disabled={printState.busy} onClick={() => void printer.current?.retry()}>继续上次打印</button>}</div>}
    <div className="twin-rack-content"><button className="twin-rack-switch previous" disabled={locked} aria-label="上一货架" onClick={onPrevious}>‹</button><div className="twin-elevation-shell"><div className="twin-elevation-frame">
      {[...view.levels].reverse().map(level => <div className={`twin-elevation-level mold-level ${level.blocked ? "blocked" : ""}`} key={level.level} style={{flex: `${(rack.level_heights_mm[level.level-1] || rack.height_mm) - (rack.level_heights_mm[level.level-2] || 0)} 1 0`}}><span>第 {level.level} 层</span><div style={{gridTemplateColumns: `repeat(${level.cell_count || 1}, minmax(0, 1fr))`}}>{level.blocked ? <i>设备占用层</i> : !level.cell_count ? <i>尚未分格</i> : level.cells.map(cell => {
        const key = `L${level.level}-G${cell.grid}`;
        const hit = Boolean(highlightedMoldId && cell.items.some(mold => mold.id === highlightedMoldId)) || Boolean(query && filterShelfMolds(cell.items, query).length);
        return <section className={`mold-rack-cell ${cell.items.length ? "occupied" : "empty"} ${selectedKey === key ? "selected" : ""} ${hit ? "search-match" : ""}`} key={key}><button className="mold-rack-cell-summary" disabled={locked} onClick={() => {setSelectedKey(key); setSelectedId(null); setPutaway(false); setMoveMode(false); setMoveTarget("");}}><b>{cell.alias || `第${cell.grid}格`}</b><strong>{response?.truncated ? `可见 ${cell.items.length} 块` : cell.items.length ? `有模具 · ${cell.items.length} 块` : "空格"}</strong><span>{moldCellSummary(cell.items).join(" · ")}</span>{cell.items.length > 2 && <small>查看全部</small>}</button>{canMoveMolds && <button className="mold-cell-print" aria-label={`打印${cell.alias || `第${level.level}层第${cell.grid}格`}模具标签`} disabled={locked || !printReady || !cell.items.length} onClick={() => void printer.current?.run({...printScope, ...(cell.id ? {cellId: cell.id} : {level: level.level, grid: cell.grid})}, printTemplate)}>打印本格模具标签</button>}</section>;
      })}</div></div>)}
    </div><div className="twin-width-ruler">正面宽度 {rack.width_mm} mm · 当前可见 {response?.total || 0} 块模具</div></div>
    <aside className="twin-mold-rack-aside">
      {loading && <p role="status">正在读取…</p>}{error && <p className="error">{error}</p>}
      {!loading && !error && <><h3>{selectedCell?.alias || (selectedCell ? `第${selectedCell.level}层第${selectedCell.grid}格` : "整架目录")}</h3><label className="shelf-search">查找模具<input value={query} disabled={locked} onChange={event => {setQuery(event.target.value); setSelectedKey(null); setSelectedId(null);}} placeholder="编码、图号、名称或客户" /></label>
      {selectedCell && canMoveMolds && <button disabled={locked || !target} onClick={openPutaway}>＋ 放入模具</button>}
      {selectedCell?.id && <a href={`/m/mold-cell?cell_id=${encodeURIComponent(selectedCell.id)}&return_to=${encodeURIComponent(returnTo)}`}>手机格位页</a>}
      {selectedMold ? <article className="twin-rack-product-label mold-label"><h3>{name(selectedMold)}</h3><p>{position(selectedMold)}</p><p>{selectedMold.remarks || ""}</p><p>{selectedMold.is_active === false ? "停用" : selectedMold.repair_status === "needs_repair" ? "待维修" : "正常"}</p>{selectedMold.products.map(product => <div key={product.id}>{product.product_code} · {product.customer_name} · {product.product_name}</div>)}<div className="twin-mold-rack-selected-actions"><button disabled={locked} onClick={() => setSelectedId(null)}>返回目录</button>{canMoveMolds && <button disabled={locked} onClick={() => void openMove(selectedMold)}>移动模具</button>}<a href={`/M/${selectedMold.id}?return_to=${encodeURIComponent(returnTo)}`}>手机扫码资料</a></div></article> : <><small>目录排序</small><div className="twin-mold-rack-item-list shelf-mold-directory">{visible.map(mold => <button key={mold.id} className={mold.id === highlightedMoldId ? "search-match" : ""} disabled={locked} onClick={() => {setSelectedId(mold.id); const cell = cells.find(cell => cell.items.some(item => item.id === mold.id)); if (cell) setSelectedKey(cell.key);}}><b>{name(mold)}</b><span>{mold.products.slice(0, 2).map(product => `${product.product_code || ""} ${product.customer_name || ""}`).join(" · ")}</span><small>{position(mold)}</small></button>)}</div>{!visible.length && <p>当前没有匹配模具</p>}</>}
      {putaway && <section className="twin-mold-move-panel"><h4>{moveMode ? "移动模具" : `放入 ${selectedCell?.alias || "当前格"}`}</h4>{moveMode ? <label>目标格<select value={moveTarget} disabled={locked} onChange={event => {setMoveTarget(event.target.value); attempt.current = null;}}><option value="">请选择目标格</option>{moldLocationChoices(options).map(choice => <option key={choice.value} value={choice.value}>{choice.label}</option>)}</select></label> : <><form onSubmit={event => {event.preventDefault(); void findMolds();}}><label>搜索或扫描模具<input value={search} disabled={locked} onChange={event => setSearch(event.target.value)} placeholder="编码、名称或模具二维码" /></label><button disabled={locked}>查询</button></form><div className="mold-putaway-results">{candidates.map(mold => <label key={mold.id}><input type="checkbox" disabled={locked} checked={selection.some(item => item.id === mold.id)} onChange={event => {setSelection(current => event.target.checked ? [...current, mold] : current.filter(item => item.id !== mold.id)); attempt.current = null;}} /><span><b>{name(mold)}</b><small>{mold.products.map(product => `${product.product_code || ""} ${product.customer_name || ""}`).join(" · ")} · {position(mold)}</small></span></label>)}</div></>}
      <p>已选 {selection.length} 块{selection.length ? ` · ${selection.map(name).join("、")}` : ""}</p><div className="twin-mold-move-actions"><button disabled={busy || !selection.length || !target} onClick={() => void save()}>{uncertain ? "用原凭证核对结果" : "实物已放好，保存归位"}</button><button disabled={locked} onClick={() => {setPutaway(false); setMoveMode(false); setMoveTarget(""); setOptions([]); setSelection([]);}}>取消</button></div></section>}
      {message && <p role="status">{message}</p>}{response?.truncated && <p>目录较多，请使用地图查找定位。</p>}</>}
    </aside><button className="twin-rack-switch next" disabled={locked} aria-label="下一货架" onClick={onNext}>›</button></div>
  </section>;
}
