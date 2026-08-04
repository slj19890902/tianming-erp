import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { EditorCanvas } from "./EditorCanvas";
import { nextFeatureCode, resolveFeatureCode } from "./featureCodes.mjs";
import type { FeatureKind } from "./featureCodes.mjs";
import type {
  AssetTemplate,
  LayerVisibility,
  Layout,
  LayoutFeature,
  LayoutSummary,
  Placement,
  Rack,
  SelectedEntity,
  ViewMode
} from "./types";

type PlacementDraft = Pick<Placement, "name" | "width_mm" | "depth_mm" | "height_mm" | "x_mm" | "y_mm">;
type RackDraft = Pick<Rack, "name" | "width_mm" | "depth_mm" | "height_mm" | "levels" | "bays" | "access_side" | "min_aisle_width_mm">;
type FeatureVerticalDraft = Pick<LayoutFeature, "storage_mode" | "elevation_mm" | "storage_height_mm">;

const initialLayers: LayerVisibility = {
  structures: true,
  equipment: true,
  racks: true,
  zones: true,
  aisles: true,
  noGo: true,
  labels: true
};

const zoneTypes = [
  ["raw_material", "原料"], ["semi_finished", "半成品"], ["finished_wait_delivery", "成品待送"],
  ["mold", "模具"], ["printing_plate", "印刷版"], ["abnormal_isolation", "异常隔离"], ["temporary_turnover", "临时周转"]
];
const aisleTypes = [["pedestrian", "人行"], ["forklift", "叉车"], ["fire", "消防"], ["loading", "装卸"]];
const noGoTypes = [
  ["fire_exit", "消防出口"], ["electrical_box", "配电箱"], ["maintenance", "设备检修区"],
  ["door_swing", "门扇开启区"], ["freight_elevator", "货梯口"]
];

