import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { EditorCanvas } from "./EditorCanvas";
import { RackFrontView } from "./RackFrontView";
import { distanceMm, formatDistanceMm } from "./factoryVisuals.mjs";
import { nextFeatureCode, resolveFeatureCode } from "./featureCodes.mjs";
import { pointsBoundsMm, resizeAndMovePointsMm, resizeSegmentMm, translatePointsMm } from "./layoutGeometry.mjs";
import { createDefaultReferenceOverlay, normalizeReferenceOverlayDraft } from "./referenceOverlay.mjs";
import { PALLET_VISUAL_STATES, palletStatusInfo } from "./palletStatus";
import type { FeatureKind } from "./featureCodes.mjs";
import type {
  AssetTemplate,
  CameraPreset,
  LayerVisibility,
  Layout,
  LayoutFeature,
  LayoutSummary,
  Pallet,
  Placement,
  ProductionProjectionResponse,
  ProductionTaskProjection,
  Rack,
  ReferenceOverlayConfig,
  SelectedEntity,
  ViewMode
} from "./types";

type PlacementDraft = Pick<Placement, "name" | "width_mm" | "depth_mm" | "height_mm" | "x_mm" | "y_mm">;
type RackDraft = Pick<Rack, "name" | "width_mm" | "depth_mm" | "height_mm" | "levels" | "level_heights_mm" | "cargo_rows" | "bays" | "access_side" | "min_aisle_width_mm">;
type PalletDraft = Pick<Pallet, "name" | "width_mm" | "depth_mm" | "height_mm" | "x_mm" | "y_mm" | "color" | "visual_status" | "status_note">;
type FeatureVerticalDraft = Pick<LayoutFeature, "storage_mode" | "elevation_mm" | "storage_height_mm">;
type FeatureSemanticDraft = Pick<LayoutFeature, "name" | "subtype" | "color">;
type FeatureGeometryDraft = { center_x_mm: number; center_y_mm: number; width_mm: number; height_mm: number };
type StructureGeometryDraft = { width_mm: number; storage_height_mm: number; elevation_mm: number; span_mm: number };
type ElevatorPeer = { layout_id: string; layout_name: string; floor_code: string; feature: LayoutFeature };
type Floor1CalibrationKind = "structure" | "equipment";

const initialLayers: LayerVisibility = {
  structures: true,
  equipment: true,
  racks: true,
  pallets: true,
  zones: true,
  aisles: true,
  noGo: true,
  customStructures: true,
  labels: true,
  production: true
};

const defaultReferenceOverlay = createDefaultReferenceOverlay();
const FLOOR3_COORDINATE_FRAME_MARKER = "COORDINATE_FRAME:3F_MEASURED_V1";

const zoneTypes = [
  ["raw_material", "原料"], ["semi_finished", "半成品"], ["finished_wait_delivery", "成品待送"],
  ["delivery_surplus", "送剩零头长期存放"], ["floor_marked_storage", "地线分区（用途待确认）"],
  ["rack_storage", "货架存放"], ["unassigned_storage", "待确认存放"],
  ["finished_storage", "成品存放（可临时混放）"],
  ["mold", "模具"], ["printing_plate", "印刷版"], ["abnormal_isolation", "异常隔离"], ["temporary_turnover", "临时周转"]
];
const zoneSemanticColors: Record<string, string> = {
  raw_material: "#16a34a", semi_finished: "#2563eb", finished_wait_delivery: "#f59e0b",
  delivery_surplus: "#f97316", floor_marked_storage: "#64748b", rack_storage: "#8b5cf6",
  unassigned_storage: "#94a3b8", mold: "#db2777", printing_plate: "#0891b2",
  finished_storage: "#eab308",
  abnormal_isolation: "#dc2626", temporary_turnover: "#a855f7"
};
const aisleTypes = [
  ["shared_main", "主通道（人/液压搬运车共用，1900mm）"],
  ["shared_secondary", "次通道（人/液压搬运车共用，1500mm）"],
  ["pedestrian", "纯人行"], ["forklift", "特种设备叉车"], ["fire", "消防"], ["loading", "装卸"]
];
const noGoTypes = [
  ["fire_exit", "消防出口"], ["electrical_box", "配电箱"], ["maintenance", "设备检修区"],
  ["door_swing", "门扇开启区"], ["freight_elevator", "货梯口"]
];
const structureTypes = [
  ["custom_wall", "简易墙体"], ["rolling_door", "卷帘门"], ["custom_window", "窗"],
  ["custom_column", "柱子"], ["freight_elevator", "货梯（跨楼层定位）"]
];

function nextPalletCode(layout: Layout) {
  const prefix = `PAL-${layout.floor_code.toUpperCase()}-`;
  const maximum = layout.pallets.reduce((current, pallet) => {
    const match = pallet.pallet_code.toUpperCase().match(new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\d+)$`));
    return Math.max(current, match ? Number(match[1]) : 0);
  }, 0);
  return `${prefix}${String(maximum + 1).padStart(3, "0")}`;
}

function formatProjectionTime(value: string | null) {
  if (!value) return "未记录";
  const parsed = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

export default function App() {
  const [layouts, setLayouts] = useState<LayoutSummary[]>([]);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [assets, setAssets] = useState<AssetTemplate[]>([]);
  const [selected, setSelected] = useState<SelectedEntity>(null);
  const [rackFocusId, setRackFocusId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("2d");
  const [cameraPreset, setCameraPreset] = useState<CameraPreset>("fit");
  const [viewResetToken, setViewResetToken] = useState(0);
  const [layers, setLayers] = useState<LayerVisibility>(initialLayers);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("正在读取布局…");
  const [placementDraft, setPlacementDraft] = useState<PlacementDraft | null>(null);
  const [rackDraft, setRackDraft] = useState<RackDraft | null>(null);
  const [palletDraft, setPalletDraft] = useState<PalletDraft | null>(null);
  const [featureVerticalDraft, setFeatureVerticalDraft] = useState<FeatureVerticalDraft | null>(null);
  const [featureSemanticDraft, setFeatureSemanticDraft] = useState<FeatureSemanticDraft | null>(null);
  const [featureGeometryDraft, setFeatureGeometryDraft] = useState<FeatureGeometryDraft | null>(null);
  const [structureGeometryDraft, setStructureGeometryDraft] = useState<StructureGeometryDraft | null>(null);
  const [referenceLayout, setReferenceLayout] = useState<Layout | null>(null);
  const [referenceOverlay, setReferenceOverlay] = useState<ReferenceOverlayConfig>(defaultReferenceOverlay);
  const [elevatorPeers, setElevatorPeers] = useState<ElevatorPeer[]>([]);
  const [floor1CalibrationKind, setFloor1CalibrationKind] = useState<Floor1CalibrationKind>("structure");
  const [floor1CalibrationSearch, setFloor1CalibrationSearch] = useState("");
  const [productionProjection, setProductionProjection] = useState<ProductionProjectionResponse | null>(null);
  const [productionTaskId, setProductionTaskId] = useState<number | null>(null);
  const [productionSearch, setProductionSearch] = useState("");
  const [productionTargetKind, setProductionTargetKind] = useState<"pallet" | "zone">("pallet");
  const [productionTargetId, setProductionTargetId] = useState("");
  const [productionBusy, setProductionBusy] = useState(false);
  const [drawMode, setDrawMode] = useState<FeatureKind | null>(null);
  const [drawPoints, setDrawPoints] = useState<number[][]>([]);
  const [measureMode, setMeasureMode] = useState(false);
  const [measurePoints, setMeasurePoints] = useState<number[][]>([]);
  const [rackTemplate, setRackTemplate] = useState({
    rack_code: "RACK-1F-001", name: "标准模块货架", width_mm: 2800, depth_mm: 1100,
    height_mm: 2200, levels: 3, level_heights_mm: [750, 1500], cargo_rows: 4, bays: 1, access_side: "south", min_aisle_width_mm: 1500,
    rotation_deg: 0, color: "#8b5cf6", source: "manual"
  });
  const [palletTemplate, setPalletTemplate] = useState({
    name: "标准木质栈板", width_mm: 1200, depth_mm: 1000, height_mm: 150,
    rotation_deg: 0, color: "#b7793f", visual_status: "empty" as Pallet["visual_status"],
    status_note: "", is_simulated: false
  });
  const [palletSnapEnabled, setPalletSnapEnabled] = useState(true);
  const [palletSnapThresholdMm, setPalletSnapThresholdMm] = useState(180);
  const [featureDraft, setFeatureDraft] = useState({
    feature_kind: "zone" as FeatureKind, subtype: "raw_material",
    feature_code: "ZONE-1F-RAW-001", name: "原料堆放区", width_mm: 3000,
    direction: "two_way", no_stacking: false, storage_mode: "floor" as "floor" | "rack" | "overhead",
    elevation_mm: 0, storage_height_mm: 1000, span_mm: 4000, color: "#60a5fa", source: "manual"
  });

  const loadLayout = useCallback(async (id: string) => {
    const next = await api.getLayout(id);
    setLayout(next);
    return next;
  }, []);
  const loadProductionProjections = useCallback(async (layoutId: string, announce = false) => {
    const next = await api.getProductionProjections(layoutId);
    setProductionProjection(next);
    if (announce) {
      setMessage(next.available
        ? `ERP只读投影已刷新：${next.items.length} 项待生产，${next.items.filter((item) => item.mapping && !item.mapping.target_missing).length} 项已定位`
        : next.error || "ERP只读源不可用");
    }
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
  useEffect(() => {
    if (!layout || layout.floor_code.toUpperCase() !== "1F") {
      setProductionProjection(null);
      setProductionTaskId(null);
      return;
    }
    let cancelled = false;
    const refresh = () => loadProductionProjections(layout.id).catch((error: Error) => {
      if (!cancelled) setProductionProjection({
        available: false,
        source_label: "ERP生产任务",
        source_read_only: true,
        fetched_at: null,
        error: error.message,
        layout_id: layout.id,
        floor_code: layout.floor_code,
        items: [],
        stale_mappings: []
      });
    });
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [layout?.id, layout?.floor_code, loadProductionProjections]);
  useEffect(() => {
    if (!layout || layout.floor_code.toUpperCase() !== "1F") { setReferenceLayout(null); return; }
    const reference = layouts.find((item) => item.floor_code.toUpperCase() === "3F");
    if (!reference) return;
    const sharedCoordinates = layout.warnings.some((warning) => warning === FLOOR3_COORDINATE_FRAME_MARKER);
    let cancelled = false;
    api.getLayout(reference.id).then((next) => {
      if (cancelled) return;
      setReferenceLayout(next);
      const key = `${sharedCoordinates ? "tm-floor-reference-overlay-shared-v1" : "tm-floor-reference-overlay-v1"}:${layout.id}:${next.id}`;
      try { setReferenceOverlay(normalizeReferenceOverlayDraft(JSON.parse(localStorage.getItem(key) || "null"), sharedCoordinates)); }
      catch { setReferenceOverlay(createDefaultReferenceOverlay(sharedCoordinates)); }
    }).catch((error: Error) => !cancelled && setMessage(`三楼参照层加载失败：${error.message}`));
    return () => { cancelled = true; };
  }, [layout?.id, layout?.floor_code, layout?.warnings, layouts]);

  const sharesFloor3Coordinates = Boolean(layout?.warnings.some((warning) => warning === FLOOR3_COORDINATE_FRAME_MARKER));
  const referenceOverlayStorageKey = layout && referenceLayout ? `${sharesFloor3Coordinates ? "tm-floor-reference-overlay-shared-v1" : "tm-floor-reference-overlay-v1"}:${layout.id}:${referenceLayout.id}` : "";
  const saveReferenceOverlayDraft = () => {
    if (!referenceOverlayStorageKey || !layout || !referenceLayout) return;
    localStorage.setItem(referenceOverlayStorageKey, JSON.stringify({ schemaVersion: 3, draftType: "floor_reference_manual_calibration", transformMode: sharesFloor3Coordinates ? "shared_3f_measured_coordinates" : "fitted_anchor_plus_manual_mm", sourceLayoutId: referenceLayout.id, targetLayoutId: layout.id, geometryChanged: false, savedAt: new Date().toISOString(), config: referenceOverlay }));
    setMessage(sharesFloor3Coordinates ? "三楼实测柱墙复核草稿已保存；一楼候选坐标未再次改变" : "一楼对齐草稿已保存在当前浏览器；没有修改一楼或三楼结构");
  };
  const exportReferenceOverlayDraft = () => {
    if (!layout || !referenceLayout) return;
    const payload = { schemaVersion: 3, draftType: "floor_reference_manual_calibration", transformMode: sharesFloor3Coordinates ? "shared_3f_measured_coordinates" : "fitted_anchor_plus_manual_mm", sourceLayoutId: referenceLayout.id, sourceFloor: "3F-left-half", targetLayoutId: layout.id, targetFloor: "1F", units: "mm", geometryChanged: false, savedAt: new Date().toISOString(), config: referenceOverlay };
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:"application/json;charset=utf-8"})),link=document.createElement("a");link.href=url;link.download=`1F-align-to-3F-reference-${new Date().toISOString().slice(0,10)}.json`;document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);setMessage("一楼对齐草稿 JSON 已导出");
  };
  const updateReferenceNumber = (key: keyof ReferenceOverlayConfig, value: number) => setReferenceOverlay((current) => ({ ...current, [key]: value }));
  const nudgeReference = (dx: number, dy: number) => setReferenceOverlay((current) => ({ ...current, offset_x_mm: current.offset_x_mm + dx, offset_y_mm: current.offset_y_mm + dy }));
  const rotateReference = (degrees: number) => setReferenceOverlay((current) => ({ ...current, rotation_deg: ((current.rotation_deg + degrees + 180) % 360) - 180 }));

  const selectedPlacement = useMemo(
    () => selected?.kind === "equipment" ? layout?.placements.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  const selectedRack = useMemo(
    () => selected?.kind === "rack" ? layout?.racks.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  const focusedRack = useMemo(
    () => rackFocusId ? layout?.racks.find((item) => item.id === rackFocusId) || null : null,
    [layout, rackFocusId]
  );
  const selectFromMap = useCallback((entity: SelectedEntity) => {
    setSelected(entity);
    if (entity?.kind === "rack") setRackFocusId(entity.id);
  }, []);
  const selectCalibrationObject = useCallback((entity: SelectedEntity) => {
    setSelected(entity);
    window.requestAnimationFrame(() => {
      document.getElementById("selected-object-panel")?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  }, []);
  const selectedPallet = useMemo(
    () => selected?.kind === "pallet" ? layout?.pallets.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  const selectedPalletState = selectedPallet ? palletStatusInfo(selectedPallet.visual_status) : null;
  const selectedProductionTask = useMemo(
    () => productionTaskId === null ? null : productionProjection?.items.find((item) => item.source_task_id === productionTaskId) || null,
    [productionProjection, productionTaskId]
  );
  const visibleProductionTasks = useMemo(() => {
    const query = productionSearch.trim().toLowerCase();
    return (productionProjection?.items || []).filter((item) => !query || `${item.order_number} ${item.customer_name} ${item.product_code} ${item.product_name}`.toLowerCase().includes(query));
  }, [productionProjection, productionSearch]);
  const selectedFeature = useMemo(
    () => selected?.kind === "feature" ? layout?.features.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  const selectedStructure = useMemo(
    () => selected?.kind === "structure" ? layout?.structures.find((item) => item.id === selected.id) || null : null,
    [layout, selected]
  );
  const selectedFloor1EditableStructure = Boolean(
    selectedFeature
    && selectedFeature.feature_kind === "structure"
    && ["custom_wall", "rolling_door", "custom_window"].includes(selectedFeature.subtype)
    && !selectedFeature.is_locked
  );
  const selectedFloor1Anchor = Boolean(
    selectedFeature
    && selectedFeature.feature_kind === "structure"
    && ["custom_column", "freight_elevator"].includes(selectedFeature.subtype)
  );
  const floor1EditableStructures = useMemo(() => {
    if (layout?.floor_code.toUpperCase() !== "1F") return [];
    const query = floor1CalibrationSearch.trim().toLowerCase();
    return layout.features
      .filter((item) => item.feature_kind === "structure" && ["custom_wall", "rolling_door", "custom_window"].includes(item.subtype) && !item.is_locked)
      .filter((item) => !query || `${item.feature_code} ${item.name}`.toLowerCase().includes(query))
      .sort((left, right) => left.feature_code.localeCompare(right.feature_code, "zh-CN"));
  }, [layout, floor1CalibrationSearch]);
  const floor1EditableEquipment = useMemo(() => {
    if (layout?.floor_code.toUpperCase() !== "1F") return [];
    const query = floor1CalibrationSearch.trim().toLowerCase();
    return layout.placements
      .filter((item) => !query || `${item.name} ${item.id}`.toLowerCase().includes(query))
      .sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
  }, [layout, floor1CalibrationSearch]);
  useEffect(() => {
    setPlacementDraft(selectedPlacement ? {
      name: selectedPlacement.name, width_mm: selectedPlacement.width_mm, depth_mm: selectedPlacement.depth_mm,
      height_mm: selectedPlacement.height_mm, x_mm: selectedPlacement.x_mm, y_mm: selectedPlacement.y_mm
    } : null);
  }, [selectedPlacement]);
  useEffect(() => {
    setRackDraft(selectedRack ? {
      name: selectedRack.name, width_mm: selectedRack.width_mm, depth_mm: selectedRack.depth_mm,
      height_mm: selectedRack.height_mm, levels: selectedRack.levels, level_heights_mm: selectedRack.level_heights_mm,
      cargo_rows: selectedRack.cargo_rows, bays: selectedRack.bays,
      access_side: selectedRack.access_side, min_aisle_width_mm: selectedRack.min_aisle_width_mm
    } : null);
  }, [selectedRack]);
  useEffect(() => {
    setPalletDraft(selectedPallet ? {
      name: selectedPallet.name, width_mm: selectedPallet.width_mm, depth_mm: selectedPallet.depth_mm,
      height_mm: selectedPallet.height_mm, x_mm: selectedPallet.x_mm, y_mm: selectedPallet.y_mm,
      color: selectedPallet.color, visual_status: selectedPallet.visual_status,
      status_note: selectedPallet.status_note
    } : null);
  }, [selectedPallet]);
  useEffect(() => {
    if (!selectedProductionTask?.mapping || selectedProductionTask.mapping.target_missing) {
      setProductionTargetId("");
      return;
    }
    setProductionTargetKind(selectedProductionTask.mapping.target_kind);
    setProductionTargetId(selectedProductionTask.mapping.target_id);
  }, [selectedProductionTask]);
  useEffect(() => {
    setFeatureVerticalDraft(selectedFeature?.feature_kind === "zone" ? {
      storage_mode: selectedFeature.storage_mode,
      elevation_mm: selectedFeature.elevation_mm,
      storage_height_mm: selectedFeature.storage_height_mm
    } : null);
  }, [selectedFeature]);
  useEffect(() => {
    setFeatureSemanticDraft(selectedFeature?.feature_kind === "zone" ? {
      name: selectedFeature.name,
      subtype: selectedFeature.subtype,
      color: selectedFeature.color
    } : null);
  }, [selectedFeature]);
  useEffect(() => {
    if (!selectedFeature || !["zone", "no_go"].includes(selectedFeature.feature_kind) || selectedFeature.points.length < 3) {
      setFeatureGeometryDraft(null);
      return;
    }
    const bounds = pointsBoundsMm(selectedFeature.points);
    setFeatureGeometryDraft({
      center_x_mm: bounds.centerXmm,
      center_y_mm: bounds.centerYmm,
      width_mm: bounds.widthMm,
      height_mm: bounds.heightMm
    });
  }, [selectedFeature]);
  useEffect(() => {
    if (!selectedFeature || selectedFeature.feature_kind !== "structure" || selectedFeature.subtype === "dxf_hidden") {
      setStructureGeometryDraft(null);
      return;
    }
    const [start, end] = selectedFeature.points;
    setStructureGeometryDraft({
      width_mm: selectedFeature.width_mm || 100,
      storage_height_mm: selectedFeature.storage_height_mm,
      elevation_mm: selectedFeature.elevation_mm,
      span_mm: start && end ? Math.round(Math.hypot(end[0] - start[0], end[1] - start[1])) : 0
    });
  }, [selectedFeature]);
  useEffect(() => {
    let cancelled = false;
    if (!selectedFeature || selectedFeature.subtype !== "freight_elevator" || !layout) {
      setElevatorPeers([]);
      return () => { cancelled = true; };
    }
    Promise.all(
      layouts
        .filter((item) => item.id !== layout.id)
        .map(async (item) => ({ summary: item, detail: await api.getLayout(item.id) }))
    ).then((results) => {
      if (cancelled) return;
      setElevatorPeers(results.flatMap(({ summary, detail }) => {
        const peer = detail.features.find((item) =>
          item.subtype === "freight_elevator" && item.feature_code === selectedFeature.feature_code
        );
        return peer ? [{
          layout_id: summary.id,
          layout_name: summary.name,
          floor_code: summary.floor_code,
          feature: peer
        }] : [];
      }));
    }).catch(() => { if (!cancelled) setElevatorPeers([]); });
    return () => { cancelled = true; };
  }, [selectedFeature, layout, layouts]);
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
    try { const next = await loadLayout(id); setSelected(null); setRackFocusId(null); setMeasureMode(false); setMeasurePoints([]); setMessage(`已切换到 ${next.name}`); }
    catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  };
  const chooseProductionTask = (task: ProductionTaskProjection) => {
    setProductionTaskId(task.source_task_id);
    if (task.mapping && !task.mapping.target_missing) {
      setProductionTargetKind(task.mapping.target_kind);
      setProductionTargetId(task.mapping.target_id);
      setSelected({ kind: task.mapping.target_kind === "pallet" ? "pallet" : "feature", id: task.mapping.target_id });
      return;
    }
    if (layout?.pallets.length) {
      setProductionTargetKind("pallet");
      setProductionTargetId(layout.pallets[0].id);
    } else {
      const firstZone = layout?.features.find((item) => item.feature_kind === "zone");
      setProductionTargetKind("zone");
      setProductionTargetId(firstZone?.id || "");
    }
  };
  const saveProductionProjection = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!layout || !selectedProductionTask || !productionTargetId) return;
    setProductionBusy(true);
    try {
      await api.bindProductionProjection(layout.id, selectedProductionTask.source_task_id, {
        target_kind: productionTargetKind,
        target_id: productionTargetId,
        ...(selectedProductionTask.mapping ? { version: selectedProductionTask.mapping.version } : {})
      });
      const next = await loadProductionProjections(layout.id);
      const updated = next.items.find((item) => item.source_task_id === selectedProductionTask.source_task_id);
      if (updated?.mapping) setSelected({ kind: updated.mapping.target_kind === "pallet" ? "pallet" : "feature", id: updated.mapping.target_id });
      setMessage(`只读任务 ${selectedProductionTask.order_number} 已人工定位；ERP订单、数量和状态未修改`);
    } catch (error) { setMessage((error as Error).message); }
    finally { setProductionBusy(false); }
  };
  const removeProductionProjection = async () => {
    if (!layout || !selectedProductionTask?.mapping) return;
    setProductionBusy(true);
    try {
      await api.deleteProductionProjection(layout.id, selectedProductionTask.source_task_id, selectedProductionTask.mapping.version);
      await loadProductionProjections(layout.id);
      setMessage(`已移除 ${selectedProductionTask.order_number} 的地图定位；ERP任务保持不变`);
    } catch (error) { setMessage((error as Error).message); }
    finally { setProductionBusy(false); }
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
    if (selectedPlacement.is_locked && !window.confirm("确认解除设备锁定并进入现场校正吗？解除后设备可移动，原确认状态会撤销，调整完成后需要重新确认并锁定。")) return;
    try {
      await api.updatePlacement(selectedPlacement.id, {
        version: selectedPlacement.version,
        is_locked: !selectedPlacement.is_locked,
        is_confirmed: selectedPlacement.is_locked ? false : true
      });
      await reload(selectedPlacement.is_locked ? "设备已解除锁定并转为待重新确认，可按柱网拖动或输入坐标" : "设备已重新确认并锁定");
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
  const moveFeature = useCallback(async (id: string, deltaXmm: number, deltaYmm: number) => {
    const current = layout?.features.find((item) => item.id === id);
    if (!current || current.subtype === "dxf_hidden" || current.is_locked) return;
    try {
      await api.updateFeature(id, {
        version: current.version,
        points: translatePointsMm(current.points, deltaXmm, deltaYmm)
      });
      await reload(`${current.feature_code} 已平移 ΔX ${deltaXmm} / ΔY ${deltaYmm} mm，并重新设为待确认`);
    } catch (error) {
      setMessage((error as Error).message);
      if (layout) await loadLayout(layout.id);
    }
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

  const dropPallet = useCallback(async (payload: Record<string, unknown>, xMm: number, yMm: number) => {
    if (!layout) return;
    try {
      const pallet = await api.createPallet(layout.id, {
        ...payload,
        pallet_code: nextPalletCode(layout),
        x_mm: xMm,
        y_mm: yMm,
        snap_enabled: palletSnapEnabled,
        snap_threshold_mm: palletSnapThresholdMm
      });
      setSelected({ kind: "pallet", id: pallet.id });
      await reload(`${pallet.pallet_code} 已放入 ${pallet.zone_code}${pallet.snapped ? "，已自动吸附" : "，保持自由位置"}`);
    } catch (error) { setMessage((error as Error).message); }
  }, [layout, palletSnapEnabled, palletSnapThresholdMm, reload]);
  const movePallet = useCallback(async (id: string, xMm: number, yMm: number) => {
    const current = layout?.pallets.find((item) => item.id === id); if (!current) return;
    try {
      const pallet = await api.updatePallet(id, {
        version: current.version,
        x_mm: xMm,
        y_mm: yMm,
        snap_enabled: palletSnapEnabled,
        snap_threshold_mm: palletSnapThresholdMm
      });
      await reload(`${pallet.pallet_code} 坐标已保存${pallet.snapped ? "并吸附对齐" : "（自由位置）"}`);
    } catch (error) { setMessage((error as Error).message); if (layout) await loadLayout(layout.id); }
  }, [layout, palletSnapEnabled, palletSnapThresholdMm, reload, loadLayout]);
  const savePallet = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selectedPallet || !palletDraft) return;
    try {
      await api.updatePallet(selectedPallet.id, {
        version: selectedPallet.version,
        ...palletDraft,
        snap_enabled: false,
        snap_threshold_mm: palletSnapThresholdMm
      });
      await reload("栈板状态、尺寸和坐标已保存");
    } catch (error) { setMessage((error as Error).message); }
  };
  const rotatePallet = async () => {
    if (!selectedPallet) return;
    try {
      const pallet = await api.updatePallet(selectedPallet.id, {
        version: selectedPallet.version,
        rotation_deg: selectedPallet.rotation_deg + 90,
        snap_enabled: palletSnapEnabled,
        snap_threshold_mm: palletSnapThresholdMm
      });
      await reload(`${pallet.pallet_code} 已旋转 90°`);
    } catch (error) { setMessage((error as Error).message); }
  };
  const duplicatePallet = async () => {
    if (!layout || !selectedPallet) return;
    const offset = (selectedPallet.rotation_deg % 180 === 90 ? selectedPallet.depth_mm : selectedPallet.width_mm) + 80;
    try {
      const pallet = await api.createPallet(layout.id, {
        pallet_code: nextPalletCode(layout), name: selectedPallet.name,
        x_mm: selectedPallet.x_mm + offset, y_mm: selectedPallet.y_mm,
        width_mm: selectedPallet.width_mm, depth_mm: selectedPallet.depth_mm,
        height_mm: selectedPallet.height_mm, rotation_deg: selectedPallet.rotation_deg,
        color: selectedPallet.color, visual_status: selectedPallet.visual_status,
        status_note: selectedPallet.status_note, is_simulated: selectedPallet.is_simulated,
        snap_enabled: palletSnapEnabled,
        snap_threshold_mm: palletSnapThresholdMm
      });
      setSelected({ kind: "pallet", id: pallet.id });
      await reload(`${pallet.pallet_code} 已复制并${pallet.snapped ? "吸附排列" : "自由放置"}`);
    } catch (error) { setMessage((error as Error).message); }
  };
  const removePallet = async () => {
    if (!selectedPallet || !window.confirm(`确认删除栈板 ${selectedPallet.pallet_code} 吗？`)) return;
    try { await api.deletePallet(selectedPallet.id); setSelected(null); await reload("栈板已删除"); }
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
    setMeasureMode(false);
    setMeasurePoints([]);
    setDrawMode(featureDraft.feature_kind);
    setSelected(null);
    setMessage(`正在绘制 ${featureCode}：依次点击坐标点，完成后点击“生成候选”`);
  };
  const addDrawPoint = useCallback((xMm: number, yMm: number) => setDrawPoints((current) => [...current, [xMm, yMm]]), []);
  const addMeasurePoint = useCallback((xMm: number, yMm: number) => {
    setMeasurePoints((current) => current.length >= 2 ? [[xMm, yMm]] : [...current, [xMm, yMm]]);
  }, []);
  const finishDrawing = async () => {
    if (!layout || !drawMode) return;
    const minimum = drawMode === "aisle" || drawMode === "structure" ? 2 : 3;
    if (drawPoints.length < minimum) { setMessage(`当前类型至少需要 ${minimum} 个坐标点`); return; }
    try {
      const latestLayout = await api.getLayout(layout.id);
      const featureCode = resolveFeatureCode(
        latestLayout,
        drawMode,
        featureDraft.subtype,
        featureDraft.feature_code
      );
      let points = drawMode === "structure" && featureDraft.subtype !== "custom_wall"
        ? drawPoints.slice(0, 2)
        : drawPoints;
      if (drawMode === "structure" && featureDraft.subtype !== "custom_wall" && points.length === 2) {
        const [start, directionPoint] = points;
        const dx = directionPoint[0] - start[0];
        const dy = directionPoint[1] - start[1];
        const sourceLength = Math.hypot(dx, dy) || 1;
        const span = Math.max(1, featureDraft.span_mm);
        if (["custom_column", "freight_elevator"].includes(featureDraft.subtype)) {
          const halfX = dx / sourceLength * span / 2;
          const halfY = dy / sourceLength * span / 2;
          points = [
            [Math.round(start[0] - halfX), Math.round(start[1] - halfY)],
            [Math.round(start[0] + halfX), Math.round(start[1] + halfY)]
          ];
        } else {
          points = [start, [Math.round(start[0] + dx / sourceLength * span), Math.round(start[1] + dy / sourceLength * span)]];
        }
      }
      const { span_mm: _spanMm, ...featurePayload } = featureDraft;
      void _spanMm;
      const feature = await api.createFeature(layout.id, {
        ...featurePayload, feature_code: featureCode, feature_kind: drawMode,
        points,
        width_mm: drawMode === "aisle" || drawMode === "structure" ? featureDraft.width_mm : null,
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
  const saveFeatureSemantics = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedFeature || !featureSemanticDraft) return;
    try {
      await api.updateFeature(selectedFeature.id, {
        version: selectedFeature.version,
        ...featureSemanticDraft
      });
      await reload(`${selectedFeature.feature_code} 的区域用途已保存，并重新设为待确认`);
    } catch (error) { setMessage((error as Error).message); }
  };
  const saveFeatureGeometry = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedFeature || !featureGeometryDraft) return;
    if (featureGeometryDraft.width_mm <= 0 || featureGeometryDraft.height_mm <= 0) {
      setMessage("区域长宽必须大于 0mm");
      return;
    }
    try {
      await api.updateFeature(selectedFeature.id, {
        version: selectedFeature.version,
        points: resizeAndMovePointsMm(
          selectedFeature.points,
          featureGeometryDraft.center_x_mm,
          featureGeometryDraft.center_y_mm,
          featureGeometryDraft.width_mm,
          featureGeometryDraft.height_mm
        )
      });
      await reload(`${selectedFeature.feature_code} 的中心坐标和真实长宽已保存，并重新设为待确认`);
    } catch (error) { setMessage((error as Error).message); }
  };
  const nudgeFeatureGeometry = (deltaXmm: number, deltaYmm: number) => {
    setFeatureGeometryDraft((current) => current ? {
      ...current,
      center_x_mm: current.center_x_mm + deltaXmm,
      center_y_mm: current.center_y_mm + deltaYmm
    } : current);
  };
  const saveStructureGeometry = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedFeature || !structureGeometryDraft) return;
    const canResizeSpan = selectedFeature.subtype !== "custom_wall" && selectedFeature.points.length >= 2;
    try {
      const sharedDimensions = {
        width_mm: structureGeometryDraft.width_mm,
        storage_height_mm: structureGeometryDraft.storage_height_mm,
        elevation_mm: selectedFeature.subtype === "custom_window" ? structureGeometryDraft.elevation_mm : 0
      };
      await api.updateFeature(selectedFeature.id, {
        version: selectedFeature.version,
        ...sharedDimensions,
        ...(canResizeSpan ? { points: resizeSegmentMm(selectedFeature.points, structureGeometryDraft.span_mm) } : {})
      });
      if (selectedFeature.subtype === "freight_elevator") {
        for (const peer of elevatorPeers) {
          await api.updateFeature(peer.feature.id, {
            version: peer.feature.version,
            ...sharedDimensions,
            points: resizeSegmentMm(peer.feature.points, structureGeometryDraft.span_mm)
          });
        }
      }
      await reload(selectedFeature.subtype === "freight_elevator"
        ? `${selectedFeature.feature_code} 长宽高已同步到 ${elevatorPeers.length + 1} 个楼层投影，并全部重新设为待确认`
        : `${selectedFeature.feature_code} 的真实尺寸已保存，并重新设为待确认`);
    } catch (error) { setMessage((error as Error).message); }
  };
  const removeFeature = async () => {
    if (!selectedFeature || !window.confirm(`确认删除 ${selectedFeature.feature_code} 吗？`)) return;
    try { await api.deleteFeature(selectedFeature.id); setSelected(null); await reload("布局语义对象已删除"); }
    catch (error) { setMessage((error as Error).message); }
  };
  const hideSelectedStructure = async () => {
    if (!layout || !selectedStructure) return;
    const hiddenCode = `HIDE-${layout.floor_code}-${selectedStructure.source_handle}`.toUpperCase().slice(0, 80);
    if (layout.features.some((item) => item.feature_kind === "structure" && item.subtype === "dxf_hidden" && item.feature_code === hiddenCode)) {
      setMessage("该 DXF 结构已经隐藏；删除对应 HIDE 候选即可恢复显示");
      return;
    }
    if (!window.confirm(`确认隐藏 DXF 识别对象 ${selectedStructure.source_handle} 吗？原始 DXF 不会被修改。`)) return;
    const geometry = selectedStructure.geometry;
    let points = geometry.points?.slice(0, 2) || [];
    if (points.length < 2) {
      const x = geometry.x_mm || 0;
      const y = geometry.y_mm || 0;
      points = [[x, y], [x + 1, y]];
    }
    try {
      await api.createFeature(layout.id, {
        feature_code: hiddenCode,
        name: `隐藏 DXF 结构 ${selectedStructure.source_handle}`,
        feature_kind: "structure",
        subtype: "dxf_hidden",
        points,
        width_mm: 1,
        direction: null,
        no_stacking: false,
        storage_mode: "floor",
        elevation_mm: 0,
        storage_height_mm: 1,
        color: "#64748b",
        source: "manual"
      });
      setSelected(null);
      await reload("已生成 DXF 误识别隐藏候选；原始 DXF 保持只读");
    } catch (error) { setMessage((error as Error).message); }
  };

  const structureCounts = useMemo(() => {
    const counts = { wall: 0, column: 0, door: 0 };
    for (const item of layout?.structures || []) {
      if (item.kind === "wall" || item.kind === "exterior_wall") counts.wall += 1;
      else if (item.kind === "column" || item.kind === "door") counts[item.kind] += 1;
    }
    counts.column += (layout?.features || []).filter((item) => item.subtype === "custom_column").length;
    return counts;
  }, [layout]);
  const totalZoneArea = useMemo(() => (layout?.features || []).filter((item) => item.feature_kind === "zone").reduce((sum, item) => sum + item.area_mm2, 0) / 1_000_000, [layout]);
  const measuredDistance = measurePoints.length === 2
    ? formatDistanceMm(distanceMm(measurePoints[0], measurePoints[1]))
    : measureMode ? "请选择两个点" : "";
  const switchView = (mode: ViewMode) => {
    setViewMode(mode);
    setCameraPreset(mode === "2d" ? "fit" : "north_east");
    setViewResetToken((current) => current + 1);
  };
  const resetView = () => setViewResetToken((current) => current + 1);
  const toggleMeasure = () => {
    setMeasureMode((current) => {
      const next = !current;
      if (next) {
        setDrawMode(null);
        setDrawPoints([]);
        setSelected(null);
        setMeasurePoints([]);
        setMessage("测距模式：在平面图中依次点击两个点；只读，不保存布局数据");
      }
      return next;
    });
  };
  const exportPng = () => {
    const canvas = document.querySelector<HTMLCanvasElement>(".editor-canvas canvas");
    if (!canvas) { setMessage("画布尚未准备完成"); return; }
    canvas.toBlob((blob) => {
      if (!blob) { setMessage("PNG 生成失败，请刷新画布后重试"); return; }
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.download = `${layout?.name || "factory-layout"}-${viewMode}-${new Date().toISOString().slice(0, 10)}.png`;
      link.href = url;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMessage("当前视图已导出为高清 PNG；导出不会修改布局数据");
    }, "image/png");
  };
  const printA3 = () => {
    setMessage("已打开 A3 横向打印，可在系统打印窗口选择“另存为 PDF”");
    window.print();
  };
  const subtypeOptions = featureDraft.feature_kind === "zone" ? zoneTypes : featureDraft.feature_kind === "aisle" ? aisleTypes : featureDraft.feature_kind === "structure" ? structureTypes : noGoTypes;
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
      elevation_mm: kind === "zone" ? current.elevation_mm : kind === "structure" && subtype === "custom_window" ? 900 : 0,
      storage_height_mm: kind === "structure" ? (subtype === "rolling_door" || subtype === "freight_elevator" ? 3500 : subtype === "custom_window" ? 1400 : 3000) : current.storage_height_mm,
      width_mm: kind === "structure" ? (subtype === "custom_wall" ? 120 : subtype === "custom_column" ? 500 : subtype === "freight_elevator" ? 2500 : 100) : current.width_mm,
      span_mm: kind === "structure" ? (subtype === "custom_window" ? 1800 : subtype === "custom_column" ? 500 : subtype === "freight_elevator" ? 3000 : 4000) : current.span_mm
    }));
  };
  const chooseFeatureSubtype = (subtype: string) => {
    const label = subtypeOptions.find(([value]) => value === subtype)?.[1] || "布局";
    const suffix = featureDraft.feature_kind === "aisle" ? "通道" : featureDraft.feature_kind === "no_go" ? "禁放区" : featureDraft.feature_kind === "structure" ? "" : "堆放区";
    setFeatureDraft((current) => ({
      ...current,
      subtype,
      feature_code: nextFeatureCode(layout, current.feature_kind, subtype),
      name: `${label}${suffix}`,
      width_mm: current.feature_kind === "structure" ? (subtype === "custom_wall" ? 120 : subtype === "custom_column" ? 500 : subtype === "freight_elevator" ? 2500 : 100) : current.width_mm,
      storage_height_mm: current.feature_kind === "structure" ? (subtype === "rolling_door" || subtype === "freight_elevator" ? 3500 : subtype === "custom_window" ? 1400 : 3000) : current.storage_height_mm,
      elevation_mm: current.feature_kind === "structure" && subtype === "custom_window" ? 900 : current.feature_kind === "structure" ? 0 : current.elevation_mm,
      span_mm: current.feature_kind === "structure" ? (subtype === "custom_window" ? 1800 : subtype === "custom_column" ? 500 : subtype === "freight_elevator" ? 3000 : 4000) : current.span_mm
    }));
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div><h1>工厂数字孪生布局编辑器 · Phase 2C-1</h1><p>工业平面 · 参数化 2.5D · ERP生产任务只读投影 · 坐标单位 mm</p></div>
        <div className="view-toggle" aria-label="视图切换">
          <button className={viewMode === "2d" ? "active" : ""} onClick={() => switchView("2d")}>二维 CAD · 工业平面</button>
          <button className={viewMode === "25d" ? "active" : ""} onClick={() => switchView("25d")}>等距视图</button>
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

          {layout?.floor_code.toUpperCase() === "1F" && referenceLayout && <details className="panel-block tool-details reference-overlay-panel" open>
            <summary>{sharesFloor3Coordinates ? "三楼左半区柱墙复核" : "三楼左半区柱墙参照"}</summary>
            <p className="hint">{sharesFloor3Coordinates ? "一楼候选坐标已按三楼实测柱网换算。青色虚线现在与三楼共用毫米坐标，仅用于复核。" : "青色虚线只用于人工对齐。一楼、三楼 DXF 与柱墙坐标不会被自动修改。"}</p>
            <div className="compact-form">
              <label className="check-row"><input type="checkbox" checked={referenceOverlay.enabled} onChange={(event) => setReferenceOverlay({ ...referenceOverlay, enabled: event.target.checked })} />显示参照层</label>
              <div className="reference-offset-note">{sharesFloor3Coordinates ? "当前为三楼同坐标复核；如需临时检查误差，输入多少毫米，参照虚线就移动多少毫米。" : "偏移值是相对初始拟合位置的真实毫米增量；输入 5000 就移动 5000mm。"}</div>
              <div className="dimension-row two"><label>人工 X 偏移 mm<input type="number" step="100" value={referenceOverlay.offset_x_mm} onChange={(event) => updateReferenceNumber("offset_x_mm", +event.target.value)} /></label><label>人工 Y 偏移 mm<input type="number" step="100" value={referenceOverlay.offset_y_mm} onChange={(event) => updateReferenceNumber("offset_y_mm", +event.target.value)} /></label></div>
              <div className="nudge-grid"><span /><button type="button" onClick={() => nudgeReference(0, 1000)}>↑ 1000</button><span /><button type="button" onClick={() => nudgeReference(-1000, 0)}>← 1000</button><button type="button" onClick={() => nudgeReference(0, -1000)}>↓ 1000</button><button type="button" onClick={() => nudgeReference(1000, 0)}>1000 →</button></div>
              <div className="reference-fine-nudge"><button type="button" onClick={() => nudgeReference(-100, 0)}>X−100</button><button type="button" onClick={() => nudgeReference(100, 0)}>X+100</button><button type="button" onClick={() => nudgeReference(0, -100)}>Y−100</button><button type="button" onClick={() => nudgeReference(0, 100)}>Y+100</button></div>
              <div className="dimension-row two"><label>X 缩放<input type="number" min="0.1" max="3" step="0.001" value={referenceOverlay.scale_x} onChange={(event) => updateReferenceNumber("scale_x", +event.target.value)} /></label><label>Y 缩放<input type="number" min="0.1" max="3" step="0.001" value={referenceOverlay.scale_y} onChange={(event) => updateReferenceNumber("scale_y", +event.target.value)} /></label></div>
              <div className="dimension-row two"><label className="check-row"><input type="checkbox" checked={referenceOverlay.mirror_x} onChange={(event) => setReferenceOverlay({ ...referenceOverlay, mirror_x: event.target.checked })} />左右镜像</label><label className="check-row"><input type="checkbox" checked={referenceOverlay.mirror_y} onChange={(event) => setReferenceOverlay({ ...referenceOverlay, mirror_y: event.target.checked })} />上下镜像</label></div>
              <label>旋转角度 °<input type="number" step="1" value={referenceOverlay.rotation_deg} onChange={(event) => updateReferenceNumber("rotation_deg", +event.target.value)} /></label>
              <div className="reference-rotation-row"><button type="button" onClick={() => rotateReference(-90)}>左转 90°</button><button type="button" onClick={() => rotateReference(-1)}>左转 1°</button><button type="button" onClick={() => rotateReference(1)}>右转 1°</button><button type="button" onClick={() => rotateReference(90)}>右转 90°</button></div>
              <label>透明度 {Math.round(referenceOverlay.opacity * 100)}%<input type="range" min="0.1" max="0.9" step="0.05" value={referenceOverlay.opacity} onChange={(event) => updateReferenceNumber("opacity", +event.target.value)} /></label>
              <div className="button-row"><button type="button" onClick={saveReferenceOverlayDraft}>保存浏览器草稿</button><button type="button" className="secondary" onClick={exportReferenceOverlayDraft}>导出 JSON</button></div>
              <button type="button" className="danger" onClick={() => { if (confirm(sharesFloor3Coordinates ? "确认恢复三楼同坐标参照的默认值吗？" : "确认恢复三楼参照层的初始拟合值吗？")) setReferenceOverlay(createDefaultReferenceOverlay(sharesFloor3Coordinates)); }}>{sharesFloor3Coordinates ? "恢复同坐标复核" : "恢复初始拟合"}</button>
            </div>
          </details>}

          <section className="panel-block">
            <h2>图层开关</h2>
            <div className="layer-grid">
              {(Object.entries({ structures: "DXF结构", customStructures: "人工墙门窗柱/货梯", equipment: "设备", racks: "货架", pallets: "栈板", zones: "堆放区域", aisles: "通道", noGo: "禁放区", production: "生产任务投影", labels: "编号标签" }) as [keyof LayerVisibility, string][]).map(([key, label]) => (
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
              <div className="dimension-row"><label>层数<input type="number" min="1" value={rackTemplate.levels} onChange={(e) => setRackTemplate({ ...rackTemplate, levels: +e.target.value })} /></label><label>结构格数<input type="number" min="1" value={rackTemplate.bays} onChange={(e) => setRackTemplate({ ...rackTemplate, bays: +e.target.value })} /></label><label>货物排数<input type="number" min="3" max="5" value={rackTemplate.cargo_rows} onChange={(e) => setRackTemplate({ ...rackTemplate, cargo_rows: +e.target.value })} /></label></div>
              <div className="dimension-row two"><label>层板离地高度 mm（逗号分隔）<input value={rackTemplate.level_heights_mm.join(",")} onChange={(e) => setRackTemplate({ ...rackTemplate, level_heights_mm: e.target.value.split(",").map((value) => Number(value.trim())).filter(Number.isFinite) })} /></label><label>最小通道 mm<input type="number" value={rackTemplate.min_aisle_width_mm} onChange={(e) => setRackTemplate({ ...rackTemplate, min_aisle_width_mm: +e.target.value })} /></label></div>
              <div className="dimension-row two"><label>正面操作方向<select value={rackTemplate.access_side} onChange={(e) => setRackTemplate({ ...rackTemplate, access_side: e.target.value })}><option value="north">北</option><option value="south">南</option><option value="east">东</option><option value="west">西</option><option value="both">双面</option></select></label><label>候选来源<select value={rackTemplate.source} onChange={(e) => setRackTemplate({ ...rackTemplate, source: e.target.value })}><option value="manual">人工</option><option value="ai">AI 候选</option></select></label></div>
              <article className="rack-drag" draggable onDragStart={(event) => event.dataTransfer.setData("application/x-twin-rack", JSON.stringify(rackTemplate))}><b>☷ 拖拽货架到地图</b><small>{rackTemplate.levels} 层 × {rackTemplate.bays} 格 · {rackTemplate.width_mm}×{rackTemplate.depth_mm}×{rackTemplate.height_mm} mm</small></article>
            </form>
          </details>

          <details className="panel-block tool-details pallet-tool" open>
            <summary>标准栈板 · 自由组合</summary>
            <div className="pallet-hero"><span className="pallet-mini"><i /><i /><i /><i /><i /></span><div><b>工业标准木质栈板</b><small>近距离吸附 · 远距离自由</small></div></div>
            <form className="compact-form" onSubmit={(event) => event.preventDefault()}>
              <label>名称<input value={palletTemplate.name} onChange={(e) => setPalletTemplate({ ...palletTemplate, name: e.target.value })} /></label>
              <div className="dimension-row"><label>长 mm<input type="number" min="1" value={palletTemplate.width_mm} onChange={(e) => setPalletTemplate({ ...palletTemplate, width_mm: +e.target.value })} /></label><label>宽 mm<input type="number" min="1" value={palletTemplate.depth_mm} onChange={(e) => setPalletTemplate({ ...palletTemplate, depth_mm: +e.target.value })} /></label><label>高 mm<input type="number" min="1" value={palletTemplate.height_mm} onChange={(e) => setPalletTemplate({ ...palletTemplate, height_mm: +e.target.value })} /></label></div>
              <label>初始周转状态<select value={palletTemplate.visual_status} onChange={(e) => setPalletTemplate({ ...palletTemplate, visual_status: e.target.value as Pallet["visual_status"] })}>{PALLET_VISUAL_STATES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
              <label className="snap-switch"><input type="checkbox" checked={palletSnapEnabled} onChange={(e) => setPalletSnapEnabled(e.target.checked)} /><span><b>智能吸附</b><small>关闭后完全自由拖动</small></span></label>
              {palletSnapEnabled && <label>吸附距离 mm<input type="number" min="0" max="1000" value={palletSnapThresholdMm} onChange={(e) => setPalletSnapThresholdMm(+e.target.value)} /></label>}
              <article className="pallet-drag" draggable onDragStart={(event) => event.dataTransfer.setData("application/x-twin-pallet", JSON.stringify(palletTemplate))}><span>▦</span><div><b>拖拽栈板到可存放区域</b><small>{palletTemplate.width_mm} × {palletTemplate.depth_mm} × {palletTemplate.height_mm} mm</small></div></article>
              <div className="pallet-status-legend" aria-label="生产周转状态图例">{PALLET_VISUAL_STATES.map((item) => <span key={item.value}><i style={{ background: item.color }} />{item.label}</span>)}</div>
              <p className="simulation-note">状态只用于一楼现场可视化，不生成正式库位，也不代表库存数量。</p>
            </form>
          </details>

          <details className="panel-block tool-details" open>
            <summary>区域 / 通道 / 墙门窗柱绘制</summary>
            <div className="compact-form">
              <div className="dimension-row"><button type="button" className={featureDraft.feature_kind === "zone" ? "active-tool" : "secondary"} onClick={() => chooseFeatureType("zone", "raw_material", "原料堆放区", "#60a5fa", false)}>区域</button><button type="button" className={featureDraft.feature_kind === "aisle" ? "active-tool" : "secondary"} onClick={() => chooseFeatureType("aisle", "forklift", "叉车通道", "#22c55e", true)}>通道</button><button type="button" className={featureDraft.feature_kind === "no_go" ? "active-tool" : "secondary"} onClick={() => chooseFeatureType("no_go", "fire_exit", "消防出口禁放区", "#ef4444", true)}>禁放</button><button type="button" className={featureDraft.feature_kind === "structure" ? "active-tool" : "secondary"} onClick={() => chooseFeatureType("structure", "custom_wall", "简易墙体", "#475569", false)}>墙/门/窗/柱</button></div>
              <div className="dimension-row two"><label>编号<input value={featureDraft.feature_code} onChange={(e) => setFeatureDraft({ ...featureDraft, feature_code: e.target.value })} /></label><label>名称<input value={featureDraft.name} onChange={(e) => setFeatureDraft({ ...featureDraft, name: e.target.value })} /></label></div>
              <p className="hint code-hint">编号重复时自动递增，不会覆盖已绘制对象。</p>
              <label>分类<select value={featureDraft.subtype} onChange={(e) => chooseFeatureSubtype(e.target.value)}>{subtypeOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              {featureDraft.feature_kind === "zone" && <><label>存放层级<select value={featureDraft.storage_mode} onChange={(e) => { const mode = e.target.value as LayoutFeature["storage_mode"]; setFeatureDraft({ ...featureDraft, storage_mode: mode, elevation_mm: mode === "floor" ? 0 : featureDraft.elevation_mm || 2500 }); }}><option value="floor">地面堆放</option><option value="rack">货架上层</option><option value="overhead">架空共享</option></select></label>{featureDraft.storage_mode !== "floor" && <><div className="dimension-row two"><label>离地高度 mm<input type="number" min="1" value={featureDraft.elevation_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, elevation_mm: +e.target.value })} /></label><label>占用高度 mm<input type="number" min="1" value={featureDraft.storage_height_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, storage_height_mm: +e.target.value })} /></label></div><p className="hint code-hint">默认 2500mm 仅为录入起点，请按货架横梁和设备实测高度填写。</p></>}</>}
              {featureDraft.feature_kind === "aisle" && <><div className="dimension-row two"><label>真实宽度 mm<input type="number" value={featureDraft.width_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, width_mm: +e.target.value })} /></label><label>方向<select value={featureDraft.direction} onChange={(e) => setFeatureDraft({ ...featureDraft, direction: e.target.value })}><option value="one_way">单向</option><option value="two_way">双向</option></select></label></div><label className="check-row"><input type="checkbox" checked={featureDraft.no_stacking} onChange={(e) => setFeatureDraft({ ...featureDraft, no_stacking: e.target.checked })} />禁止堆货</label></>}
              {featureDraft.feature_kind === "structure" && <><div className="dimension-row two"><label>{featureDraft.subtype === "custom_column" ? "柱深" : featureDraft.subtype === "freight_elevator" ? "货梯宽" : "厚度"} mm<input type="number" min="1" value={featureDraft.width_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, width_mm: +e.target.value })} /></label><label>高度 mm<input type="number" min="1" value={featureDraft.storage_height_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, storage_height_mm: +e.target.value })} /></label></div>{featureDraft.subtype !== "custom_wall" && <label>{featureDraft.subtype === "custom_column" ? "柱宽" : featureDraft.subtype === "freight_elevator" ? "货梯长" : "实际净宽"} mm<input type="number" min="1" value={featureDraft.span_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, span_mm: +e.target.value })} /></label>}{featureDraft.subtype === "custom_window" && <label>窗台离地 mm<input type="number" min="0" value={featureDraft.elevation_mm} onChange={(e) => setFeatureDraft({ ...featureDraft, elevation_mm: +e.target.value })} /></label>}<p className="hint code-hint">墙体可连续取点；门窗点击起点和方向点；柱子和货梯第一次点击中心、第二次点击确定方向，系统按真实毫米尺寸生成。</p>{featureDraft.subtype === "freight_elevator" && <p className="hint code-hint">在1F和3F分别使用同一编号（如 LIFT-001）绘制，形成跨楼层定位连接。</p>}</>}
              <div className="dimension-row two"><label>颜色<input type="color" value={featureDraft.color} onChange={(e) => setFeatureDraft({ ...featureDraft, color: e.target.value })} /></label><label>来源<select value={featureDraft.source} onChange={(e) => setFeatureDraft({ ...featureDraft, source: e.target.value })}><option value="manual">人工绘制</option><option value="ai">AI 生成候选</option></select></label></div>
              {!drawMode ? <button type="button" onClick={beginDrawing}>开始在地图绘制</button> : <><div className="draw-status">已取 {drawPoints.length} 个点（mm）</div><div className="button-row"><button type="button" onClick={finishDrawing}>生成候选</button><button type="button" className="secondary" onClick={() => { setDrawMode(null); setDrawPoints([]); }}>取消</button></div></>}
            </div>
          </details>

          <details className="panel-block asset-upload"><summary>新增 PNG / 2.5D 设备素材</summary><form className="compact-form" onSubmit={createAsset}><label>素材名称<input name="name" required /></label><label>分类<input name="category" defaultValue="生产设备" required /></label><label>显示方式<select name="render_type" defaultValue="box25d"><option value="box25d">轻量 2.5D</option><option value="png">透明 PNG</option></select></label><label>PNG 图片<input name="image" type="file" accept="image/png" /></label><label>颜色<input name="color" type="color" defaultValue="#2563eb" /></label><div className="dimension-row"><label>宽 mm<input name="default_width_mm" type="number" min="1" defaultValue="3000" required /></label><label>深 mm<input name="default_depth_mm" type="number" min="1" defaultValue="1800" required /></label><label>高 mm<input name="default_height_mm" type="number" min="1" defaultValue="2000" required /></label></div><button type="submit" disabled={busy}>加入素材库</button></form></details>
        </aside>

        <section className="canvas-panel">
          <div className="canvas-toolbar"><div className="metric"><b>{structureCounts.column}</b><span>柱</span></div><div className="metric"><b>{layout?.placements.length || 0}</b><span>设备</span></div><div className="metric"><b>{layout?.racks.length || 0}</b><span>货架</span></div><div className="metric pallet-metric"><b>{layout?.pallets.length || 0}</b><span>栈板</span></div><div className="metric"><b>{layout?.features.length || 0}</b><span>语义对象</span></div><div className="metric"><b>{totalZoneArea.toFixed(1)}</b><span>区域 m²</span></div><div className="metric violation-metric"><b>{layout?.violations.length || 0}</b><span>违规</span></div></div>
          <div className="visual-toolbar">
            <button type="button" onClick={resetView}>全图适配</button>
            {viewMode === "25d" && <div className="camera-presets" aria-label="固定等距视角">
              {([['north_east', '东北'], ['north_west', '西北'], ['south_east', '东南'], ['south_west', '西南']] as [CameraPreset, string][]).map(([preset, label]) => <button type="button" key={preset} className={cameraPreset === preset ? "active" : ""} onClick={() => { setCameraPreset(preset); resetView(); }}>{label}</button>)}
            </div>}
            <button type="button" className={measureMode ? "measure active" : "measure"} onClick={toggleMeasure}>{measureMode ? "结束测距" : "毫米测距"}</button>
            {measuredDistance && <strong className="measure-result">{measuredDistance}</strong>}
            <span className="toolbar-spacer" />
            <button type="button" onClick={exportPng}>导出 PNG</button>
            <button type="button" onClick={printA3}>A3 / PDF</button>
          </div>
          {layout?.floor_code.toUpperCase() === "1F" && <div className={`floor1-actionbar ${selectedPlacement || selectedFeature?.feature_kind === "structure" ? "has-selection" : ""}`}>
            <div><b>一楼现场校正</b><span>{selectedPlacement ? `${selectedPlacement.name} · X ${Math.round(selectedPlacement.x_mm)} / Y ${Math.round(selectedPlacement.y_mm)} mm` : selectedFeature?.feature_kind === "structure" ? `${selectedFeature.feature_code} · ${selectedFeature.name}` : "从右侧校正清单选择墙体或设备"}</span></div>
            {selectedPlacement && <><span className={`calibration-state ${selectedPlacement.is_locked ? "locked" : "editable"}`}>{selectedPlacement.is_locked ? "已锁定" : "可移动 · 待重新确认"}</span><button type="button" className={selectedPlacement.is_locked ? "unlock-action" : "confirm-action"} onClick={toggleEquipmentLock}>{selectedPlacement.is_locked ? "解除锁定并调整" : "调整完成，确认并锁定设备"}</button></>}
            {selectedFloor1EditableStructure && <><span className="calibration-state editable">可拖动 · 可修改 · 可删除</span><button type="button" className="delete-action" onClick={removeFeature}>删除这段结构</button></>}
            {selectedFloor1Anchor && <span className="calibration-state locked">柱子/货梯定位基准不可修改</span>}
          </div>}
          {layout ? <EditorCanvas layout={layout} assets={assets} viewMode={viewMode} cameraPreset={cameraPreset} viewResetToken={viewResetToken} selected={selected} layers={layers} referenceLayout={referenceLayout} referenceOverlay={referenceOverlay} productionProjections={productionProjection?.items || []} palletSnapEnabled={palletSnapEnabled} palletSnapThresholdMm={palletSnapThresholdMm} drawMode={drawMode} drawPoints={drawPoints} measureMode={measureMode} measurePoints={measurePoints} onSelect={selectFromMap} onMoveEquipment={moveEquipment} onMoveRack={moveRack} onMovePallet={movePallet} onMoveFeature={moveFeature} onDropAsset={dropAsset} onDropRack={dropRack} onDropPallet={dropPallet} onDrawPoint={addDrawPoint} onMeasurePoint={addMeasurePoint} /> : <div className="empty-state">请先导入 DXF 或等待演示布局加载。</div>}
          <div className="canvas-footnote">滚轮缩放 · 2D左键拖动空白处平移 · 2D拖动区域和人工结构 · 2D自由拖动栈板 · 青色信标为ERP生产任务只读投影 · 2.5D左键旋转 · 右键/中键平移 · LIFT 同编号连接楼层</div>
        </section>

        <aside className="sidebar right-panel">
          {layout?.floor_code.toUpperCase() === "1F" && <section className="panel-block production-projection-panel">
            <div className="production-projection-title"><div><h2>真实生产周转</h2><p>ERP权威任务 · 地图仅保存人工定位</p></div><span>只读投影</span></div>
            <div className={`production-source-state ${productionProjection?.available ? "online" : "offline"}`}>
              <i />
              <div><b>{productionProjection?.source_label || "正在连接ERP生产任务…"}</b><small>{productionProjection?.available ? `最近刷新 ${formatProjectionTime(productionProjection.fetched_at)}` : productionProjection?.error || "读取中"}</small></div>
              <button type="button" disabled={productionBusy} onClick={() => layout && loadProductionProjections(layout.id, true)}>刷新</button>
            </div>
            {productionProjection?.available && <>
              <div className="projection-summary"><span><b>{productionProjection.items.length}</b>待生产</span><span><b>{productionProjection.items.filter((item) => item.mapping && !item.mapping.target_missing).length}</b>已定位</span><span><b>{productionProjection.items.filter((item) => !item.mapping || item.mapping.target_missing).length}</b>待定位</span></div>
              <input className="production-search" placeholder="搜索单号、客户、品号或产品" value={productionSearch} onChange={(event) => setProductionSearch(event.target.value)} />
              <div className="production-task-list">
                {!visibleProductionTasks.length && <p className="projection-empty">当前ERP没有待生产任务；地图不会生成虚假任务。</p>}
                {visibleProductionTasks.map((task) => <button type="button" key={task.source_task_id} className={`${productionTaskId === task.source_task_id ? "selected" : ""} ${task.mapping && !task.mapping.target_missing ? "mapped" : "pending-location"}`} onClick={() => chooseProductionTask(task)}>
                  <span className="projection-task-head"><b>{task.order_number}</b><em>{task.mapping && !task.mapping.target_missing ? task.mapping.target_code : "待定位"}</em></span>
                  <strong>{task.customer_name}</strong><small>{task.product_code} · {task.product_name}</small><small>{task.planned_quantity} {task.production_quantity_unit === "pieces" ? "件" : "套"} · 更新 {formatProjectionTime(task.task_updated_at)}</small>
                </button>)}
              </div>
              {selectedProductionTask && <form className="projection-bind-form" onSubmit={saveProductionProjection}>
                <div className="projection-readonly-card"><span>ERP只读事实</span><b>{selectedProductionTask.order_number} · 待生产</b><small>{selectedProductionTask.customer_name} / {selectedProductionTask.product_name}</small><small>计划 {selectedProductionTask.planned_quantity} {selectedProductionTask.production_quantity_unit === "pieces" ? "件" : "套"}；此处不能改数量或确认完工</small></div>
                <div className="dimension-row two"><label>定位对象<select value={productionTargetKind} onChange={(event) => { const kind = event.target.value as "pallet" | "zone"; setProductionTargetKind(kind); setProductionTargetId(kind === "pallet" ? layout.pallets[0]?.id || "" : layout.features.find((item) => item.feature_kind === "zone")?.id || ""); }}><option value="pallet">现有栈板</option><option value="zone">现有区域</option></select></label><label>人工选择<select value={productionTargetId} onChange={(event) => setProductionTargetId(event.target.value)}>{productionTargetKind === "pallet" ? layout.pallets.map((item) => <option key={item.id} value={item.id}>{item.pallet_code} · {item.zone_code}</option>) : layout.features.filter((item) => item.feature_kind === "zone").map((item) => <option key={item.id} value={item.id}>{item.feature_code} · {item.name}</option>)}</select></label></div>
                <button type="submit" className="confirm" disabled={productionBusy || !productionTargetId}>{selectedProductionTask.mapping ? "更新人工定位" : "确认投影到地图"}</button>
                {selectedProductionTask.mapping && <button type="button" className="secondary" disabled={productionBusy} onClick={removeProductionProjection}>移回待定位</button>}
                <p>仅写入隔离数字孪生库的来源ID和定位关系，不回写ERP。</p>
              </form>}
              {!!productionProjection.stale_mappings.length && <details className="stale-projection-list"><summary>已失效或已完成的旧定位 {productionProjection.stale_mappings.length}</summary>{productionProjection.stale_mappings.map((item) => <div key={item.id}><b>任务 #{item.source_task_id}</b><span>{item.target_code || "目标已删除"}</span><small>已不再作为当前生产事实显示</small></div>)}</details>}
            </>}
          </section>}
          {layout?.floor_code.toUpperCase() === "1F" && <section className="panel-block floor1-calibration-panel">
            <div className="calibration-title"><div><h2>一楼现场校正</h2><p>按三楼柱网投影复核，逐段处理，不会自动改位置。</p></div><span>柱网 {layout.features.filter((item) => item.subtype === "custom_column").length} 根锁定</span></div>
            <div className="calibration-tabs"><button type="button" className={floor1CalibrationKind === "structure" ? "active" : ""} onClick={() => setFloor1CalibrationKind("structure")}>墙 / 门 / 窗 {floor1EditableStructures.length}</button><button type="button" className={floor1CalibrationKind === "equipment" ? "active" : ""} onClick={() => setFloor1CalibrationKind("equipment")}>设备 {floor1EditableEquipment.length}</button></div>
            <input className="calibration-search" aria-label="搜索一楼校正对象" placeholder="输入墙体编号或设备名称" value={floor1CalibrationSearch} onChange={(event) => setFloor1CalibrationSearch(event.target.value)} />
            <div className="calibration-list">
              {floor1CalibrationKind === "structure" ? floor1EditableStructures.map((item) => <button type="button" key={item.id} className={selected?.kind === "feature" && selected.id === item.id ? "selected" : ""} onClick={() => selectCalibrationObject({ kind: "feature", id: item.id })}><span><b>{item.feature_code}</b><small>{item.name}</small></span><em>{item.subtype === "custom_wall" ? "墙体" : item.subtype === "rolling_door" ? "门" : "窗"} · 可删除</em></button>) : floor1EditableEquipment.map((item) => <button type="button" key={item.id} className={selected?.kind === "equipment" && selected.id === item.id ? "selected" : ""} onClick={() => selectCalibrationObject({ kind: "equipment", id: item.id })}><span><b>{item.name}</b><small>X {Math.round(item.x_mm)} / Y {Math.round(item.y_mm)}</small></span><em className={item.is_locked ? "locked" : "editable"}>{item.is_locked ? "先解锁" : "可拖动"}</em></button>)}
            </div>
            <p className="hint calibration-hint">墙体点中后可直接删除；设备先解除锁定，再在 2D 拖动或输入毫米坐标，完成后重新确认并锁定。</p>
          </section>}
          <details className="panel-block tool-details structure-legend"><summary>结构来源、图例与导入提示</summary><div className="readout"><span>源图</span><strong>{layout?.source_name || "—"}</strong></div><div className="readout"><span>坐标单位</span><strong>mm</strong></div><div className="readout"><span>柱编号</span><strong>COL-{layout?.floor_code || "1F"}-001…</strong></div><div className="color-legend"><span><i className="legend-equipment" />设备</span><span><i className="legend-rack" />货架</span><span><i className="legend-pallet" />栈板</span><span><i className="legend-zone" />区域</span><span><i className="legend-aisle" />通道</span><span><i className="legend-no-go" />禁放</span><span><i className="legend-error" />违规</span></div><p className="locked-note">DXF 外墙、墙体和柱子无移动接口；人工已布置设备不会被候选布局自动移动或覆盖。</p>{(layout?.warnings || []).filter((warning) => !warning.startsWith("COORDINATE_FRAME:")).map((warning) => <p className="warning" key={warning}>{warning}</p>)}</details>

          <section className="panel-block property-panel" id="selected-object-panel"><h2>所选对象</h2>
            {!selected && <p className="hint">点击设备、货架、区域、通道、禁放区或 DXF 结构查看属性。</p>}
            {selectedPlacement && placementDraft && <form className="compact-form" onSubmit={savePlacement}><div className={`state-chip ${selectedPlacement.is_locked ? "confirmed" : "candidate"}`}>{selectedPlacement.is_locked ? "已确认并锁定" : "现场校正中 · 待重新确认"}</div><label>设备名称<input disabled={selectedPlacement.is_locked} value={placementDraft.name} onChange={(e) => setPlacementDraft({ ...placementDraft, name: e.target.value })} /></label><div className="dimension-row"><label>宽 mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.width_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, width_mm: +e.target.value })} /></label><label>深 mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.depth_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, depth_mm: +e.target.value })} /></label><label>高 mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.height_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, height_mm: +e.target.value })} /></label></div><div className="dimension-row two"><label>X mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.x_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, x_mm: +e.target.value })} /></label><label>Y mm<input disabled={selectedPlacement.is_locked} type="number" value={placementDraft.y_mm} onChange={(e) => setPlacementDraft({ ...placementDraft, y_mm: +e.target.value })} /></label></div><div className="readout"><span>旋转</span><strong>{selectedPlacement.rotation_deg}°</strong></div><div className="button-row"><button type="button" className="secondary" disabled={selectedPlacement.is_locked} onClick={rotatePlacement}>旋转 90°</button><button type="submit" disabled={selectedPlacement.is_locked}>保存属性</button></div><button type="button" className={selectedPlacement.is_locked ? "unlock" : "confirm"} onClick={toggleEquipmentLock}>{selectedPlacement.is_locked ? "解除锁定并现场校正" : "调整完成，确认并锁定设备"}</button><button type="button" className="danger" disabled={selectedPlacement.is_locked} onClick={removePlacement}>移除设备</button></form>}
            {selectedRack && rackDraft && <form className="compact-form" onSubmit={saveRack}><div className={`state-chip ${selectedRack.status}`}>{selectedRack.status === "confirmed" ? "已人工确认并锁定" : selectedRack.source === "ai" ? "AI 候选 · 未确认" : "人工候选 · 未确认"}</div><div className="readout"><span>编号</span><strong>{selectedRack.rack_code}</strong></div><button type="button" className="rack-front-button" onClick={() => setRackFocusId(selectedRack.id)}>打开动画正视图</button><label>名称<input disabled={selectedRack.is_locked} value={rackDraft.name} onChange={(e) => setRackDraft({ ...rackDraft, name: e.target.value })} /></label><div className="dimension-row"><label>长 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.width_mm} onChange={(e) => setRackDraft({ ...rackDraft, width_mm: +e.target.value })} /></label><label>宽 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.depth_mm} onChange={(e) => setRackDraft({ ...rackDraft, depth_mm: +e.target.value })} /></label><label>高 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.height_mm} onChange={(e) => setRackDraft({ ...rackDraft, height_mm: +e.target.value })} /></label></div><div className="dimension-row"><label>层<input disabled={selectedRack.is_locked} type="number" value={rackDraft.levels} onChange={(e) => setRackDraft({ ...rackDraft, levels: +e.target.value })} /></label><label>结构格<input disabled={selectedRack.is_locked} type="number" value={rackDraft.bays} onChange={(e) => setRackDraft({ ...rackDraft, bays: +e.target.value })} /></label><label>货物排数<input disabled={selectedRack.is_locked} type="number" min="3" max="5" value={rackDraft.cargo_rows} onChange={(e) => setRackDraft({ ...rackDraft, cargo_rows: +e.target.value })} /></label></div><label>层板离地高度 mm（逗号分隔）<input disabled={selectedRack.is_locked} value={rackDraft.level_heights_mm.join(",")} onChange={(e) => setRackDraft({ ...rackDraft, level_heights_mm: e.target.value.split(",").map((value) => Number(value.trim())).filter(Number.isFinite) })} /></label><label>通道 mm<input disabled={selectedRack.is_locked} type="number" value={rackDraft.min_aisle_width_mm} onChange={(e) => setRackDraft({ ...rackDraft, min_aisle_width_mm: +e.target.value })} /></label><label>正面方向<select disabled={selectedRack.is_locked} value={rackDraft.access_side} onChange={(e) => setRackDraft({ ...rackDraft, access_side: e.target.value as Rack["access_side"] })}><option value="north">北</option><option value="south">南</option><option value="east">东</option><option value="west">西</option><option value="both">双面</option></select></label><div className="button-row"><button type="button" className="secondary" disabled={selectedRack.is_locked} onClick={rotateRack}>旋转 90°</button><button type="submit" disabled={selectedRack.is_locked}>保存候选</button></div>{selectedRack.status === "candidate" && <button type="button" className="confirm" onClick={confirmRack}>人工确认并锁定</button>}<button type="button" className="danger" disabled={selectedRack.is_locked} onClick={removeRack}>删除货架候选</button></form>}
            {selectedPallet && palletDraft && selectedPalletState && <form className="compact-form pallet-properties" onSubmit={savePallet}>
              <div className="state-chip pallet-state" style={{ borderColor: selectedPalletState.color, color: selectedPalletState.color }}>{selectedPalletState.label} · {selectedPallet.zone_code}</div>
              {selectedPallet.is_simulated && <div className="simulated-badge">隔离模拟数据 · 不绑定正式库存</div>}
              <div className="readout"><span>编号</span><strong>{selectedPallet.pallet_code}</strong></div>
              <label>名称<input value={palletDraft.name} onChange={(e) => setPalletDraft({ ...palletDraft, name: e.target.value })} /></label>
              <label>生产周转状态<select value={palletDraft.visual_status} onChange={(e) => setPalletDraft({ ...palletDraft, visual_status: e.target.value as Pallet["visual_status"] })}>{PALLET_VISUAL_STATES.map((item) => <option key={item.value} value={item.value}>{item.label} · {item.description}</option>)}</select></label>
              <label>现场说明<input maxLength={160} placeholder="例如：印刷后待模切（不填客户、数量）" value={palletDraft.status_note} onChange={(e) => setPalletDraft({ ...palletDraft, status_note: e.target.value })} /></label>
              <div className="dimension-row"><label>长 mm<input type="number" min="1" value={palletDraft.width_mm} onChange={(e) => setPalletDraft({ ...palletDraft, width_mm: +e.target.value })} /></label><label>宽 mm<input type="number" min="1" value={palletDraft.depth_mm} onChange={(e) => setPalletDraft({ ...palletDraft, depth_mm: +e.target.value })} /></label><label>高 mm<input type="number" min="1" value={palletDraft.height_mm} onChange={(e) => setPalletDraft({ ...palletDraft, height_mm: +e.target.value })} /></label></div>
              <div className="dimension-row two"><label>X mm<input type="number" value={palletDraft.x_mm} onChange={(e) => setPalletDraft({ ...palletDraft, x_mm: +e.target.value })} /></label><label>Y mm<input type="number" value={palletDraft.y_mm} onChange={(e) => setPalletDraft({ ...palletDraft, y_mm: +e.target.value })} /></label></div>
              <label>栈板木色<input type="color" value={palletDraft.color} onChange={(e) => setPalletDraft({ ...palletDraft, color: e.target.value })} /></label>
              <div className="readout"><span>方向</span><strong>{selectedPallet.rotation_deg}°</strong></div>
              <div className="button-row"><button type="button" className="secondary" onClick={rotatePallet}>旋转 90°</button><button type="submit">保存状态与参数</button></div>
              <button type="button" className="duplicate" onClick={duplicatePallet}>复制并智能排列</button><button type="button" className="danger" onClick={removePallet}>删除栈板</button>
            </form>}
            {selectedStructure && <div className="compact-form"><div className="state-chip confirmed">DXF 只读结构</div><div className="readout"><span>源句柄</span><strong>{selectedStructure.source_handle}</strong></div><div className="readout"><span>识别分类</span><strong>{selectedStructure.kind}</strong></div><div className="readout"><span>图层</span><strong>{selectedStructure.layer}</strong></div><p className="hint">结构本身不修改；若这是误识别的门或墙，可生成一条可撤销的隐藏候选。</p><button type="button" className="danger" onClick={hideSelectedStructure}>隐藏误识别结构</button></div>}
            {selectedFeature && <div className="compact-form">
              <div className={`state-chip ${selectedFeature.status}`}>{selectedFeature.is_locked || selectedFloor1Anchor ? "已确认并锁定" : selectedFeature.status === "confirmed" ? "已人工确认" : selectedFeature.source === "ai" ? "AI 候选 · 未确认" : "人工候选 · 未确认"}</div>
              <div className="readout"><span>编号</span><strong>{selectedFeature.feature_code}</strong></div>
              <div className="readout"><span>名称</span><strong>{selectedFeature.name}</strong></div>
              <div className="readout"><span>分类</span><strong>{selectedFeature.subtype}</strong></div>
              {selectedFeature.feature_kind === "zone" && selectedFeature.subtype === "temporary_turnover" && <p className="temporary-zone-note">临时周转区只用于短时在制品与异常暂存，不会自动变成长期正式库存区。</p>}
              <div className="readout"><span>{selectedFeature.feature_kind === "structure" ? "投影面积" : "面积"}</span><strong>{(selectedFeature.area_mm2 / 1_000_000).toFixed(2)} m²</strong></div>
              {selectedFeature.is_locked || selectedFloor1Anchor ? <p className="locked-note">该柱子或货梯已作为楼层共同参照确认并锁定，不能拖动、改尺寸或删除。</p> : selectedFeature.subtype !== "dxf_hidden" && <p className="hint">在2D视图按住对象即可整体拖动；松开后保存毫米坐标并重新变为待确认。</p>}
              {featureSemanticDraft && <form className="compact-form geometry-form" onSubmit={saveFeatureSemantics}>
                <b>区域用途</b>
                <label>用途分类<select value={featureSemanticDraft.subtype} onChange={(e) => setFeatureSemanticDraft({ ...featureSemanticDraft, subtype: e.target.value, color: zoneSemanticColors[e.target.value] || featureSemanticDraft.color })}>{zoneTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                <label>区域名称<input value={featureSemanticDraft.name} onChange={(e) => setFeatureSemanticDraft({ ...featureSemanticDraft, name: e.target.value })} /></label>
                <button type="submit">保存区域用途</button><p className="hint">修改用途不会移动区域，也不会修改库存；保存后仍需人工确认。</p>
              </form>}
              {featureGeometryDraft && <form className="compact-form geometry-form" onSubmit={saveFeatureGeometry}>
                <b>区域长宽与位置</b>
                <div className="dimension-row two"><label>中心 X mm<input type="number" value={featureGeometryDraft.center_x_mm} onChange={(e) => setFeatureGeometryDraft({ ...featureGeometryDraft, center_x_mm: +e.target.value })} /></label><label>中心 Y mm<input type="number" value={featureGeometryDraft.center_y_mm} onChange={(e) => setFeatureGeometryDraft({ ...featureGeometryDraft, center_y_mm: +e.target.value })} /></label></div>
                <div className="dimension-row two"><label>X方向尺寸 mm<input type="number" min="1" value={featureGeometryDraft.width_mm} onChange={(e) => setFeatureGeometryDraft({ ...featureGeometryDraft, width_mm: +e.target.value })} /></label><label>Y方向尺寸 mm<input type="number" min="1" value={featureGeometryDraft.height_mm} onChange={(e) => setFeatureGeometryDraft({ ...featureGeometryDraft, height_mm: +e.target.value })} /></label></div>
                <div className="nudge-grid"><span /><button type="button" onClick={() => nudgeFeatureGeometry(0, 100)}>Y +100</button><span /><button type="button" onClick={() => nudgeFeatureGeometry(-100, 0)}>X -100</button><button type="button" onClick={() => nudgeFeatureGeometry(0, -100)}>Y -100</button><button type="button" onClick={() => nudgeFeatureGeometry(100, 0)}>X +100</button></div>
                <button type="submit">保存区域长宽位置</button><p className="hint">尺寸按外包框缩放，编号和多边形顶点顺序不变。</p>
              </form>}
              {structureGeometryDraft && !selectedFeature.is_locked && !selectedFloor1Anchor && <form className="compact-form geometry-form" onSubmit={saveStructureGeometry}>
                <b>人工结构真实尺寸</b>
                {selectedFeature.subtype === "freight_elevator" && <div className={`connector-status ${elevatorPeers.length ? "linked" : "unlinked"}`}><strong>{selectedFeature.feature_code}</strong><span>{elevatorPeers.length ? `已连接 ${layout?.floor_code} ↔ ${elevatorPeers.map((item) => item.floor_code).join(" / ")}` : "尚未连接：请在另一楼层使用相同编号绘制"}</span><small>两层位置独立；保存尺寸时同步长、宽、高。</small></div>}
                <div className="dimension-row two"><label>{selectedFeature.subtype === "custom_column" ? "柱深" : selectedFeature.subtype === "freight_elevator" ? "货梯宽" : "厚度"} mm<input type="number" min="1" value={structureGeometryDraft.width_mm} onChange={(e) => setStructureGeometryDraft({ ...structureGeometryDraft, width_mm: +e.target.value })} /></label><label>高度 mm<input type="number" min="1" value={structureGeometryDraft.storage_height_mm} onChange={(e) => setStructureGeometryDraft({ ...structureGeometryDraft, storage_height_mm: +e.target.value })} /></label></div>
                {selectedFeature.subtype !== "custom_wall" && <label>{selectedFeature.subtype === "custom_column" ? "柱宽" : selectedFeature.subtype === "freight_elevator" ? "货梯长" : "净宽"} mm<input type="number" min="1" value={structureGeometryDraft.span_mm} onChange={(e) => setStructureGeometryDraft({ ...structureGeometryDraft, span_mm: +e.target.value })} /></label>}
                {selectedFeature.subtype === "custom_window" && <label>窗台离地 mm<input type="number" min="0" value={structureGeometryDraft.elevation_mm} onChange={(e) => setStructureGeometryDraft({ ...structureGeometryDraft, elevation_mm: +e.target.value })} /></label>}
                <button type="submit">保存结构尺寸</button>
              </form>}
              {selectedFeature.feature_kind === "zone" && featureVerticalDraft && <form className="compact-form vertical-form" onSubmit={saveFeatureVertical}><label>存放层级<select value={featureVerticalDraft.storage_mode} onChange={(e) => { const mode = e.target.value as LayoutFeature["storage_mode"]; setFeatureVerticalDraft({ ...featureVerticalDraft, storage_mode: mode, elevation_mm: mode === "floor" ? 0 : featureVerticalDraft.elevation_mm || 2500 }); }}><option value="floor">地面堆放</option><option value="rack">货架上层</option><option value="overhead">架空共享</option></select></label>{featureVerticalDraft.storage_mode !== "floor" && <div className="dimension-row two"><label>离地高度 mm<input type="number" min="1" value={featureVerticalDraft.elevation_mm} onChange={(e) => setFeatureVerticalDraft({ ...featureVerticalDraft, elevation_mm: +e.target.value })} /></label><label>占用高度 mm<input type="number" min="1" value={featureVerticalDraft.storage_height_mm} onChange={(e) => setFeatureVerticalDraft({ ...featureVerticalDraft, storage_height_mm: +e.target.value })} /></label></div>}<button type="submit">保存垂直属性</button></form>}
              {selectedFeature.status === "candidate" && <button type="button" className="confirm" onClick={confirmFeature}>人工确认候选</button>}
              <button type="button" className="danger" disabled={selectedFeature.is_locked || selectedFloor1Anchor} onClick={removeFeature}>{selectedFeature.subtype === "dxf_hidden" ? "删除并恢复 DXF 结构" : "删除对象"}</button>
            </div>}
          </section>

          <section className="panel-block"><h2>规则检查</h2><p className="hint">三楼现场标准：人/液压搬运车共用主通道 {layout?.rule_defaults.shared_main || 1900}、次通道 {layout?.rule_defaults.shared_secondary || 1500} mm；栈板允许放在货架下方，但不能占用通道、设备、柱子或禁放区。</p>{!layout?.violations.length ? <p className="pass-note">未发现简单规则冲突</p> : <div className="violation-list">{layout.violations.map((item) => <button key={item.id} onClick={() => item.entity_kind === "rack" ? setSelected({ kind: "rack", id: item.entity_id }) : item.entity_kind === "equipment" ? setSelected({ kind: "equipment", id: item.entity_id }) : item.entity_kind === "pallet" ? setSelected({ kind: "pallet", id: item.entity_id }) : setSelected({ kind: "feature", id: item.entity_id })}><b>{item.severity === "error" ? "违规" : "提醒"}</b><span>{item.message}</span></button>)}</div>}</section>

          <details className="panel-block tool-details"><summary>全部语义对象清单（{(layout?.pallets.length || 0) + (layout?.racks.length || 0) + (layout?.features.length || 0)}）</summary><div className="object-list">{(layout?.pallets || []).map((item) => { const state = palletStatusInfo(item.visual_status); return <button key={item.id} onClick={() => setSelected({ kind: "pallet", id: item.id })}><b><i className="object-status-dot" style={{ background: state.color }} />{item.pallet_code}</b><span>{state.label} · {item.zone_code} · {item.width_mm}×{item.depth_mm}</span></button>; })}{(layout?.racks || []).map((item) => <button key={item.id} onClick={() => { setSelected({ kind: "rack", id: item.id }); setRackFocusId(item.id); }}><b>{item.rack_code}</b><span>点击正视 · {item.status === "confirmed" ? "已确认" : "候选"}</span></button>)}{(layout?.features || []).map((item) => <button key={item.id} onClick={() => setSelected({ kind: "feature", id: item.id })}><b>{item.feature_code}</b><span>{(item.area_mm2 / 1_000_000).toFixed(1)} m² · {item.status === "confirmed" ? "已确认" : "候选"}</span></button>)}</div></details>
        </aside>
      </section>
      {focusedRack && <RackFrontView rack={focusedRack} onClose={() => setRackFocusId(null)} />}
    </main>
  );
}