export default function App() {
  const [layouts, setLayouts] = useState<LayoutSummary[]>([]);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [assets, setAssets] = useState<AssetTemplate[]>([]);
  const [selected, setSelected] = useState<SelectedEntity>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("2d");
  const [layers, setLayers] = useState<LayerVisibility>(initialLayers);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("正在读取布局…");
  const [placementDraft, setPlacementDraft] = useState<PlacementDraft | null>(null);
  const [rackDraft, setRackDraft] = useState<RackDraft | null>(null);
  const [featureVerticalDraft, setFeatureVerticalDraft] = useState<FeatureVerticalDraft | null>(null);
  const [drawMode, setDrawMode] = useState<FeatureKind | null>(null);
  const [drawPoints, setDrawPoints] = useState<number[][]>([]);
  const [rackTemplate, setRackTemplate] = useState({
    rack_code: "RACK-1F-001", name: "纸板货架", width_mm: 4000, depth_mm: 1100,
    height_mm: 2600, levels: 3, bays: 4, access_side: "south", min_aisle_width_mm: 3000,
    rotation_deg: 0, color: "#8b5cf6", source: "manual"
  });
  const [featureDraft, setFeatureDraft] = useState({
    feature_kind: "zone" as "zone" | "aisle" | "no_go", subtype: "raw_material",
    feature_code: "ZONE-1F-RAW-001", name: "原料堆放区", width_mm: 3000,
    direction: "two_way", no_stacking: false, storage_mode: "floor" as "floor" | "rack" | "overhead",
    elevation_mm: 0, storage_height_mm: 1000, color: "#60a5fa", source: "manual"
  });

  const loadLayout = useCallback(async (id: string) => {
    const next = await api.getLayout(id);
    setLayout(next);
    return next;
  }, []);
  const refreshLists = useCallback(async () => {
    const [layoutItems, assetItems] = await Promise.all([api.listLayouts(), api.listAssets()]);
    setLayouts(layoutItems);
    setAssets(assetItems);
    if (layoutItems.length) {
      const first = await loadLayout(layoutItems[0].id);
      setMessage(`已加载 ${first.name}；语义对象均使用毫米坐标`);
    }
  }, [loadLayout]);
  useEffect(() => { refreshLists().catch((error: Error) => setMessage(error.message)); }, [refreshLists]);

  const selectedPlacement = useMemo(
    () => selected?.kind === "equipment" ? layout?.placements.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  const selectedRack = useMemo(
    () => selected?.kind === "rack" ? layout?.racks.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  const selectedFeature = useMemo(
    () => selected?.kind === "feature" ? layout?.features.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  useEffect(() => {
    setPlacementDraft(selectedPlacement ? {
      name: selectedPlacement.name, width_mm: selectedPlacement.width_mm, depth_mm: selectedPlacement.depth_mm,
      height_mm: selectedPlacement.height_mm, x_mm: selectedPlacement.x_mm, y_mm: selectedPlacement.y_mm
    } : null);
  }, [selectedPlacement]);
  useEffect(() => {
    setRackDraft(selectedRack ? {
      name: selectedRack.name, width_mm: selectedRack.width_mm, depth_mm: selectedRack.depth_mm,
      height_mm: selectedRack.height_mm, levels: selectedRack.levels, bays: selectedRack.bays,
      access_side: selectedRack.access_side, min_aisle_width_mm: selectedRack.min_aisle_width_mm
    } : null);
  }, [selectedRack]);
  useEffect(() => {
    setFeatureVerticalDraft(selectedFeature?.feature_kind === "zone" ? {
      storage_mode: selectedFeature.storage_mode,
      elevation_mm: selectedFeature.elevation_mm,
      storage_height_mm: selectedFeature.storage_height_mm
    } : null);
  }, [selectedFeature]);
  useEffect(() => {
    if (!layout || drawMode) return;
    setFeatureDraft((current) => {
      const featureCode = resolveFeatureCode(
        layout,
        current.feature_kind,
        current.subtype,
        current.feature_code
      );
      return featureCode === current.feature_code
        ? current
        : { ...current, feature_code: featureCode };
    });
  }, [layout, drawMode]);

  const reload = useCallback(async (text: string) => {
    if (!layout) return;
    const next = await loadLayout(layout.id);
    setMessage(`${text}；规则检查 ${next.violations.length} 项`);
  }, [layout, loadLayout]);
  const chooseLayout = async (id: string) => {
    setBusy(true);
    try { const next = await loadLayout(id); setSelected(null); setMessage(`已切换到 ${next.name}`); }
    catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  };
  const importDxf = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setMessage("正在只读解析 DXF…");
    try {
      const imported = await api.importDxf(new FormData(event.currentTarget));
      setLayout(imported); setSelected(null); setLayouts(await api.listLayouts());
      setMessage(`导入完成：识别 ${imported.structures.length} 个底图实体`);
      event.currentTarget.reset();
    } catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  };
  const createAsset = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setBusy(true);
    try {
      const asset = await api.createAsset(new FormData(event.currentTarget));
      setAssets((current) => [...current, asset]); setMessage(`素材 ${asset.name} 已加入设备库`);
      event.currentTarget.reset();
    } catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  };

  const dropAsset = useCallback(async (templateId: string, xMm: number, yMm: number) => {
    if (!layout) return;
    try {
      const item = await api.createPlacement(layout.id, { template_id: templateId, x_mm: xMm, y_mm: yMm, rotation_deg: 0 });
      await reload(`设备候选坐标已保存：X ${xMm} / Y ${yMm} mm`);
      setSelected({ kind: "equipment", id: item.id });
    } catch (error) { setMessage((error as Error).message); }
  }, [layout, reload]);
  const moveEquipment = useCallback(async (id: string, xMm: number, yMm: number) => {
    const current = layout?.placements.find((item) => item.id === id);
    if (!current) return;
    try {
      await api.updatePlacement(id, { version: current.version, x_mm: xMm, y_mm: yMm });
      await reload(`设备坐标已保存：X ${xMm} / Y ${yMm} mm`);
    } catch (error) { setMessage((error as Error).message); await loadLayout(layout!.id); }
  }, [layout, reload, loadLayout]);
  const savePlacement = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selectedPlacement || !placementDraft) return;
    try { await api.updatePlacement(selectedPlacement.id, { version: selectedPlacement.version, ...placementDraft }); await reload("设备属性已保存"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const rotatePlacement = async () => {
    if (!selectedPlacement) return;
    try { await api.updatePlacement(selectedPlacement.id, { version: selectedPlacement.version, rotation_deg: selectedPlacement.rotation_deg + 90 }); await reload("设备已旋转 90°"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const toggleEquipmentLock = async () => {
    if (!selectedPlacement) return;
    if (selectedPlacement.is_locked && !window.confirm("确认解除设备锁定吗？解除后设备可再次移动。")) return;
    try {
      await api.updatePlacement(selectedPlacement.id, {
        version: selectedPlacement.version,
        is_locked: !selectedPlacement.is_locked,
        is_confirmed: selectedPlacement.is_locked ? selectedPlacement.is_confirmed : true
      });
      await reload(selectedPlacement.is_locked ? "设备已解除锁定" : "设备已确认并锁定");
    } catch (error) { setMessage((error as Error).message); }
  };
  const removePlacement = async () => {
    if (!selectedPlacement || !window.confirm(`确认移除 ${selectedPlacement.name} 吗？`)) return;
    try { await api.deletePlacement(selectedPlacement.id); setSelected(null); await reload("设备候选已移除"); }
    catch (error) { setMessage((error as Error).message); }
  };

  const dropRack = useCallback(async (payload: Record<string, unknown>, xMm: number, yMm: number) => {
    if (!layout) return;
    try {
      const rack = await api.createRack(layout.id, { ...payload, x_mm: xMm, y_mm: yMm });
      setSelected({ kind: "rack", id: rack.id });
      await reload(`货架候选已保存：X ${xMm} / Y ${yMm} mm`);
    } catch (error) { setMessage((error as Error).message); }
  }, [layout, reload]);
  const moveRack = useCallback(async (id: string, xMm: number, yMm: number) => {
    const current = layout?.racks.find((item) => item.id === id); if (!current) return;
    try { await api.updateRack(id, { version: current.version, x_mm: xMm, y_mm: yMm }); await reload(`货架坐标已保存：X ${xMm} / Y ${yMm} mm`); }
    catch (error) { setMessage((error as Error).message); await loadLayout(layout!.id); }
  }, [layout, reload, loadLayout]);
  const saveRack = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selectedRack || !rackDraft) return;
    try { await api.updateRack(selectedRack.id, { version: selectedRack.version, ...rackDraft }); await reload("货架参数已保存为候选"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const rotateRack = async () => {
    if (!selectedRack) return;
    try { await api.updateRack(selectedRack.id, { version: selectedRack.version, rotation_deg: selectedRack.rotation_deg + 90 }); await reload("货架已旋转 90°"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const confirmRack = async () => {
    if (!selectedRack || !window.confirm(`确认采纳并锁定货架 ${selectedRack.rack_code} 吗？`)) return;
    try { await api.confirmRack(selectedRack.id, selectedRack.version); await reload("货架候选已人工确认并锁定"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const removeRack = async () => {
    if (!selectedRack || !window.confirm(`确认删除货架候选 ${selectedRack.rack_code} 吗？`)) return;
    try { await api.deleteRack(selectedRack.id); setSelected(null); await reload("货架候选已删除"); }
    catch (error) { setMessage((error as Error).message); }
  };

  const beginDrawing = () => {
    const featureCode = resolveFeatureCode(
      layout,
      featureDraft.feature_kind,
      featureDraft.subtype,
      featureDraft.feature_code
    );
    setFeatureDraft((current) => ({ ...current, feature_code: featureCode }));
    setDrawPoints([]);
    setDrawMode(featureDraft.feature_kind);
    setSelected(null);
    setMessage(`正在绘制 ${featureCode}：依次点击坐标点，完成后点击“生成候选”`);
  };
  const addDrawPoint = useCallback((xMm: number, yMm: number) => setDrawPoints((current) => [...current, [xMm, yMm]]), []);
  const finishDrawing = async () => {
    if (!layout || !drawMode) return;
    const minimum = drawMode === "aisle" ? 2 : 3;
    if (drawPoints.length < minimum) { setMessage(`当前类型至少需要 ${minimum} 个坐标点`); return; }
    try {
      const latestLayout = await api.getLayout(layout.id);
      const featureCode = resolveFeatureCode(
        latestLayout,
        drawMode,
        featureDraft.subtype,
        featureDraft.feature_code
      );
      const feature = await api.createFeature(layout.id, {
        ...featureDraft, feature_code: featureCode, feature_kind: drawMode, points: drawPoints,
        width_mm: drawMode === "aisle" ? featureDraft.width_mm : null,
        direction: drawMode === "aisle" ? featureDraft.direction : null,
        no_stacking: drawMode === "aisle" ? featureDraft.no_stacking : drawMode === "no_go" ? true : featureDraft.no_stacking
      });
      setFeatureDraft((current) => ({
        ...current,
        feature_code: nextFeatureCode(latestLayout, drawMode, current.subtype, [feature.feature_code])
      }));
      setDrawMode(null);
      setDrawPoints([]);
      setSelected({ kind: "feature", id: feature.id });
      await reload(feature.source === "ai"
        ? `${feature.feature_code} 已保存为 AI 候选，等待人工确认`
        : `${feature.feature_code} 已保存；下一个编号已自动生成`);
    } catch (error) {
      const reason = (error as Error).message;
      if (reason.includes("编号") && layout) {
        const latestLayout = await loadLayout(layout.id);
        const nextCode = nextFeatureCode(latestLayout, drawMode, featureDraft.subtype);
        setFeatureDraft((current) => ({ ...current, feature_code: nextCode }));
        setMessage(`编号已被使用，已自动改为 ${nextCode}；坐标点仍保留，请再次点击“生成候选”`);
      } else {
        setMessage(reason);
      }
    }
  };
  const confirmFeature = async () => {
    if (!selectedFeature || !window.confirm(`确认采纳 ${selectedFeature.feature_code} 吗？`)) return;
    try { await api.confirmFeature(selectedFeature.id, selectedFeature.version); await reload("候选布局已人工确认"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const saveFeatureVertical = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedFeature || !featureVerticalDraft) return;
    try {
      await api.updateFeature(selectedFeature.id, {
        version: selectedFeature.version,
        ...featureVerticalDraft
      });
      await reload("区域垂直层级已保存并重新执行空间规则");
    } catch (error) { setMessage((error as Error).message); }
  };
  const removeFeature = async () => {
    if (!selectedFeature || !window.confirm(`确认删除 ${selectedFeature.feature_code} 吗？`)) return;
    try { await api.deleteFeature(selectedFeature.id); setSelected(null); await reload("布局语义对象已删除"); }
    catch (error) { setMessage((error as Error).message); }
  };

  const structureCounts = useMemo(() => {
    const counts = { wall: 0, column: 0, door: 0 };
    for (const item of layout?.structures || []) {
      if (item.kind === "wall" || item.kind === "exterior_wall") counts.wall += 1;
      else if (item.kind === "column" || item.kind === "door") counts[item.kind] += 1;
    }
    return counts;
  }, [layout]);
  const totalZoneArea = useMemo(() => (layout?.features || []).filter((item) => item.feature_kind === "zone").reduce((sum, item) => sum + item.area_mm2, 0) / 1_000_000, [layout]);
  const subtypeOptions = featureDraft.feature_kind === "zone" ? zoneTypes : featureDraft.feature_kind === "aisle" ? aisleTypes : noGoTypes;
  const chooseFeatureType = (kind: FeatureKind, subtype: string, name: string, color: string, noStacking: boolean) => {
    setFeatureDraft((current) => ({
      ...current,
      feature_kind: kind,
      subtype,
      feature_code: nextFeatureCode(layout, kind, subtype),
      name,
      color,
      no_stacking: noStacking,
      storage_mode: kind === "zone" ? current.storage_mode : "floor",
      elevation_mm: kind === "zone" ? current.elevation_mm : 0
    }));
  };
  const chooseFeatureSubtype = (subtype: string) => {
    const label = subtypeOptions.find(([value]) => value === subtype)?.[1] || "布局";
    const suffix = featureDraft.feature_kind === "aisle" ? "通道" : featureDraft.feature_kind === "no_go" ? "禁放区" : "堆放区";
    setFeatureDraft((current) => ({
      ...current,
      subtype,
      feature_code: nextFeatureCode(layout, current.feature_kind, subtype),
      name: `${label}${suffix}`
    }));
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div><h1>工厂数字孪生布局编辑器 · Phase 2A</h1><p>人工设备位置受保护 · 语义布局候选 · 全部尺寸与坐标单位 mm</p></div>
        <div className="view-toggle" aria-label="视图切换">
          <button className={viewMode === "2d" ? "active" : ""} onClick={() => setViewMode("2d")}>二维 CAD</button>
          <button className={viewMode === "25d" ? "active" : ""} onClick={() => setViewMode("25d")}>2.5D 等距</button>
        </div>
      </header>
      <section className="statusbar"><span className={busy ? "status busy" : "status"}>{message}</span><strong>不生成正式库位 · 不修改库存、订单、生产或流水</strong></section>

      <section className="workspace">
        <aside className="sidebar left-panel">
          <section className="panel-block">
            <h2>底图与布局</h2>
            <form className="compact-form" onSubmit={importDxf}>
              <label>布局名称<input name="name" required defaultValue="一楼设备布局" /></label>
              <div className="dimension-row two"><label>楼层<input name="floor_code" required defaultValue="1F" /></label><label>DXF 文件<input name="file" type="file" accept=".dxf" required /></label></div>
              <button type="submit" disabled={busy}>只读导入并识别</button>
            </form>
            <label className="select-label">当前布局<select value={layout?.id || ""} onChange={(event) => chooseLayout(event.target.value)} disabled={busy}>{layouts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          </section>

          <section className="panel-block">
            <h2>图层开关</h2>
            <div className="layer-grid">
              {(Object.entries({ structures: "DXF结构", equipment: "设备", racks: "货架", zones: "堆放区域", aisles: "通道", noGo: "禁放区", labels: "编号标签" }) as [keyof LayerVisibility, string][]).map(([key, label]) => (
                <label key={key}><input type="checkbox" checked={layers[key]} onChange={(event) => setLayers({ ...layers, [key]: event.target.checked })} />{label}</label>
              ))}
            </div>
          </section>

          <section className="panel-block">
            <h2>设备素材库</h2><p className="hint">拖到地图后仍是可编辑设备；人工确认锁定后不可移动。</p>
            <div className="asset-grid">{assets.map((asset) => <article key={asset.id} className="asset-card" draggable onDragStart={(event) => event.dataTransfer.setData("application/x-twin-asset", asset.id)}><span className="asset-preview" style={{ backgroundColor: asset.color }}>{asset.image_url ? <img src={asset.image_url} alt="" /> : "2.5D"}</span><strong>{asset.name}</strong><small>{asset.default_width_mm} × {asset.default_depth_mm} mm</small></article>)}</div>
          </section>

          <details className="panel-block tool-details" open>
            <summary>参数化货架素材</summary>
            <form className="compact-form" onSubmit={(event) => event.preventDefault()}>
              <div className="dimension-row two"><label>编号<input value={rackTemplate.rack_code} onChange={(e) => setRackTemplate({ ...rackTemplate, rack_code: e.target.value })} /></label><label>名称<input value={rackTemplate.name} onChange={(e) => setRackTemplate({ ...rackTemplate, name: e.target.value })} /></label></div>
              <div className="dimension-row"><label>长 mm<input type="number" value={rackTemplate.width_mm} onChange={(e) => setRackTemplate({ ...rackTemplate, width_mm: +e.target.value })} /></label><label>宽 mm<input type="number" value={rackTemplate.depth_mm} onChange={(e) => setRackTemplate({ ...rackTemplate, depth_mm: +e.target.value })} /></label><label>高 mm<input type="number" value={rackTemplate.height_mm} onChange={(e) => setRackTemplate({ ...rackTemplate, height_mm: +e.target.value })} /></label></div>
              <div className="dimension-row"><label>层数<input type="number" min="1" value={rackTemplate.levels} onChange={(e) => setRackTemplate({ ...rackTemplate, levels: +e.target.value })} /></label><label>格数<input type="number" min="1" value={rackTemplate.bays} onChange={(e) => setRackTemplate({ ...rackTemplate, bays: +e.target.value })} /></label><label>最小通道 mm<input type="number" value={rackTemplate.min_aisle_width_mm} onChange={(e) => setRackTemplate({ ...rackTemplate, min_aisle_width_mm: +e.target.value })} /></label></div>
              <div className="dimension-row two"><label>正面操作方向<select value={rackTemplate.access_side} onChange={(e) => setRackTemplate({ ...rackTemplate, access_side: e.target.value })}><option value="north">北</option><option value="south">南</option><option value="east">东</option><option value="west">西</option><option value="both">双面</option></select></label><label>候选来源<select value={rackTemplate.source} onChange={(e) => setRackTemplate({ ...rackTemplate, source: e.target.value })}><option value="manual">人工</option><option value="ai">AI 候选</option></select></label></div>
              <article className="rack-drag" draggable onDragStart={(event) => event.dataTransfer.setData("application/x-twin-rack", JSON.stringify(rackTemplate))}><b>☷ 拖拽货架到地图</b><small>{rackTemplate.levels} 层 × {rackTemplate.bays} 格 · {rackTemplate.width_mm}×{rackTemplate.depth_mm}×{rackTemplate.height_mm} mm</small></article>
            </form>
          </details>

          <details className="panel-block tool-details" open>
            <summary>区域 / 通道 / 禁放区绘制</summary>
            <div className="compact-form">
              <div className="dimension-row"><button type="button" className={featureDraft.feature_kind === "zone" ? "active-tool" : "secondary"} onClick={() => chooseFeatureType("zone", "raw_material", "原料堆放区", "#60a5fa", false)}>区域</button><button type="button" className={featureDraft.feature_kind === "aisle" ? "active-tool" : "secondary"} onClick={() => chooseFeatureType("aisle", "forklift", "叉车通道", "#22c55e", true)}>通道</button><button type="button" className={featureDraft.feature_kind === "no_go" ? "active-tool" : "secondary"} onClick={() => chooseFeatureType("no_go", "fire_exit", "消防出口禁放区", "#ef4444", true)}>禁放</button></div>
              <div className="dimension-row two"><label>编号<input value={featureDraft.feature_code} onChange={(e) => setFeatureDraft({ ...featureDraft, feature_code: e.target.value })} /></label><label>名称<input value={featureDraft.name} onChange={(e) => setFeatureDraft({ ...featureDraft, name: e.target.value })} /></label></div>
              <p className="hint code-hint">编号重复时自动递增，不会覆盖已绘制对象。</p>
              <label>分类<select value={featureDraft.subtype} onChange={(e) => chooseFeatureSubtype(e.target.value)}>{subtypeOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              {featureDraft.feature_kind === "zone" && <><label>存放层级<select value={featureDraft.storage_mode} onChange={(e) => { const mode = e.target.value as LayoutFeature["storage_mode"]; setFeatureDraft({ ...featureDraft, storage_mode: mode, elevation_mm: mode === "floor" ? 0 : featureDraft.elevation_mm || 2500 }); }}><option value="floor">地面堆放</option><option value="rack">货架上层</option><option value="overhead">架空共享</option></select></label>{featureDraft.storage_mode !== "floor" && <><div className="dimension-row two"><label>离地高度 mm<input type="number" min="1" value={featureDraft.elevation_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, elevation_mm: +e.target.value })} /></label><label>占用高度 mm<input type="number" min="1" value={featureDraft.storage_height_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, storage_height_mm: +e.target.value })} /></label></div><p className="hint code-hint">默认 2500mm 仅为录入起点，请按货架横梁和设备实测高度填写。</p></>}</>}
              {featureDraft.feature_kind === "aisle" && <><div className="dimension-row two"><label>真实宽度 mm<input type="number" value={featureDraft.width_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, width_mm: +e.target.value })} /></label><label>方向<select value={featureDraft.direction} onChange={(e) => setFeatureDraft({ ...featureDraft, direction: e.target.value })}><option value="one_way">单向</option><option value="two_way">双向</option></select></label></div><label className="check-row"><input type="checkbox" checked={featureDraft.no_stacking} onChange={(e) => setFeatureDraft({ ...featureDraft, no_stacking: e.target.checked })} />禁止堆货</label></>}
              <div className="dimension-row two"><label>颜色<input type="color" value={featureDraft.color} onChange={(e) => setFeatureDraft({ ...featureDraft, color: e.target.value })} /></label><label>来源<select value={featureDraft.source} onChange={(e) => setFeatureDraft({ ...featureDraft, source: e.target.value })}><option value="manual">人工绘制</option><option value="ai">AI 生成候选</option></select></label></div>
              {!drawMode ? <button type="button" onClick={beginDrawing}>开始在地图绘制</button> : <><div className="draw-status">已取 {drawPoints.length} 个点（mm）</div><div className="button-row"><button type="button" onClick={finishDrawing}>生成候选</button><button type="button" className="secondary" onClick={() => { setDrawMode(null); setDrawPoints([]); }}>取消</button></div></>}
            </div>
          </details>

          <details className="panel-block asset-upload"><summary>新增 PNG / 2.5D 设备素材</summary><form className="compact-form" onSubmit={createAsset}><label>素材名称<input name="name" required /></label><label>分类<input name="category" defaultValue="生产设备" required /></label><label>显示方式<select name="render_type" defaultValue="box25d"><option value="box25d">轻量 2.5D</option><option value="png">透明 PNG</option></select></label><label>PNG 图片<input name="image" type="file" accept="image/png" /></label><label>颜色<input name="color" type="color" defaultValue="#2563eb" /></label><div className="dimension-row"><label>宽 mm<input name="default_width_mm" type="number" min="1" defaultValue="3000" required /></label><label>深 mm<input name="default_depth_mm" type="number" min="1" defaultValue="1800" required /></label><label>高 mm<input name="default_height_mm" type="number" min="1" defaultValue="2000" required /></label></div><button type="submit" disabled={busy}>加入素材库</button></form></details>
        </aside>

        <section className="canvas-panel">
          <div className="canvas-toolbar"><div className="metric"><b>{structureCounts.column}</b><span>柱</span></div><div className="metric"><b>{layout?.placements.length || 0}</b><span>设备</span></div><div className="metric"><b>{layout?.racks.length || 0}</b><span>货架</span></div><div className="metric"><b>{layout?.features.length || 0}</b><span>语义对象</span></div><div className="metric"><b>{totalZoneArea.toFixed(1)}</b><span>区域 m²</span></div><div className="metric violation-metric"><b>{layout?.violations.length || 0}</b><span>违规</span></div></div>
          {layout ? <EditorCanvas layout={layout} assets={assets} viewMode={viewMode} selected={selected} layers={layers} drawMode={drawMode} drawPoints={drawPoints} onSelect={setSelected} onMoveEquipment={moveEquipment} onMoveRack={moveRack} onDropAsset={dropAsset} onDropRack={dropRack} onDrawPoint={addDrawPoint} /> : <div className="empty-state">请先导入 DXF 或等待演示布局加载。</div>}
          <div className="canvas-footnote">滚轮缩放 · 右键/中键平移 · 2.5D 可旋转 · 锁定对象不可拖动 · 红色表示规则违规</div>
        </section>

        <aside className="sidebar right-panel">
          <section className="panel-block"><h2>结构与图例</h2><div className="readout"><span>源图</span><strong>{layout?.source_name || "—"}</strong></div><div className="readout"><span>坐标单位</span><strong>mm</strong></div><div className="readout"><span>柱编号</span><strong>COL-{layout?.floor_code || "1F"}-001…</strong></div><div className="color-legend"><span><i className="legend-equipment" />设备</span><span><i className="legend-rack" />货架</span><span><i className="legend-zone" />区域</span><span><i className="legend-aisle" />通道</span><span><i className="legend-no-go" />禁放</span><span><i className="legend-error" />违规</span></div><p className="locked-note">DXF 外墙、墙体和柱子无移动接口；人工已布置设备不会被候选布局自动移动或覆盖。</p>{(layout?.warnings || []).map((warning) => <p className="warning" key={warning}>{warning}</p>)}</section>

          <section className="panel-block property-panel"><h2>所选对象</h2>
            {!selected && <p className="hint">点击设备、货架、区域、通道或禁放区查看属性。</p>}
            {selectedPlacement && placementDraft && <form className="compact-form" onSubmit={savePlacement}><div className={`state-chip ${selectedPlacement.is_locked ? "confirmed" : "candidate"}`}>{selectedPlacement.is_locked ? "已确认并锁定" : "未锁定"}</div><label>设备名称<input disabled={selectedPlacement.is_locked} value={placementDraft.name} onChange={(e) => setPlacementDraft({ ...placementDraft, name: e.target.value })} /></label><div className="dimension-row"><label>宽 mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.width_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, width_mm: +e.target.value })} /></label><label>深 mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.depth_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, depth_mm: +e.target.value })} /></label><label>高 mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.height_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, height_mm: +e.target.value })} /></label></div><div className="dimension-row two"><label>X mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.x_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, x_mm: +e.target.value })} /></label><label>Y mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.y_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, y_mm: +e.target.value })} /></label></div><div className="readout"><span>旋转</span><strong>{selectedPlacement.rotation_deg}°</strong></div><div className="button-row"><button type="button" className="secondary" disabled={selectedPlacement.is_locked} onClick={rotatePlacement}>旋转 90°</button><button type="submit" disabled={selectedPlacement.is_locked}>保存属性</button></div><button type="button" className={selectedPlacement.is_locked ? "unlock" : "confirm"} onClick={toggleEquipmentLock}>{selectedPlacement.is_locked ? "解除锁定" : "确认并锁定设备"}</button><button type="button" className="danger" disabled={selectedPlacement.is_locked} onClick={removePlacement}>移除设备</button></form>}
            {selectedRack && rackDraft && <form className="compact-form" onSubmit={saveRack}><div className={`state-chip ${selectedRack.status}`}>{selectedRack.status === "confirmed" ? "已人工确认并锁定" : selectedRack.source === "ai" ? "AI 候选 · 未确认" : "人工候选 · 未确认"}</div><div className="readout"><span>编号</span><strong>{selectedRack.rack_code}</strong></div><label>名称<input disabled={selectedRack.is_locked} value={rackDraft.name} onChange={(e) => setRackDraft({ ...rackDraft, name: e.target.value })} /></label><div className="dimension-row"><label>长 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.width_mm} onChange={(e) => setRackDraft({ ...rackDraft, width_mm: +e.target.value })} /></label><label>宽 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.depth_mm} onChange={(e) => setRackDraft({ ...rackDraft, depth_mm: +e.target.value })} /></label><label>高 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.height_mm} onChange={(e) => setRackDraft({ ...rackDraft, height_mm: +e.target.value })} /></label></div><div className="dimension-row"><label>层<input disabled={selectedRack.is_locked} type="number" value={rackDraft.levels} onChange={(e) => setRackDraft({ ...rackDraft, levels: +e.target.value })} /></label><label>格<input disabled={selectedRack.is_locked} type="number" value={rackDraft.bays} onChange={(e) => setRackDraft({ ...rackDraft, bays: +e.target.value })} /></label><label>通道 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.min_aisle_width_mm} onChange={(e) => setRackDraft({ ...rackDraft, min_aisle_width_mm: +e.target.value })} /></label></div><label>正面方向<select disabled={selectedRack.is_locked} value={rackDraft.access_side} onChange={(e) => setRackDraft({ ...rackDraft, access_side: e.target.value as Rack["access_side"] })}><option value="north">北</option><option value="south">南</option><option value="east">东</option><option value="west">西</option><option value="both">双面</option></select></label><div className="button-row"><button type="button" className="secondary" disabled={selectedRack.is_locked} onClick={rotateRack}>旋转 90°</button><button type="submit" disabled={selectedRack.is_locked}>保存候选</button></div>{selectedRack.status === "candidate" && <button type="button" className="confirm" onClick={confirmRack}>人工确认并锁定</button>}<button type="button" className="danger" disabled={selectedRack.is_locked} onClick={removeRack}>删除货架候选</button></form>}
            {selectedFeature && <div className="compact-form"><div className={`state-chip ${selectedFeature.status}`}>{selectedFeature.status === "confirmed" ? "已人工确认" : selectedFeature.source === "ai" ? "AI 候选 · 未确认" : "人工候选 · 未确认"}</div><div className="readout"><span>编号</span><strong>{selectedFeature.feature_code}</strong></div><div className="readout"><span>名称</span><strong>{selectedFeature.name}</strong></div><div className="readout"><span>分类</span><strong>{selectedFeature.subtype}</strong></div><div className="readout"><span>面积</span><strong>{(selectedFeature.area_mm2 / 1_000_000).toFixed(2)} m²</strong></div>{selectedFeature.width_mm && <div className="readout"><span>真实宽度</span><strong>{selectedFeature.width_mm} mm</strong></div>}{selectedFeature.feature_kind === "zone" && featureVerticalDraft && <form className="compact-form vertical-form" onSubmit={saveFeatureVertical}><label>存放层级<select value={featureVerticalDraft.storage_mode} onChange={(e) => { const mode = e.target.value as LayoutFeature["storage_mode"]; setFeatureVerticalDraft({ ...featureVerticalDraft, storage_mode: mode, elevation_mm: mode === "floor" ? 0 : featureVerticalDraft.elevation_mm || 2500 }); }}><option value="floor">地面堆放</option><option value="rack">货架上层</option><option value="overhead">架空共享</option></select></label>{featureVerticalDraft.storage_mode !== "floor" && <div className="dimension-row two"><label>离地高度 mm<input type="number" min="1" value={featureVerticalDraft.elevation_mm} onChange={(e) => setFeatureVerticalDraft({ ...featureVerticalDraft, elevation_mm: +e.target.value })} /></label><label>占用高度 mm<input type="number" min="1" value={featureVerticalDraft.storage_height_mm} onChange={(e) => setFeatureVerticalDraft({ ...featureVerticalDraft, storage_height_mm: +e.target.value })} /></label></div>}<button type="submit">保存垂直属性</button></form>}{selectedFeature.status === "candidate" && <button type="button" className="confirm" onClick={confirmFeature}>人工确认候选</button>}<button type="button" className="danger" onClick={removeFeature}>删除对象</button></div>}
          </section>

          <section className="panel-block"><h2>规则检查</h2><p className="hint">当前建议宽度：人行 {layout?.rule_defaults.pedestrian || 1200}、叉车 {layout?.rule_defaults.forklift || 3000}、消防 {layout?.rule_defaults.fire || 4000}、装卸 {layout?.rule_defaults.loading || 3500} mm；需现场最终确认。</p>{!layout?.violations.length ? <p className="pass-note">未发现简单规则冲突</p> : <div className="violation-list">{layout.violations.map((item) => <button key={item.id} onClick={() => item.entity_kind === "rack" ? setSelected({ kind: "rack", id: item.entity_id }) : item.entity_kind === "equipment" ? setSelected({ kind: "equipment", id: item.entity_id }) : setSelected({ kind: "feature", id: item.entity_id })}><b>{item.severity === "error" ? "违规" : "提醒"}</b><span>{item.message}</span></button>)}</div>}</section>

          <section className="panel-block"><h2>语义对象清单</h2><div className="object-list">{(layout?.racks || []).map((item) => <button key={item.id} onClick={() => setSelected({ kind: "rack", id: item.id })}><b>{item.rack_code}</b><span>{item.status === "confirmed" ? "已确认" : "候选"}</span></button>)}{(layout?.features || []).map((item) => <button key={item.id} onClick={() => setSelected({ kind: "feature", id: item.id })}><b>{item.feature_code}</b><span>{(item.area_mm2 / 1_000_000).toFixed(1)} m² · {item.status === "confirmed" ? "已确认" : "候选"}</span></button>)}</div></section>
        </aside>
      </section>
    </main>
  );
}
