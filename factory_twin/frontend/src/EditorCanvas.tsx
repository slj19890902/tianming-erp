import { useEffect, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { formatDistanceMm, niceScaleLengthMm } from "./factoryVisuals.mjs";
import {
  addGroupOutlines,
  buildEquipmentVisual,
  buildPalletMarkerVisual,
  buildPalletVisual,
  buildRackVisual,
  createGroundArrow,
  createHatchTexture,
  palletMarkerSpec
} from "./industrialScene";
import { snapPalletPosition } from "./palletSnap.mjs";
import { snapRackPosition } from "./rackSnap.mjs";
import { createMapKeyboard, MAP_KEYBOARD_HINT } from "./mapKeyboard.mjs";
import { palletStatusInfo } from "./palletStatus";
import {
  aisleSurfaceStyle,
  effectiveMapFeatures,
  operationalEntitySelectable,
  wallSurfaceStyle,
  warehouseAisleColor,
  warehouseFrustumDivisor,
  warehousePassageEnvelope,
  warehousePassageSurfaceStyle,
  shouldShowWarehousePalletVisual,
  warehouseZoneColor
} from "./operationalView.mjs";
import { transformReferencePoint } from "./referenceOverlay.mjs";
import type {
  AssetTemplate,
  CameraPreset,
  LayerVisibility,
  Layout,
  Pallet,
  ProductionTaskProjection,
  Rack,
  ReferenceOverlayConfig,
  SelectedEntity,
  Structure,
  ViewMode
} from "./types";

const WAREHOUSE_PICK_LAYER = 31;

interface Props {
  layout: Layout;
  assets: AssetTemplate[];
  viewMode: ViewMode;
  cameraPreset: CameraPreset;
  viewResetToken: number;
  selected: SelectedEntity;
  layers: LayerVisibility;
  referenceLayout?: Layout | null;
  referenceOverlay?: ReferenceOverlayConfig | null;
  productionProjections?: ProductionTaskProjection[];
  highlightFeatureIds?: string[];
  highlightedPalletIds?: string[];
  productQuantityLabels?: Record<string, string>;
  selectedAreaFeatureId?: string;
  sourcePalletIds?: string[];
  mergeTargetPalletId?: string;
  moveLocationStates?: Record<string, string>;
  draggablePalletIds?: string[];
  focusTarget?: CanvasFocusTarget | null;
  palletEditingOnly?: boolean;
  rackEditingEnabled?: boolean;
  featureEditingEnabled?: boolean;
  aisleEditingEnabled?: boolean;
  mapPanLocked?: boolean;
  allowPalletSelection?: boolean;
  preferStorageSelection?: boolean;
  hideRackLocationMarkers?: boolean;
  palletSnapEnabled: boolean;
  palletSnapThresholdMm: number;
  drawMode: "zone" | "aisle" | "no_go" | "structure" | null;
  drawPoints: number[][];
  drawPointLabels?: string[];
  calibrationMode?: boolean;
  measureMode: boolean;
  measurePoints: number[][];
  onSelect: (entity: SelectedEntity) => void;
  onMoveEquipment: (id: string, xMm: number, yMm: number) => void;
  onMoveRack: (id: string, xMm: number, yMm: number) => void;
  onMovePallet: (id: string, xMm: number, yMm: number) => void;
  onMoveFeature: (id: string, deltaXmm: number, deltaYmm: number) => void;
  onNudgeFeature?: (id: string, deltaXmm: number, deltaYmm: number) => void;
  onFinishFeatureNudge?: (id: string) => void;
  onNudgePallet?: (id: string, deltaXmm: number, deltaYmm: number) => void;
  onFinishPalletNudge?: () => void;
  onFeatureContextMenu?: (id: string, clientX: number, clientY: number) => void;
  onEntityContextMenu?: (entity: NonNullable<SelectedEntity>, clientX: number, clientY: number) => boolean;
  onDropAsset: (templateId: string, xMm: number, yMm: number) => void;
  onDropRack: (rack: Record<string, unknown>, xMm: number, yMm: number) => void;
  onDropPallet: (pallet: Record<string, unknown>, xMm: number, yMm: number) => void;
  onDrawPoint: (xMm: number, yMm: number) => void;
  onMeasurePoint: (xMm: number, yMm: number) => void;
  readOnly?: boolean;
  visualTheme?: "editor" | "warehouse";
  showInternalCodes?: boolean;
}

export interface CanvasFocusTarget {
  entity: NonNullable<SelectedEntity>;
  token: number;
  source: "search" | "selection";
}

interface CanvasRuntime {
  scene: THREE.Scene;
  camera: THREE.OrthographicCamera;
  controls: OrbitControls;
  entityNodes: Map<string, THREE.Object3D>;
  selectionHighlight: THREE.Group;
  searchHighlight: THREE.Group;
  resultHighlight: THREE.Group;
  productQuantityGroup: THREE.Group;
  focusFrame: number | null;
  requestRender: () => void;
  viewMode: ViewMode;
  layoutId: string;
}

function entityKey(entity: NonNullable<SelectedEntity>) {
  return `${entity.kind}:${entity.id}`;
}

function usesRealEastCompass(layout: Layout) {
  const floorCode = layout.floor_code.toUpperCase();
  if (["1F", "3F"].includes(floorCode)) return true;
  const calibration = layout.metadata?.calibration || layout.calibration;
  return floorCode === "4F"
    && (
      (calibration?.status === "aligned" && calibration?.applied === true)
      || (layout.alignment_status === "aligned" && layout.alignment_applied === true)
    );
}

function clearHighlightGroup(group: THREE.Group) {
  for (const child of [...group.children]) {
    group.remove(child);
    child.traverse((object) => {
      if (object instanceof THREE.Sprite) {
        object.material.map?.dispose();
        object.material.dispose();
        return;
      }
      if (!(object instanceof THREE.Mesh || object instanceof THREE.Line || object instanceof THREE.LineSegments)) return;
      object.geometry.dispose();
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      materials.forEach((material) => material.dispose());
    });
  }
}

function syncProductQuantities(runtime: CanvasRuntime, labels?: Record<string, string>) {
  clearHighlightGroup(runtime.productQuantityGroup);
  for (const [id, text] of Object.entries(labels || {})) {
    const node = runtime.entityNodes.get(`pallet:${id}`);
    if (!node) continue;
    const box = new THREE.Box3().setFromObject(node);
    if (box.isEmpty()) continue;
    const label = textSprite(text, '#1d4ed8', 1800, 340, true);
    label.position.copy(box.getCenter(new THREE.Vector3()));
    label.position.y = box.max.y + 220;
    label.renderOrder = 150;
    runtime.productQuantityGroup.add(label);
  }
  runtime.requestRender();
}

function addEntityHighlight(group: THREE.Group, object: THREE.Object3D, color: number, paddingMm: number, order = 100) {
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return;
  box.expandByScalar(paddingMm);
  const helper = new THREE.Box3Helper(box, color);
  const helperMaterial = helper.material as THREE.LineBasicMaterial;
  helperMaterial.transparent = true;
  helperMaterial.opacity = 0.98;
  helper.renderOrder = order;
  group.add(helper);
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const marker = new THREE.Mesh(
    new THREE.BoxGeometry(Math.max(size.x, 220), 26, Math.max(size.z, 220)),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: order >= 110 ? 0.94 : (order < 100 ? 0.32 : 0.62), depthTest: false, depthWrite: false })
  );
  marker.position.set(center.x, box.max.y + 90, center.z);
  marker.renderOrder = order + 1;
  group.add(marker);
}

function syncEntityHighlights(runtime: CanvasRuntime, selected: SelectedEntity, focusTarget: CanvasFocusTarget | null | undefined, moveStates?: Record<string, string>) {
  clearHighlightGroup(runtime.selectionHighlight);
  clearHighlightGroup(runtime.searchHighlight);
  const focusedKey = focusTarget ? entityKey(focusTarget.entity) : null;
  if (selected && entityKey(selected) !== focusedKey && !(selected.kind === "pallet" && moveStates?.[selected.id])) {
    const selectedObject = runtime.entityNodes.get(entityKey(selected));
    if (selectedObject) addEntityHighlight(runtime.selectionHighlight, selectedObject, selected.kind === "feature" ? 0xe9d5ff : 0xf59e0b, 90, selected.kind === "feature" ? 88 : 100);
  }
  if (focusTarget && !(focusTarget.entity.kind === "pallet" && moveStates?.[focusTarget.entity.id])) {
    const focusedObject = runtime.entityNodes.get(focusedKey!);
    if (focusedObject) addEntityHighlight(runtime.searchHighlight, focusedObject,
      focusTarget.source === "selection" ? (focusTarget.entity.kind === "feature" ? 0xe9d5ff : 0xf59e0b)
        : focusTarget.entity.kind === "feature" ? 0xbbf7d0 : 0xffeb00, 160,
      focusTarget.entity.kind === "feature" ? 90 : focusTarget.source === "search" ? 110 : 100);
  }
  runtime.requestRender();
}

function syncResultHighlights(runtime: CanvasRuntime, featureIds: string[], palletIds: string[], mergeTargetPalletId?: string, moveLocationStates?: Record<string, string>, selectedAreaFeatureId?: string, sourcePalletIds: string[] = []) {
  clearHighlightGroup(runtime.resultHighlight);
  if (selectedAreaFeatureId && !featureIds.includes(selectedAreaFeatureId)) {
    const area = runtime.entityNodes.get(`feature:${selectedAreaFeatureId}`);
    if (area) addEntityHighlight(runtime.resultHighlight, area, 0xe9d5ff, 60, 88);
  }
  const colors: Record<string, number> = {empty:0xffffff,occupied:0x2563eb,target:0xf59e0b,source:0xf59e0b,blocked:0x94a3b8};
  for (const [id, state] of Object.entries(moveLocationStates || {})) {
    const object = runtime.entityNodes.get(`pallet:${id}`);
    if (object) addEntityHighlight(runtime.resultHighlight, object, colors[state] ?? colors.blocked, state === "target" ? 160 : 20);
  }
  for (const id of featureIds) {
    const object = runtime.entityNodes.get(`feature:${id}`);
    if (object) addEntityHighlight(runtime.resultHighlight, object, 0xbbf7d0, 120, 90);
  }
  for (const id of sourcePalletIds) {
    const object = runtime.entityNodes.get(`pallet:${id}`);
    if (object) addEntityHighlight(runtime.resultHighlight, object, 0xf59e0b, 100);
  }
  for (const id of palletIds) {
    const object = runtime.entityNodes.get(`pallet:${id}`);
    if (object) addEntityHighlight(runtime.resultHighlight, object, 0xffeb00, 100, 110);
  }
  if (mergeTargetPalletId) {
    const target = runtime.entityNodes.get(`pallet:${mergeTargetPalletId}`);
    if (target) addEntityHighlight(runtime.resultHighlight, target, 0xf59e0b, 200);
  }
  runtime.requestRender();
}

function animateFocus(runtime: CanvasRuntime, focusTarget: CanvasFocusTarget) {
  const object = runtime.entityNodes.get(entityKey(focusTarget.entity));
  if (!object) return false;
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return false;
  if (runtime.focusFrame !== null) cancelAnimationFrame(runtime.focusFrame);
  const boxCenter = box.getCenter(new THREE.Vector3());
  const boxSize = box.getSize(new THREE.Vector3());
  const startTarget = runtime.controls.target.clone();
  const endTarget = new THREE.Vector3(boxCenter.x, runtime.viewMode === "25d" ? Math.max(0, boxCenter.y * 0.22) : 0, boxCenter.z);
  const targetOffset = endTarget.clone().sub(startTarget);
  const startPosition = runtime.camera.position.clone();
  const endPosition = startPosition.clone().add(targetOffset);
  const startZoom = runtime.camera.zoom;
  const baseHeight = Math.abs(runtime.camera.top - runtime.camera.bottom);
  const contextSpan = Math.max(boxSize.x, boxSize.z, 1200) * 5;
  const endZoom = THREE.MathUtils.clamp(baseHeight / Math.max(contextSpan, 12000), runtime.controls.minZoom, Math.min(runtime.controls.maxZoom, 6));
  const startedAt = performance.now();
  const durationMs = 360;
  const step = (now: number) => {
    const progress = Math.min(1, (now - startedAt) / durationMs);
    const eased = 1 - Math.pow(1 - progress, 3);
    runtime.controls.target.lerpVectors(startTarget, endTarget, eased);
    runtime.camera.position.lerpVectors(startPosition, endPosition, eased);
    runtime.camera.zoom = THREE.MathUtils.lerp(startZoom, endZoom, eased);
    runtime.camera.updateProjectionMatrix();
    runtime.controls.update();
    runtime.requestRender();
    runtime.focusFrame = progress < 1 ? requestAnimationFrame(step) : null;
  };
  runtime.focusFrame = requestAnimationFrame(step);
  return true;
}

function materialFor(kind: Structure["kind"], warehouseTheme = false) {
  if (warehouseTheme) {
    const colors: Record<Structure["kind"], number> = {
      exterior_wall: 0x5eead4,
      wall: 0x38bdf8,
      column: 0x99f6e4,
      door: 0xfbbf24,
      window: 0x67e8f9,
      unknown: 0x64748b
    };
    return new THREE.LineBasicMaterial({ color: colors[kind], transparent: true, opacity: 0.86 });
  }
  const colors: Record<Structure["kind"], number> = {
    exterior_wall: 0x1e293b,
    wall: 0x475569,
    column: 0x334155,
    door: 0xd97706,
    window: 0x0284c7,
    unknown: 0x94a3b8
  };
  return new THREE.LineBasicMaterial({ color: colors[kind] });
}

function textSprite(
  text: string,
  color = "#0f172a",
  widthMm = 2200,
  heightMm = 420,
  warehouseTheme = false
) {
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 96;
  const context = canvas.getContext("2d")!;
  context.fillStyle = warehouseTheme ? "rgba(255,255,255,.92)" : "rgba(255,255,255,.9)";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = color;
  context.lineWidth = 6;
  context.strokeRect(2, 2, canvas.width - 4, canvas.height - 4);
  context.fillStyle = warehouseTheme ? "#164e63" : color;
  context.font = "bold 38px Microsoft YaHei, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text.slice(0, 24), canvas.width / 2, canvas.height / 2);
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false }));
  sprite.scale.set(widthMm, heightMm, 1);
  sprite.renderOrder = 30;
  return sprite;
}

function entityNode(object: THREE.Object3D | null): THREE.Object3D | null {
  let current = object;
  while (current) {
    if (current.userData.entityRoot instanceof THREE.Object3D) return current.userData.entityRoot;
    if (current.userData.entityKind && current.userData.entityId) return current;
    current = current.parent;
  }
  return null;
}

function warehousePickProxy(group: THREE.Group) {
  group.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(group);
  if (box.isEmpty()) return null;
  const size = box.getSize(new THREE.Vector3());
  const center = group.worldToLocal(box.getCenter(new THREE.Vector3()));
  const proxy = new THREE.Mesh(
    new THREE.BoxGeometry(Math.max(size.x, 160), Math.max(size.y, 180), Math.max(size.z, 160)),
    new THREE.MeshBasicMaterial({
      transparent: true,
      opacity: 0.001,
      depthWrite: false,
      colorWrite: false
    })
  );
  proxy.position.copy(center);
  proxy.layers.set(WAREHOUSE_PICK_LAYER);
  proxy.userData.entityRoot = group;
  group.add(proxy);
  return proxy;
}

function warehousePalletPickProxy(pallet: Pallet, viewMode: ViewMode, violated: boolean) {
  const spec = palletMarkerSpec(pallet, viewMode, violated);
  const proxy = new THREE.Mesh(
    new THREE.BoxGeometry(spec.width, spec.pickHeight, spec.depth),
    new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false, colorWrite: false })
  );
  proxy.position.y = spec.pickHeight / 2;
  proxy.layers.set(WAREHOUSE_PICK_LAYER);
  proxy.userData.pickProxy = true;
  return proxy;
}

function warehouseRackPickProxy(rack: Rack) {
  const width = Math.max(rack.width_mm, 400);
  const depth = Math.max(rack.depth_mm, 300);
  const pickHeight = Math.max(180, rack.height_mm);
  const proxy = new THREE.Mesh(
    new THREE.BoxGeometry(width, pickHeight, depth),
    new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false, colorWrite: false })
  );
  proxy.position.y = pickHeight / 2;
  proxy.layers.set(WAREHOUSE_PICK_LAYER);
  return proxy;
}

function passageGeometryFromEnvelope(envelope: number[][], centerX: number, centerY: number) {
  const rowCount = Math.floor(envelope.length / 2);
  if (rowCount < 2) return null;
  const positions: number[] = [];
  const point = ([x, y]: number[]) => [x - centerX, 0, -(y - centerY)];
  for (let index = 0; index < rowCount - 1; index += 1) {
    const leftTop = point(envelope[index]);
    const leftBottom = point(envelope[index + 1]);
    const rightTop = point(envelope[envelope.length - 1 - index]);
    const rightBottom = point(envelope[envelope.length - 2 - index]);
    positions.push(
      ...leftTop, ...rightTop, ...leftBottom,
      ...leftBottom, ...rightTop, ...rightBottom
    );
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.computeVertexNormals();
  return geometry;
}

type WarehousePalletInstance = {
  pallet: Pallet;
  position: THREE.Vector3;
  rotationY: number;
  violated: boolean;
};

function addWarehousePalletInstances(scene: THREE.Scene, entries: WarehousePalletInstance[], viewMode: ViewMode) {
  if (!entries.length) return;
  const base = new THREE.InstancedMesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.84 }),
    entries.length
  );
  const load = new THREE.InstancedMesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.76 }),
    entries.length
  );
  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  const scale = new THREE.Vector3();
  const center = new THREE.Vector3();
  const axisY = new THREE.Vector3(0, 1, 0);

  entries.forEach(({ pallet, position, rotationY, violated }, index) => {
    const spec = palletMarkerSpec(pallet, viewMode, violated);
    quaternion.setFromAxisAngle(axisY, rotationY);
    center.set(position.x, spec.baseY, position.z);
    scale.set(spec.width, spec.baseHeight, spec.depth);
    matrix.compose(center, quaternion, scale);
    base.setMatrixAt(index, matrix);
    base.setColorAt(index, new THREE.Color(spec.baseColor));

    center.set(position.x, spec.loadY, position.z);
    scale.set(spec.loadWidth, spec.loadHeight, spec.loadDepth);
    matrix.compose(center, quaternion, scale);
    load.setMatrixAt(index, matrix);
    load.setColorAt(index, new THREE.Color(spec.loadColor));
  });

  base.instanceMatrix.needsUpdate = true;
  load.instanceMatrix.needsUpdate = true;
  if (base.instanceColor) base.instanceColor.needsUpdate = true;
  if (load.instanceColor) load.instanceColor.needsUpdate = true;
  base.computeBoundingBox();
  base.computeBoundingSphere();
  load.computeBoundingBox();
  load.computeBoundingSphere();
  base.userData.warehouseBatch = "pallet-base";
  load.userData.warehouseBatch = "pallet-load";
  scene.add(base, load);
}

export function EditorCanvas({
  layout,
  assets,
  viewMode,
  cameraPreset,
  viewResetToken,
  selected,
  layers,
  referenceLayout,
  referenceOverlay,
  productionProjections = [],
  highlightFeatureIds = [],
  highlightedPalletIds = [],
  productQuantityLabels,
  selectedAreaFeatureId,
  sourcePalletIds = [],
  mergeTargetPalletId,
  moveLocationStates,
  draggablePalletIds,
  focusTarget = null,
  palletEditingOnly = false,
  rackEditingEnabled = false,
  featureEditingEnabled = true,
  aisleEditingEnabled = true,
  mapPanLocked = false,
  allowPalletSelection = false,
  preferStorageSelection = false,
  hideRackLocationMarkers = false,
  palletSnapEnabled,
  palletSnapThresholdMm,
  drawMode,
  drawPoints,
  drawPointLabels = [],
  calibrationMode = false,
  measureMode,
  measurePoints,
  onSelect,
  onMoveEquipment,
  onMoveRack,
  onMovePallet,
  onMoveFeature,
  onNudgeFeature,
  onFinishFeatureNudge,
  onNudgePallet,
  onFinishPalletNudge,
  onFeatureContextMenu,
  onEntityContextMenu,
  onDropAsset,
  onDropRack,
  onDropPallet,
  onDrawPoint,
  onMeasurePoint,
  readOnly = false,
  visualTheme = "editor",
  showInternalCodes = true
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const effectiveDrawMode = calibrationMode ? "structure" : drawMode;
  const canvasMountRef = useRef<HTMLDivElement>(null);
  const scaleBarRef = useRef<HTMLSpanElement>(null);
  const scaleLabelRef = useRef<HTMLElement>(null);
  const northArrowRef = useRef<HTMLSpanElement>(null);
  const coordinateRef = useRef<HTMLElement>(null);
  const runtimeRef = useRef<CanvasRuntime | null>(null);
  const selectedRef = useRef<SelectedEntity>(selected);
  const moveStatesRef = useRef(moveLocationStates);
  moveStatesRef.current = moveLocationStates;
  const nudgeHandlers = useRef({ onNudgeFeature, onFinishFeatureNudge, onNudgePallet, onFinishPalletNudge, featureEditingEnabled, onMoveRack });
  nudgeHandlers.current = { onNudgeFeature, onFinishFeatureNudge, onNudgePallet, onFinishPalletNudge, featureEditingEnabled, onMoveRack };
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  useEffect(() => {
    if (readOnly || viewMode !== "2d") return;
    const keyboard = createMapKeyboard({ begin: () => {
      const selection = selectedRef.current;
      const runtime = runtimeRef.current;
      if (!runtime || (selection?.kind !== "feature" && selection?.kind !== "pallet" && selection?.kind !== "rack")) return;
      const callback = selection.kind === "rack" ? nudgeHandlers.current.onMoveRack : selection.kind === "feature"
        ? nudgeHandlers.current.featureEditingEnabled && nudgeHandlers.current.onNudgeFeature
        : nudgeHandlers.current.onNudgePallet;
      if (!callback) return;
      const entity = runtime.entityNodes.get(`${selection.kind}:${selection.id}`);
      if (!entity?.userData.draggable) return;
      const cameraRotation = runtime.camera.quaternion.clone();
      const rack = layoutRef.current.racks.find(item => item.id === selection.id);
      let dx = 0, dy = 0;
      return {
        move: (step, key) => {
          const direction = new THREE.Vector3(key === "ArrowRight" ? 1 : key === "ArrowLeft" ? -1 : 0,
            key === "ArrowUp" ? 1 : key === "ArrowDown" ? -1 : 0, 0).applyQuaternion(cameraRotation);
          const length = Math.hypot(direction.x, direction.z);
          if (length < .001) return;
          const x = direction.x / length * step, y = -direction.z / length * step;
          dx += x; dy += y;
          if (selection.kind === "rack" && rack) {
            const current = runtimeRef.current;
            const node = current?.entityNodes.get(`rack:${selection.id}`);
            const bounds = layoutRef.current.bounds_mm;
            if (node) { node.position.x = rack.x_mm + dx - (bounds.min_x + bounds.max_x) / 2;
              node.position.z = (bounds.min_y + bounds.max_y) / 2 - rack.y_mm - dy;
              if (current) { syncEntityHighlights(current, selection, focusTargetRef.current, moveStatesRef.current); current.requestRender(); } }
          } else if (selection.kind === "feature") nudgeHandlers.current.onNudgeFeature?.(selection.id, x, y);
          else nudgeHandlers.current.onNudgePallet?.(selection.id, x, y);
        },
        finish: () => {
          if (selection.kind === "rack" && rack) nudgeHandlers.current.onMoveRack(selection.id, rack.x_mm + dx, rack.y_mm + dy);
          else if (selection.kind === "feature") nudgeHandlers.current.onFinishFeatureNudge?.(selection.id);
          else nudgeHandlers.current.onFinishPalletNudge?.();
        },
      };
    } });
    const hidden = () => { if (document.hidden) keyboard.stop(); };
    window.addEventListener("keydown", keyboard.down);
    window.addEventListener("keyup", keyboard.up);
    window.addEventListener("blur", keyboard.stop);
    window.addEventListener("pointerdown", keyboard.stop);
    document.addEventListener("visibilitychange", hidden);
    return () => { keyboard.stop(); window.removeEventListener("keydown", keyboard.down); window.removeEventListener("keyup", keyboard.up);
      window.removeEventListener("blur", keyboard.stop); window.removeEventListener("pointerdown", keyboard.stop); document.removeEventListener("visibilitychange", hidden); };
  }, [readOnly, viewMode, cameraPreset, viewResetToken, selected?.kind, selected?.id, layout.id, rackEditingEnabled, featureEditingEnabled]);
  const focusTargetRef = useRef<CanvasFocusTarget | null>(focusTarget);
  const lastFocusKeyRef = useRef("");
  const handlersRef = useRef({ onSelect, onMoveEquipment, onMoveRack, onMovePallet, onMoveFeature, onFeatureContextMenu, onEntityContextMenu, onDropAsset, onDropRack, onDropPallet, onDrawPoint, onMeasurePoint });
  selectedRef.current = selected;
  focusTargetRef.current = focusTarget;
  handlersRef.current = { onSelect, onMoveEquipment, onMoveRack, onMovePallet, onMoveFeature, onFeatureContextMenu, onEntityContextMenu, onDropAsset, onDropRack, onDropPallet, onDrawPoint, onMeasurePoint };
  const cameraStateRef = useRef<{
    position: [number, number, number];
    target: [number, number, number];
    zoom: number;
  } | null>(null);
  const cameraResetKeyRef = useRef("");

  useEffect(() => {
    const container = containerRef.current;
    const canvasMount = canvasMountRef.current;
    if (!container || !canvasMount) return;
    const width = Math.max(container.clientWidth, 1);
    const height = Math.max(container.clientHeight, 420);
    const bounds = layout.bounds_mm;
    const centerX = (bounds.min_x + bounds.max_x) / 2;
    const centerY = (bounds.min_y + bounds.max_y) / 2;
    const span = Math.max(bounds.max_x - bounds.min_x, bounds.max_y - bounds.min_y, 10000);
    const frustumDivisor = warehouseFrustumDivisor(layout.floor_code, visualTheme);
    const cameraResetKey = `${layout.id}:${viewMode}:${cameraPreset}:${viewResetToken}`;
    const shouldResetCamera = cameraResetKeyRef.current !== cameraResetKey;
    cameraResetKeyRef.current = cameraResetKey;
    const warehouseTheme = visualTheme === "warehouse";
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(warehouseTheme ? 0xf3f5f7 : 0xf8fafc);
    const camera = new THREE.OrthographicCamera(
      (-span * width) / height / frustumDivisor,
      (span * width) / height / frustumDivisor,
      span / frustumDivisor,
      -span / frustumDivisor,
      1,
      span * 10
    );
    if (viewMode === "2d") {
      camera.position.set(0, span * 2, 0.001);
      camera.up.set(0, 0, -1);
    } else {
      const cameraPositions: Record<CameraPreset, [number, number, number]> = {
        fit: [0.95, 0.9, 0.95],
        north_east: [0.95, 0.9, 0.95],
        north_west: [-0.95, 0.9, 0.95],
        south_east: [0.95, 0.9, -0.95],
        south_west: [-0.95, 0.9, -0.95]
      };
      const [px, py, pz] = cameraPositions[cameraPreset];
      camera.position.set(span * px, span * py, span * pz);
      camera.up.set(0, 1, 0);
    }
    camera.lookAt(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: !warehouseTheme, preserveDrawingBuffer: !warehouseTheme });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, warehouseTheme ? 1 : 2));
    renderer.setSize(width, height);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.shadowMap.enabled = viewMode === "25d" && !warehouseTheme;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    canvasMount.replaceChildren(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.enableRotate = viewMode === "25d";
    controls.enablePan = !mapPanLocked;
    if (effectiveDrawMode === "structure") controls.enablePan = false;
    controls.screenSpacePanning = true;
    controls.maxZoom = 12;
    controls.minZoom = 0.25;
    let disposed = false;
    let renderFrame: number | null = null;
    const requestRender = () => {
      if (disposed || renderFrame !== null) return;
      renderFrame = requestAnimationFrame(() => {
        renderFrame = null;
        const controlsChanged = controls.update();
        renderer.render(scene, camera);
        if (controlsChanged) requestRender();
      });
    };
    if (viewMode === "2d") {
      controls.touches.ONE = THREE.TOUCH.PAN;
      controls.mouseButtons.LEFT = THREE.MOUSE.PAN;
      controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN;
      controls.mouseButtons.RIGHT = THREE.MOUSE.PAN;
    } else {
      controls.mouseButtons.LEFT = THREE.MOUSE.PAN;
      controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN;
      controls.mouseButtons.RIGHT = THREE.MOUSE.ROTATE;
    }
    if (!shouldResetCamera && cameraStateRef.current) {
      camera.position.fromArray(cameraStateRef.current.position);
      controls.target.fromArray(cameraStateRef.current.target);
      camera.zoom = cameraStateRef.current.zoom;
      camera.updateProjectionMatrix();
      controls.update();
    }

    scene.add(new THREE.HemisphereLight(warehouseTheme ? 0xffffff : 0xffffff, warehouseTheme ? 0xd6dfe4 : 0x94a3b8, warehouseTheme ? 1.45 : 1.55));
    const directional = new THREE.DirectionalLight(0xffffff, warehouseTheme ? 1.5 : 1.65);
    directional.position.set(span, span * 1.5, span);
    directional.castShadow = viewMode === "25d" && !warehouseTheme;
    directional.shadow.mapSize.set(1024, 1024);
    scene.add(directional);
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(span * 2.3, span * 2.3),
      new THREE.MeshStandardMaterial({
        color: warehouseTheme ? 0xe8edf1 : 0xf1f5f9,
        roughness: 1,
        metalness: warehouseTheme ? 0.04 : 0
      })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -4;
    floor.receiveShadow = true;
    scene.add(floor);
    const passageStyle = warehousePassageSurfaceStyle(visualTheme);
    if (passageStyle.visible) {
      const passageEnvelope = warehousePassageEnvelope(bounds, layout.structures);
      const envelopeGeometry = passageGeometryFromEnvelope(passageEnvelope, centerX, centerY);
      const passageSurface = new THREE.Mesh(
        envelopeGeometry
          ? envelopeGeometry
          : new THREE.PlaneGeometry(
              Math.max(bounds.max_x - bounds.min_x, 1),
              Math.max(bounds.max_y - bounds.min_y, 1)
            ),
        new THREE.MeshBasicMaterial({ color: passageStyle.color, side: THREE.DoubleSide })
      );
      if (!envelopeGeometry) passageSurface.rotation.x = -Math.PI / 2;
      passageSurface.position.y = passageStyle.elevationMm;
      passageSurface.userData = { visualKind: "automatic_passage_surface" };
      scene.add(passageSurface);
    }
    const grid = new THREE.GridHelper(
      span * 2.2,
      40,
      warehouseTheme ? 0x9fb2ba : 0xcbd5e1,
      warehouseTheme ? 0xd2dce1 : 0xe2e8f0
    );
    grid.position.y = warehouseTheme ? 2 : -2;
    scene.add(grid);
    const worldPoint = (xMm: number, yMm: number, elevation = 0) =>
      new THREE.Vector3(xMm - centerX, elevation, -(yMm - centerY));

    if (referenceLayout && referenceOverlay?.enabled && layout.floor_code.toUpperCase() === "1F") {
      const referenceMidX = (referenceLayout.bounds_mm.min_x + referenceLayout.bounds_mm.max_x) / 2;
      const transformReference = (x: number, y: number) => transformReferencePoint(x, y, referenceLayout.bounds_mm, referenceOverlay);
      const inLeftHalf = (points: number[][]) => points.length > 0 && points.reduce((sum, point) => sum + point[0], 0) / points.length <= referenceMidX;
      const referenceGroup = new THREE.Group();
      referenceGroup.name = "3F-left-half-reference-overlay";
      referenceGroup.renderOrder = 24;
      const lineMaterial = new THREE.LineDashedMaterial({ color: 0x0891b2, transparent: true, opacity: referenceOverlay.opacity, dashSize: 420, gapSize: 180, depthTest: false });
      const columnMaterial = new THREE.MeshBasicMaterial({ color: 0x06b6d4, transparent: true, opacity: Math.min(.78, referenceOverlay.opacity + .18), depthTest: false, wireframe: true });
      for (const structure of referenceLayout.structures) {
        if (!["exterior_wall", "wall", "column"].includes(structure.kind)) continue;
        const geometry = structure.geometry;
        if (geometry.type === "polyline" && geometry.points && inLeftHalf(geometry.points)) {
          const transformed = geometry.points.map(([x, y]) => transformReference(x, y));
          const points = transformed.map(([x, y]) => worldPoint(x, y, 95));
          if (geometry.closed && points.length) points.push(points[0].clone());
          const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), lineMaterial);
          line.computeLineDistances();referenceGroup.add(line);
          if (structure.kind === "column") {
            const xs=transformed.map(point=>point[0]),ys=transformed.map(point=>point[1]),column=new THREE.Mesh(new THREE.BoxGeometry(Math.max(120,Math.max(...xs)-Math.min(...xs)),80,Math.max(120,Math.max(...ys)-Math.min(...ys))),columnMaterial),point=worldPoint((Math.max(...xs)+Math.min(...xs))/2,(Math.max(...ys)+Math.min(...ys))/2,105);column.position.copy(point);referenceGroup.add(column);
          }
        } else if (geometry.type === "circle" && (geometry.x_mm || 0) <= referenceMidX) {
          const [x,y]=transformReference(geometry.x_mm||0,geometry.y_mm||0),radius=(geometry.radius_mm||250)*(Math.abs(referenceOverlay.scale_x)+Math.abs(referenceOverlay.scale_y))/2,ring=new THREE.Mesh(new THREE.RingGeometry(Math.max(20,radius-45),radius,24),columnMaterial),point=worldPoint(x,y,105);ring.rotation.x=-Math.PI/2;ring.position.copy(point);referenceGroup.add(ring);
        }
      }
      for (const feature of referenceLayout.features) {
        if (feature.feature_kind !== "structure" || !["custom_wall","custom_column","freight_elevator"].includes(feature.subtype) || !inLeftHalf(feature.points)) continue;
        const transformed=feature.points.map(([x,y])=>transformReference(x,y)),points=transformed.map(([x,y])=>worldPoint(x,y,115));if(points.length<2)continue;const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),lineMaterial);line.computeLineDistances();referenceGroup.add(line);
      }
      scene.add(referenceGroup);
    }

    const updateOverlay = () => {
      const visibleWidthMm = (camera.right - camera.left) / camera.zoom;
      const scaleLengthMm = niceScaleLengthMm(visibleWidthMm * 0.16);
      const currentWidth = Math.max(container.clientWidth, 1);
      const barWidthPx = Math.max(44, Math.min(180, (scaleLengthMm / visibleWidthMm) * currentWidth));
      if (scaleBarRef.current) scaleBarRef.current.style.width = `${barWidthPx}px`;
      if (scaleLabelRef.current) scaleLabelRef.current.textContent = scaleLengthMm >= 1000
        ? `${scaleLengthMm / 1000} m`
        : `${scaleLengthMm} mm`;
      if (northArrowRef.current) {
        const origin = new THREE.Vector3(0, 0, 0).project(camera);
        const north = new THREE.Vector3(0, 0, -1000).project(camera);
        const angle = Math.atan2(north.x - origin.x, north.y - origin.y) * 180 / Math.PI;
        northArrowRef.current.style.transform = `rotate(${angle}deg)`;
      }
    };
    const violationIds = new Set(
      layout.violations.flatMap((item) => [item.entity_id, item.related_id || ""])
    );
    const interactive: THREE.Object3D[] = [];
    const hiddenSourceHandles = new Set(
      layout.features
        .filter((item) => item.feature_kind === "structure" && item.subtype === "dxf_hidden")
        .map((item) => item.name.replace(/^隐藏 DXF 结构\s+/, ""))
    );

    if (layers.structures) {
      for (const structure of layout.structures) {
        if (hiddenSourceHandles.has(structure.source_handle)) continue;
        const geometry = structure.geometry;
        const structureGroup = new THREE.Group();
        structureGroup.userData = { entityKind: "structure", entityId: structure.id, draggable: false };
        if (geometry.type === "polyline" && geometry.points) {
          const points = geometry.points.map(([x, y]) => worldPoint(x, y, 8));
          if (geometry.closed && points.length) points.push(points[0].clone());
          structureGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), materialFor(structure.kind, warehouseTheme)));
          if (
            geometry.closed
            && geometry.points.length >= 3
            && (structure.kind === "wall" || structure.kind === "exterior_wall")
          ) {
            const structureWallStyle = wallSurfaceStyle(visualTheme, viewMode);
            const wallShape = new THREE.Shape();
            geometry.points.forEach(([x, y], index) => {
              const sx = x - centerX;
              const sy = y - centerY;
              if (index === 0) wallShape.moveTo(sx, sy); else wallShape.lineTo(sx, sy);
            });
            wallShape.closePath();
            const wallHeight = viewMode === "25d" ? 3200 : 55;
            const wallMesh = new THREE.Mesh(
              viewMode === "25d"
                ? new THREE.ExtrudeGeometry(wallShape, { depth: wallHeight, bevelEnabled: false })
                : new THREE.ShapeGeometry(wallShape),
              new THREE.MeshStandardMaterial({
                color: warehouseTheme ? (structure.kind === "exterior_wall" ? 0x164e63 : 0x155e75) : structure.kind === "exterior_wall" ? 0x334155 : 0x64748b,
                emissive: warehouseTheme ? 0x062c38 : 0x000000,
                roughness: 0.95,
                transparent: structureWallStyle.transparent,
                opacity: structureWallStyle.opacity,
                depthWrite: structureWallStyle.depthWrite,
                side: THREE.DoubleSide
              })
            );
            wallMesh.rotation.x = -Math.PI / 2;
            wallMesh.position.y = viewMode === "25d" ? 0 : 5;
            wallMesh.castShadow = viewMode === "25d";
            wallMesh.receiveShadow = true;
            structureGroup.add(wallMesh);
          }
          if (structure.kind === "column") {
            const xs = geometry.points.map((point) => point[0]);
            const ys = geometry.points.map((point) => point[1]);
            const columnWidth = Math.max(...xs) - Math.min(...xs) || 600;
            const columnDepth = Math.max(...ys) - Math.min(...ys) || 600;
            const column = new THREE.Mesh(
              new THREE.BoxGeometry(columnWidth, viewMode === "25d" ? 3000 : 40, columnDepth),
              new THREE.MeshStandardMaterial({ color: 0x475569 })
            );
            const center = worldPoint((Math.max(...xs) + Math.min(...xs)) / 2, (Math.max(...ys) + Math.min(...ys)) / 2);
            column.position.set(center.x, viewMode === "25d" ? 1500 : 20, center.z);
            structureGroup.add(column);
          }
        } else if (geometry.type === "circle") {
          const center = worldPoint(geometry.x_mm || 0, geometry.y_mm || 0);
          const radius = geometry.radius_mm || 250;
          const mesh = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, viewMode === "25d" ? 3000 : 40, 20),
            new THREE.MeshStandardMaterial({ color: structure.kind === "column" ? 0x475569 : 0x94a3b8 })
          );
          mesh.position.set(center.x, viewMode === "25d" ? 1500 : 20, center.z);
          structureGroup.add(mesh);
          if (layers.labels && structure.column_code) {
            const label = textSprite(structure.column_code, warehouseTheme ? "#5eead4" : "#0f172a", 1500, 300, warehouseTheme);
            label.position.set(center.x, viewMode === "25d" ? 3300 : 160, center.z);
            scene.add(label);
          }
        } else if (geometry.type === "insert") {
          const center = worldPoint(geometry.x_mm || 0, geometry.y_mm || 0);
          if (geometry.points?.length === 2) {
            const start = worldPoint(geometry.points[0][0], geometry.points[0][1]);
            const end = worldPoint(geometry.points[1][0], geometry.points[1][1]);
            const length = start.distanceTo(end);
            const opening = new THREE.Mesh(
              new THREE.BoxGeometry(length, viewMode === "25d" ? (structure.kind === "door" ? 3200 : 1400) : 55, 90),
              new THREE.MeshStandardMaterial({
                color: structure.kind === "door" ? 0xd97706 : 0x0284c7,
                transparent: true,
                opacity: structure.kind === "door" ? 0.74 : 0.5
              })
            );
            opening.position.copy(start).add(end).multiplyScalar(0.5);
            opening.position.y = viewMode === "25d" ? (structure.kind === "door" ? 1600 : 1600) : 28;
            opening.rotation.y = Math.atan2(geometry.points[1][1] - geometry.points[0][1], geometry.points[1][0] - geometry.points[0][0]);
            structureGroup.add(opening);
          } else {
            const marker = new THREE.Mesh(
              new THREE.BoxGeometry(500, 40, 500),
              new THREE.MeshBasicMaterial({ color: structure.kind === "door" ? 0xd97706 : structure.kind === "window" ? 0x0284c7 : 0x94a3b8 })
            );
            marker.position.set(center.x, 20, center.z);
            structureGroup.add(marker);
          }
        }
        if (operationalEntitySelectable(visualTheme, "structure")) interactive.push(structureGroup);
        scene.add(structureGroup);
      }
    }

    for (const feature of effectiveMapFeatures(visualTheme, layout.features)) {
      if (
        calibrationMode
        && layout.floor_code.toUpperCase() === "4F"
        && feature.feature_kind === "structure"
        && feature.subtype === "freight_elevator"
        && feature.feature_code === "LIFT-002"
      ) continue;
      const visible = feature.feature_kind === "zone" ? layers.zones : feature.feature_kind === "aisle" ? layers.aisles : feature.feature_kind === "structure" ? layers.customStructures : layers.noGo;
      if (feature.feature_kind === "structure" && feature.subtype === "dxf_hidden") continue;
      if (!visible || feature.points.length < 2) continue;
      const protectedAnchor = feature.status === "confirmed" && ["custom_column", "freight_elevator"].includes(feature.subtype);
      const violated = violationIds.has(feature.id);
      const color = violated
        ? "#dc2626"
        : feature.feature_kind === "aisle"
            ? warehouseAisleColor(layout.floor_code, visualTheme, feature.color)
            : feature.feature_kind === "zone"
              ? warehouseZoneColor(visualTheme, feature.color)
              : feature.color;
      const group = new THREE.Group();
      const planningFeatureEditable = !readOnly
        && featureEditingEnabled
        && ["zone", "aisle"].includes(feature.feature_kind)
        && (feature.feature_kind !== "aisle" || aisleEditingEnabled)
        && feature.subtype !== "dxf_hidden"
        && !protectedAnchor;
      group.userData = {
        entityKind: "feature",
        entityId: feature.id,
        draggable: planningFeatureEditable
      };
      if (feature.feature_kind === "structure") {
        for (let index = 0; index < feature.points.length - 1; index += 1) {
          const [x1, y1] = feature.points[index];
          const [x2, y2] = feature.points[index + 1];
          const start = worldPoint(x1, y1);
          const end = worldPoint(x2, y2);
          const length = start.distanceTo(end);
          const heightMm = viewMode === "25d" ? feature.storage_height_mm : 45;
          const elevationMm = viewMode === "25d" && feature.subtype === "custom_window" ? feature.elevation_mm : 0;
          const solidStructure = ["custom_wall", "custom_column", "freight_elevator"].includes(feature.subtype);
          const customWallStyle = feature.subtype === "custom_wall" ? wallSurfaceStyle(visualTheme, viewMode) : null;
          const mesh = new THREE.Mesh(
            new THREE.BoxGeometry(length, heightMm, feature.width_mm || 100),
            new THREE.MeshStandardMaterial({
              color,
              transparent: customWallStyle?.transparent ?? !solidStructure,
              opacity: customWallStyle?.opacity ?? (feature.subtype === "rolling_door" ? 0.72 : feature.subtype === "custom_window" ? 0.48 : 0.94),
              depthWrite: customWallStyle?.depthWrite ?? true,
              roughness: solidStructure ? 0.92 : 0.55
            })
          );
          mesh.position.copy(start).add(end).multiplyScalar(0.5);
          mesh.position.y = elevationMm + heightMm / 2;
          mesh.rotation.y = Math.atan2(y2 - y1, x2 - x1);
          mesh.castShadow = viewMode === "25d";
          group.add(mesh);
        }
      } else if (feature.feature_kind === "aisle") {
        const aisleStyle = aisleSurfaceStyle(visualTheme);
        for (let index = 0; index < feature.points.length - 1; index += 1) {
          const [x1, y1] = feature.points[index];
          const [x2, y2] = feature.points[index + 1];
          const start = worldPoint(x1, y1);
          const end = worldPoint(x2, y2);
          const length = start.distanceTo(end);
          const mesh = new THREE.Mesh(
            new THREE.BoxGeometry(length, aisleStyle.heightMm, feature.width_mm || 1),
            new THREE.MeshBasicMaterial({
              color,
              transparent: aisleStyle.transparent,
              opacity: aisleStyle.opacity,
              depthWrite: aisleStyle.depthWrite,
              polygonOffset: warehouseTheme,
              polygonOffsetFactor: warehouseTheme ? -1 : 0,
              polygonOffsetUnits: warehouseTheme ? -1 : 0
            })
          );
          mesh.position.copy(start).add(end).multiplyScalar(0.5);
          mesh.position.y = aisleStyle.elevationMm;
          mesh.rotation.y = Math.atan2(y2 - y1, x2 - x1);
          group.add(mesh);
          const arrowLength = Math.min(Math.max(length * 0.16, 500), 1400);
          const arrowAngle = Math.atan2(-(y2 - y1), x2 - x1);
          const addArrowAt = (ratio: number, reverse = false) => {
            const arrow = createGroundArrow(arrowLength, violated ? "#dc2626" : "#15803d");
            arrow.position.set(
              start.x + (end.x - start.x) * ratio,
              viewMode === "25d" ? 48 : 36,
              start.z + (end.z - start.z) * ratio
            );
            arrow.rotation.y = arrowAngle + (reverse ? Math.PI : 0);
            group.add(arrow);
          };
          if (feature.direction === "two_way") {
            addArrowAt(0.34);
            addArrowAt(0.66, true);
          } else {
            addArrowAt(0.5);
          }
        }
      } else if (feature.points.length >= 3) {
        const shape = new THREE.Shape();
        feature.points.forEach(([x, y], index) => {
          const sx = x - centerX;
          const sy = y - centerY;
          if (index === 0) shape.moveTo(sx, sy); else shape.lineTo(sx, sy);
        });
        shape.closePath();
        const elevatedZone = feature.feature_kind === "zone" && feature.storage_mode !== "floor";
        const featureGeometry = viewMode === "25d" && elevatedZone
          ? new THREE.ExtrudeGeometry(shape, {
              depth: feature.storage_height_mm,
              bevelEnabled: false
            })
          : new THREE.ShapeGeometry(shape);
        const elevated = viewMode === "25d" && elevatedZone;
        const hatchTexture = !elevated ? createHatchTexture(color, feature.feature_kind === "no_go") : null;
        const mesh = new THREE.Mesh(
          featureGeometry,
          elevated
            ? new THREE.MeshStandardMaterial({ color, transparent: true, opacity: 0.38, roughness: 0.85, side: THREE.DoubleSide })
            : new THREE.MeshBasicMaterial({
                color: 0xffffff,
                map: hatchTexture,
                transparent: true,
                opacity: feature.feature_kind === "no_go" ? 0.72 : 0.5,
                side: THREE.DoubleSide
              })
        );
        mesh.rotation.x = -Math.PI / 2;
        mesh.position.y = viewMode === "25d" && elevatedZone
          ? feature.elevation_mm
          : feature.feature_kind === "no_go" ? 22 : 10;
        group.add(mesh);
        const boundary = new THREE.LineLoop(
          new THREE.BufferGeometry().setFromPoints(feature.points.map(([x, y]) => worldPoint(x, y, 42))),
          new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.95 })
        );
        group.add(boundary);
      }
      if (operationalEntitySelectable(visualTheme, "feature", feature.feature_kind) || planningFeatureEditable) {
        interactive.push(warehouseTheme ? warehousePickProxy(group) || group : group);
      }
      scene.add(group);
      if (layers.labels) {
        const average = feature.points.reduce((sum, point) => [sum[0] + point[0], sum[1] + point[1]], [0, 0]);
        const labelHeight = viewMode === "25d" && feature.feature_kind === "zone" && feature.storage_mode !== "floor"
          ? feature.elevation_mm + feature.storage_height_mm + 260
          : viewMode === "25d" ? 500 : 90;
        const labelAt = worldPoint(average[0] / feature.points.length, average[1] / feature.points.length, labelHeight);
        const employeeFeatureLabel = feature.subtype === "finished_wait_delivery"
          ? "一楼成品合并暂存区"
          : feature.name || "区域名称待完善";
        const levelLabel = showInternalCodes
          ? feature.feature_kind === "zone" && feature.storage_mode !== "floor"
            ? `${feature.feature_code} ↑${feature.elevation_mm}mm`
            : feature.feature_code
          : feature.feature_kind === "zone" && feature.storage_mode !== "floor"
            ? `${employeeFeatureLabel} · 离地 ${feature.elevation_mm}mm`
            : employeeFeatureLabel;
        const label = textSprite(levelLabel, color, 1700, 320, warehouseTheme);
        label.position.copy(labelAt);
        group.add(label);
      }
    }

    const textureLoader = new THREE.TextureLoader();
    if (layers.equipment) {
      for (const placement of layout.placements) {
        const template = assets.find((item) => item.id === placement.template_id);
        const violated = violationIds.has(placement.id);
        const group = new THREE.Group();
        group.userData = { entityKind: "equipment", entityId: placement.id, draggable: !readOnly && !palletEditingOnly && !placement.is_locked };
        if (template?.render_type === "png" && template.image_url) {
          const texture = textureLoader.load(template.image_url, requestRender);
          texture.colorSpace = THREE.SRGBColorSpace;
          const mesh = new THREE.Mesh(
            new THREE.PlaneGeometry(placement.width_mm, placement.depth_mm),
            new THREE.MeshBasicMaterial({ map: texture, transparent: true, side: THREE.DoubleSide })
          );
          mesh.rotation.x = -Math.PI / 2;
          mesh.position.y = 45;
          group.add(mesh);
        } else {
          group.add(buildEquipmentVisual(
            placement,
            template,
            viewMode,
            violated ? "#dc2626" : warehouseTheme ? "#0f766e" : template?.color || "#2563eb"
          ));
        }
        if (!warehouseTheme || violated) {
          addGroupOutlines(group, violated ? 0xdc2626 : placement.is_locked ? 0x166534 : 0x0f172a);
        }
        const position = worldPoint(placement.x_mm, placement.y_mm);
        group.position.set(position.x, 0, position.z);
        group.rotation.y = THREE.MathUtils.degToRad(-placement.rotation_deg);
        if (operationalEntitySelectable(visualTheme, "equipment")) {
          interactive.push(warehouseTheme ? warehousePickProxy(group) || group : group);
        }
        scene.add(group);
        if (layers.labels) {
          const label = textSprite(placement.name, violated ? "#dc2626" : warehouseTheme ? "#5eead4" : placement.is_locked ? "#166534" : "#1d4ed8", 2200, 400, warehouseTheme);
          label.position.set(position.x, viewMode === "25d" ? placement.height_mm + 340 : 150, position.z);
          scene.add(label);
        }
      }
    }

    if (layers.racks) {
      for (const rack of layout.racks) {
        const violated = violationIds.has(rack.id);
        const group = new THREE.Group();
        group.userData = { entityKind: "rack", entityId: rack.id, draggable: !readOnly && (!palletEditingOnly || rackEditingEnabled) && !rack.is_locked };
        group.add(buildRackVisual(rack, viewMode, violated, !readOnly, warehouseTheme));
        if (!warehouseTheme || violated) {
          addGroupOutlines(group, violated ? 0xdc2626 : rack.is_locked ? 0x166534 : 0x4c1d95);
        }
        const position = worldPoint(rack.x_mm, rack.y_mm);
        group.position.set(position.x, 0, position.z);
        group.rotation.y = THREE.MathUtils.degToRad(-rack.rotation_deg);
        if (operationalEntitySelectable(visualTheme, "rack")) {
          if (warehouseTheme) {
            const proxy = warehouseRackPickProxy(rack);
            proxy.userData.entityRoot = group;
            group.add(proxy);
            interactive.push(proxy);
          } else {
            interactive.push(group);
          }
        }
        scene.add(group);
        if (layers.labels) {
          const label = textSprite(rack.rack_code, violated ? "#dc2626" : warehouseTheme ? "#5eead4" : "#4c1d95", 1700, 320, warehouseTheme);
          label.position.set(position.x, viewMode === "25d" ? rack.height_mm + 380 : 150, position.z);
          scene.add(label);
        }
      }
    }

    if (layers.pallets) {
      const draggablePalletIdSet = draggablePalletIds ? new Set(draggablePalletIds) : null;
      const warehousePalletInstances: WarehousePalletInstance[] = [];
      for (const pallet of layout.pallets) {
        const violated = violationIds.has(pallet.id);
        const palletState = palletStatusInfo(pallet.visual_status);
        const group = new THREE.Group();
        group.userData = {
          entityKind: "pallet",
          entityId: pallet.id,
          draggable: !readOnly
            && (!palletEditingOnly || pallet.id.startsWith("erp-location-"))
            && (!draggablePalletIdSet || draggablePalletIdSet.has(pallet.id))
        };
        if (warehouseTheme) {
          if (pallet.is_logical_anchor && shouldShowWarehousePalletVisual(pallet, hideRackLocationMarkers)) {
            group.add(buildPalletMarkerVisual(pallet, viewMode, violated));
          }
          group.add(warehousePalletPickProxy(pallet, viewMode, violated));
        } else {
          group.add(buildPalletVisual(pallet, viewMode, violated));
        }
        if (!warehouseTheme || violated) {
          addGroupOutlines(group, violated ? 0xdc2626 : new THREE.Color(palletState.color).getHex());
        }
        const position = worldPoint(pallet.x_mm, pallet.y_mm);
        group.position.set(position.x, 0, position.z);
        group.rotation.y = THREE.MathUtils.degToRad(-pallet.rotation_deg);
        if (allowPalletSelection || palletEditingOnly || operationalEntitySelectable(visualTheme, "pallet")) {
          interactive.push(warehouseTheme ? group.children[group.children.length - 1] : group);
        }
        scene.add(group);
        if (warehouseTheme && !pallet.is_logical_anchor && shouldShowWarehousePalletVisual(pallet, hideRackLocationMarkers)) {
          warehousePalletInstances.push({ pallet, position, rotationY: group.rotation.y, violated });
        }
        if (layers.labels) {
          const label = textSprite(
            showInternalCodes
              ? `${pallet.pallet_code} · ${palletState.label} · ${pallet.zone_code}`
              : `${pallet.name || "位置名称待完善"} · ${palletState.label}`,
            violated ? "#dc2626" : pallet.candidate_status_color || palletState.color,
            2400,
            340,
            warehouseTheme
          );
          const loadedHeight = pallet.visual_status === "empty" ? pallet.height_mm : pallet.height_mm + 760;
          label.position.set(position.x, viewMode === "25d" ? loadedHeight + 280 : 130, position.z);
          scene.add(label);
        }
      }
      if (warehouseTheme) addWarehousePalletInstances(scene, warehousePalletInstances, viewMode);
    }

    if (layers.production && productionProjections.length) {
      const grouped = new Map<string, ProductionTaskProjection[]>();
      for (const task of productionProjections) {
        const mapping = task.mapping;
        if (!mapping || mapping.target_missing) continue;
        const key = `${mapping.target_kind}:${mapping.target_id}`;
        grouped.set(key, [...(grouped.get(key) || []), task]);
      }
      for (const tasks of grouped.values()) {
        const mapping = tasks[0].mapping!;
        let xMm = 0;
        let yMm = 0;
        let targetHeight = 900;
        if (mapping.target_kind === "pallet") {
          const pallet = layout.pallets.find((item) => item.id === mapping.target_id);
          if (!pallet) continue;
          xMm = pallet.x_mm;
          yMm = pallet.y_mm;
          targetHeight = pallet.height_mm + (pallet.visual_status === "empty" ? 480 : 1250);
        } else {
          const zone = layout.features.find((item) => item.id === mapping.target_id && item.feature_kind === "zone");
          if (!zone || !zone.points.length) continue;
          const center = zone.points.reduce((sum, point) => [sum[0] + point[0], sum[1] + point[1]], [0, 0]);
          xMm = center[0] / zone.points.length;
          yMm = center[1] / zone.points.length;
          targetHeight = zone.storage_mode === "floor" ? 900 : zone.elevation_mm + zone.storage_height_mm + 500;
        }
        const position = worldPoint(xMm, yMm);
        const signal = new THREE.Group();
        if (viewMode === "25d") {
          const beacon = new THREE.Mesh(
            new THREE.CylinderGeometry(95, 150, Math.max(900, targetHeight), 16),
            new THREE.MeshStandardMaterial({ color: 0x06b6d4, emissive: 0x064e5b, transparent: true, opacity: 0.72 })
          );
          beacon.position.y = Math.max(900, targetHeight) / 2;
          signal.add(beacon);
          const halo = new THREE.Mesh(
            new THREE.TorusGeometry(360, 55, 10, 32),
            new THREE.MeshBasicMaterial({ color: 0x5eead4, transparent: true, opacity: 0.9 })
          );
          halo.rotation.x = Math.PI / 2;
          halo.position.y = Math.max(900, targetHeight);
          signal.add(halo);
        } else {
          const ring = new THREE.Mesh(
            new THREE.RingGeometry(280, 480, 32),
            new THREE.MeshBasicMaterial({ color: 0x0891b2, transparent: true, opacity: 0.9, side: THREE.DoubleSide })
          );
          ring.rotation.x = -Math.PI / 2;
          ring.position.y = 180;
          signal.add(ring);
        }
        if (layers.labels) {
          const orderLabel = tasks.length === 1
            ? `${tasks[0].order_number} · ERP只读`
            : `${tasks[0].order_number} 等${tasks.length}项 · ERP只读`;
          const label = textSprite(orderLabel, "#0891b2", 2800, 380, warehouseTheme);
          label.position.y = viewMode === "25d" ? Math.max(1400, targetHeight + 380) : 240;
          signal.add(label);
        }
        signal.position.set(position.x, 0, position.z);
        scene.add(signal);
      }
    }

    if (drawPoints.length) {
      const previewPoints = drawPoints.map(([x, y]) => worldPoint(x, y, 120));
      const preview = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(previewPoints),
        new THREE.LineDashedMaterial({ color: 0x2563eb, dashSize: 250, gapSize: 120 })
      );
      preview.computeLineDistances();
      scene.add(preview);
      for (const [index, point] of previewPoints.entries()) {
        const dot = new THREE.Mesh(new THREE.SphereGeometry(100, 12, 12), new THREE.MeshBasicMaterial({ color: 0x2563eb }));
        dot.position.copy(point);
        scene.add(dot);
        const pointLabel = drawPointLabels[index];
        if (pointLabel) {
          const label = textSprite(pointLabel, "#1d4ed8", 620, 360, warehouseTheme);
          label.position.copy(point);
          label.position.y += 280;
          scene.add(label);
        }
      }
    }

    if (measurePoints.length) {
      const measured = measurePoints.slice(0, 2).map(([x, y]) => worldPoint(x, y, 180));
      if (measured.length === 2) {
        const measureLine = new THREE.Line(
          new THREE.BufferGeometry().setFromPoints(measured),
          new THREE.LineDashedMaterial({ color: 0xf59e0b, dashSize: 220, gapSize: 100 })
        );
        measureLine.computeLineDistances();
        measureLine.renderOrder = 40;
        scene.add(measureLine);
        const value = Math.hypot(
          measurePoints[1][0] - measurePoints[0][0],
          measurePoints[1][1] - measurePoints[0][1]
        );
        const label = textSprite(formatDistanceMm(value), "#92400e", 2400, 440, warehouseTheme);
        label.position.copy(measured[0]).add(measured[1]).multiplyScalar(0.5);
        label.position.y += 260;
        scene.add(label);
      }
      for (const point of measured) {
        const dot = new THREE.Mesh(
          new THREE.SphereGeometry(110, 14, 14),
          new THREE.MeshBasicMaterial({ color: 0xf59e0b })
        );
        dot.position.copy(point);
        scene.add(dot);
      }
    }

    const raycaster = new THREE.Raycaster();
    raycaster.layers.set(warehouseTheme ? WAREHOUSE_PICK_LAYER : 0);
    raycaster.params.Line.threshold = 180;
    const pointer = new THREE.Vector2();
    const ground = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const snapGuideGroup = new THREE.Group();
    scene.add(snapGuideGroup);
    const clearSnapGuides = () => {
      for (const child of [...snapGuideGroup.children]) {
        snapGuideGroup.remove(child);
        if (child instanceof THREE.Line) {
          child.geometry.dispose();
          (child.material as THREE.Material).dispose();
        }
      }
      requestRender();
    };
    const showSnapGuides = (guides: { axis: "x" | "y"; value: number }[]) => {
      clearSnapGuides();
      for (const guide of guides) {
        const points = guide.axis === "x"
          ? [worldPoint(guide.value, bounds.min_y, 170), worldPoint(guide.value, bounds.max_y, 170)]
          : [worldPoint(bounds.min_x, guide.value, 170), worldPoint(bounds.max_x, guide.value, 170)];
        const line = new THREE.Line(
          new THREE.BufferGeometry().setFromPoints(points),
          new THREE.LineDashedMaterial({ color: 0x06b6d4, dashSize: 260, gapSize: 130, transparent: true, opacity: 0.9 })
        );
        line.computeLineDistances();
        line.renderOrder = 60;
        snapGuideGroup.add(line);
      }
      requestRender();
    };
    let dragging: {
      kind: "equipment" | "rack" | "pallet" | "feature";
      id: string;
      object: THREE.Object3D;
      startGround: THREE.Vector3;
      startPosition: THREE.Vector3;
      startClientX: number;
      startClientY: number;
      moved: boolean;
    } | null = null;
    let pendingSelection: {
      entity: { kind: "equipment" | "rack" | "pallet" | "feature" | "structure"; id: string };
      pointerId: number;
      startX: number;
      startY: number;
      moved: boolean;
    } | null = null;
    let pendingCanvasAction: {
      kind: "draw" | "measure" | "clear-selection";
      pointerId: number;
      startX: number;
      startY: number;
      moved: boolean;
    } | null = null;
    const setPointer = (event: { clientX: number; clientY: number }) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
    };
    const intersectGround = () => {
      const point = new THREE.Vector3();
      return raycaster.ray.intersectPlane(ground, point) ? point : null;
    };
    const onPointerDown = (event: PointerEvent) => {
      setPointer(event);
      if (event.button !== 0) return;
      if (measureMode || effectiveDrawMode) {
        pendingCanvasAction = {
          kind: measureMode ? "measure" : "draw",
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          moved: false
        };
        return;
      }
      const roots = raycaster.intersectObjects(interactive, !warehouseTheme)
        .map((intersection) => entityNode(intersection.object))
        .filter((candidate): candidate is THREE.Object3D => Boolean(candidate));
      const preferredPlanningPallet = palletEditingOnly
        ? roots.find((candidate) => candidate.userData.entityKind === "pallet" && candidate.userData.draggable)
        : null;
      const preferredPlanningFeature = featureEditingEnabled && !rackEditingEnabled
        ? roots.find((candidate) => candidate.userData.entityKind === "feature" && candidate.userData.draggable)
        : null;
      const preferredStorage = preferStorageSelection
        ? roots.find(candidate => candidate.userData.entityKind === "rack") || roots.find(candidate => candidate.userData.entityKind === "pallet")
        : null;
      const root = preferredPlanningPallet || preferredPlanningFeature || preferredStorage || roots[0] || null;
      if (!root) {
        pendingCanvasAction = {
          kind: "clear-selection",
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          moved: false
        };
        return;
      }
      const kind = root.userData.entityKind as "equipment" | "rack" | "pallet" | "feature" | "structure";
      const id = String(root.userData.entityId);
      pendingSelection = {
        entity: { kind, id },
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        moved: false
      };
      if (viewMode === "25d") {
        return;
      }
      if (
        viewMode === "2d"
        && ((kind === "equipment" || kind === "rack" || kind === "feature") || kind === "pallet")
        && root.userData.draggable
      ) {
        const point = intersectGround();
        if (!point) return;
        dragging = {
          kind,
          id,
          object: root,
          startGround: point.clone(),
          startPosition: root.position.clone(),
          startClientX: event.clientX,
          startClientY: event.clientY,
          moved: false
        };
        controls.enabled = false;
        renderer.domElement.setPointerCapture(event.pointerId);
      }
    };
    const onContextMenu = (event: MouseEvent) => {
      if (handlersRef.current.onEntityContextMenu) {
        setPointer(event);
        const root = raycaster.intersectObjects(interactive, !warehouseTheme)
          .map((intersection) => entityNode(intersection.object))
          .find((candidate) => Boolean(candidate));
        if (!root) return;
        const entity = { kind: root.userData.entityKind, id: String(root.userData.entityId) } as NonNullable<SelectedEntity>;
        if (handlersRef.current.onEntityContextMenu(entity, event.clientX, event.clientY)) {
          event.preventDefault();
          renderer.domElement.tabIndex = 0;
          renderer.domElement.focus({ preventScroll: true });
        }
        return;
      }
      if (!featureEditingEnabled || !handlersRef.current.onFeatureContextMenu) return;
      setPointer(event);
      const featureRoot = raycaster.intersectObjects(interactive, !warehouseTheme)
        .map((intersection) => entityNode(intersection.object))
        .find((candidate) => candidate?.userData.entityKind === "feature");
      if (!featureRoot) return;
      event.preventDefault();
      const id = String(featureRoot.userData.entityId);
      handlersRef.current.onSelect({ kind: "feature", id });
      handlersRef.current.onFeatureContextMenu(id, event.clientX, event.clientY);
    };
    const processPointerMove = (event: { clientX: number; clientY: number; altKey?: boolean }) => {
      if (pendingCanvasAction && Math.hypot(event.clientX - pendingCanvasAction.startX, event.clientY - pendingCanvasAction.startY) > 4) {
        pendingCanvasAction.moved = true;
      }
      if (pendingSelection && Math.hypot(event.clientX - pendingSelection.startX, event.clientY - pendingSelection.startY) > 4) {
        pendingSelection.moved = true;
      }
      setPointer(event);
      const hoverPoint = intersectGround();
      if (hoverPoint && coordinateRef.current) {
        coordinateRef.current.textContent = `X ${Math.round(hoverPoint.x + centerX).toLocaleString("zh-CN")} · Y ${Math.round(centerY - hoverPoint.z).toLocaleString("zh-CN")} mm`;
      }
      if (!dragging) return;
      if (Math.hypot(event.clientX - dragging.startClientX, event.clientY - dragging.startClientY) > 4) {
        dragging.moved = true;
      }
      if (!dragging.moved) return;
      const point = intersectGround();
      if (point) {
        const proposed = dragging.startPosition.clone().add(point.clone().sub(dragging.startGround));
        if (dragging.kind === "pallet") {
          const pallet = layout.pallets.find((item) => item.id === dragging!.id);
          if (pallet) {
            const result = snapPalletPosition(
              pallet,
              Math.round(proposed.x + centerX),
              Math.round(centerY - proposed.z),
              layout.pallets,
              layout.features,
              palletSnapThresholdMm,
              palletSnapEnabled
            );
            const snapped = worldPoint(result.x, result.y);
            dragging.object.position.set(snapped.x, dragging.startPosition.y, snapped.z);
            showSnapGuides(result.guides);
          }
        } else if (dragging.kind === "rack") {
          const rack = layout.racks.find(item => item.id === dragging!.id);
          if (rack) {
            const result = snapRackPosition(rack, proposed.x + centerX, centerY - proposed.z, layout.racks, 120, !event.altKey);
            const snapped = worldPoint(result.x, result.y);
            dragging.object.position.set(snapped.x, dragging.startPosition.y, snapped.z);
            showSnapGuides(result.guides);
          }
        } else {
          dragging.object.position.copy(proposed);
        }
        requestRender();
      }
    };
    let pointerMoveFrame: number | null = null;
    let latestPointerMove: { clientX: number; clientY: number; altKey: boolean } | null = null;
    const onPointerMove = (event: PointerEvent) => {
      latestPointerMove = { clientX: event.clientX, clientY: event.clientY, altKey: event.altKey };
      if (pointerMoveFrame !== null) return;
      pointerMoveFrame = requestAnimationFrame(() => {
        pointerMoveFrame = null;
        const latest = latestPointerMove;
        latestPointerMove = null;
        if (latest) processPointerMove(latest);
      });
    };
    const flushPointerMove = (event: PointerEvent) => {
      if (pointerMoveFrame !== null) cancelAnimationFrame(pointerMoveFrame);
      pointerMoveFrame = null;
      latestPointerMove = null;
      processPointerMove(event);
    };
    const onPointerUp = (event: PointerEvent) => {
      flushPointerMove(event);
      if (pendingCanvasAction && pendingCanvasAction.pointerId === event.pointerId) {
        const currentAction = pendingCanvasAction;
        pendingCanvasAction = null;
        if (!currentAction.moved) {
          setPointer(event);
          if (currentAction.kind === "clear-selection") {
            handlersRef.current.onSelect(null);
          } else {
            const point = intersectGround();
            if (point) {
              const xMm = Math.round(point.x + centerX);
              const yMm = Math.round(centerY - point.z);
              if (currentAction.kind === "draw") handlersRef.current.onDrawPoint(xMm, yMm);
              else handlersRef.current.onMeasurePoint(xMm, yMm);
            }
          }
        }
      }
      if (pendingSelection && pendingSelection.pointerId === event.pointerId) {
        const currentSelection = pendingSelection;
        pendingSelection = null;
        if (!currentSelection.moved) handlersRef.current.onSelect(currentSelection.entity);
      }
      if (!dragging) return;
      const current = dragging;
      dragging = null;
      clearSnapGuides();
      controls.enabled = true;
      if (renderer.domElement.hasPointerCapture(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
      if (!current.moved) {
        current.object.position.copy(current.startPosition);
        requestRender();
        return;
      }
      // Selecting on pointer-down rebuilt the React/Three scene and erased the
      // in-progress drag. Selection is intentionally deferred until release.
      handlersRef.current.onSelect({ kind: current.kind, id: current.id });
      if (current.kind === "feature") {
        const deltaXmm = Math.round(current.object.position.x - current.startPosition.x);
        const deltaYmm = Math.round(-(current.object.position.z - current.startPosition.z));
        handlersRef.current.onMoveFeature(current.id, deltaXmm, deltaYmm);
        return;
      }
      const xMm = Math.round(current.object.position.x + centerX);
      const yMm = Math.round(centerY - current.object.position.z);
      if (current.kind === "equipment") handlersRef.current.onMoveEquipment(current.id, xMm, yMm);
      else if (current.kind === "rack") handlersRef.current.onMoveRack(current.id, xMm, yMm);
      else {
        // ERP pallet/location drags are controlled page-draft gestures. Restore
        // the rendered object before dispatching the drop coordinates so an
        // invalid target can never look like a completed inventory move.
        current.object.position.copy(current.startPosition);
        requestRender();
        handlersRef.current.onMovePallet(current.id, xMm, yMm);
      }
    };
    const onPointerCancel = (event: PointerEvent) => {
      if (pointerMoveFrame !== null) cancelAnimationFrame(pointerMoveFrame);
      pointerMoveFrame = null;
      latestPointerMove = null;
      pendingCanvasAction = null;
      pendingSelection = null;
      if (!dragging) return;
      dragging.object.position.copy(dragging.startPosition);
      clearSnapGuides();
      dragging = null;
      controls.enabled = true;
      requestRender();
      if (renderer.domElement.hasPointerCapture(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
    };
    const onDragOver = (event: DragEvent) => event.preventDefault();
    const onDrop = (event: DragEvent) => {
      event.preventDefault();
      setPointer(event);
      const point = intersectGround();
      if (!point) return;
      const xMm = Math.round(point.x + centerX);
      const yMm = Math.round(centerY - point.z);
      const templateId = event.dataTransfer?.getData("application/x-twin-asset");
      const rackJson = event.dataTransfer?.getData("application/x-twin-rack");
      const palletJson = event.dataTransfer?.getData("application/x-twin-pallet");
      if (templateId) handlersRef.current.onDropAsset(templateId, xMm, yMm);
      else if (rackJson) handlersRef.current.onDropRack(JSON.parse(rackJson), xMm, yMm);
      else if (palletJson) handlersRef.current.onDropPallet(JSON.parse(palletJson), xMm, yMm);
    };
    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("contextmenu", onContextMenu);
    renderer.domElement.addEventListener("pointermove", onPointerMove);
    renderer.domElement.addEventListener("pointerup", onPointerUp);
    renderer.domElement.addEventListener("pointercancel", onPointerCancel);
    if (!readOnly) {
      renderer.domElement.addEventListener("dragover", onDragOver);
      renderer.domElement.addEventListener("drop", onDrop);
    }
    const entityNodes = new Map<string, THREE.Object3D>();
    scene.traverse((object) => {
      if (object.userData.entityKind && object.userData.entityId) {
        entityNodes.set(`${object.userData.entityKind}:${object.userData.entityId}`, object);
      }
    });
    const selectionHighlight = new THREE.Group();
    const searchHighlight = new THREE.Group();
    const resultHighlight = new THREE.Group();
    const productQuantityGroup = new THREE.Group();
    scene.add(selectionHighlight, searchHighlight, resultHighlight, productQuantityGroup);
    const runtime: CanvasRuntime = {
      scene,
      camera,
      controls,
      entityNodes,
      selectionHighlight,
      searchHighlight,
      resultHighlight,
      productQuantityGroup,
      focusFrame: null,
      requestRender,
      viewMode,
      layoutId: layout.id
    };
    runtimeRef.current = runtime;
    syncProductQuantities(runtime, productQuantityLabels);
    syncEntityHighlights(runtime, selectedRef.current, focusTargetRef.current, moveStatesRef.current);
    syncResultHighlights(runtime, highlightFeatureIds, highlightedPalletIds, mergeTargetPalletId, moveLocationStates, selectedAreaFeatureId, sourcePalletIds);
    if (focusTargetRef.current) {
      const nextFocusKey = `${focusTargetRef.current.token}:${layout.id}:${viewMode}`;
      if (lastFocusKeyRef.current !== nextFocusKey && animateFocus(runtime, focusTargetRef.current)) {
        lastFocusKeyRef.current = nextFocusKey;
      }
    }
    const saveCameraState = () => {
      cameraStateRef.current = {
        position: camera.position.toArray() as [number, number, number],
        target: controls.target.toArray() as [number, number, number],
        zoom: camera.zoom
      };
      updateOverlay();
    };
    const onControlsChange = () => {
      saveCameraState();
      requestRender();
    };
    controls.addEventListener("change", onControlsChange);
    saveCameraState();
    updateOverlay();
    requestRender();
    const resizeObserver = new ResizeObserver(() => {
      const nextWidth = Math.max(container.clientWidth, 1);
      const nextHeight = Math.max(container.clientHeight, 420);
      camera.left = (-span * nextWidth) / nextHeight / frustumDivisor;
      camera.right = (span * nextWidth) / nextHeight / frustumDivisor;
      camera.updateProjectionMatrix();
      renderer.setSize(nextWidth, nextHeight);
      updateOverlay();
      requestRender();
    });
    resizeObserver.observe(container);
    return () => {
      disposed = true;
      if (renderFrame !== null) cancelAnimationFrame(renderFrame);
      if (pointerMoveFrame !== null) cancelAnimationFrame(pointerMoveFrame);
      resizeObserver.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      renderer.domElement.removeEventListener("pointerup", onPointerUp);
      renderer.domElement.removeEventListener("pointercancel", onPointerCancel);
      renderer.domElement.removeEventListener("contextmenu", onContextMenu);
      if (!readOnly) {
        renderer.domElement.removeEventListener("dragover", onDragOver);
        renderer.domElement.removeEventListener("drop", onDrop);
      }
      controls.removeEventListener("change", onControlsChange);
      if (runtime.focusFrame !== null) cancelAnimationFrame(runtime.focusFrame);
      clearHighlightGroup(selectionHighlight);
      clearHighlightGroup(searchHighlight);
      clearHighlightGroup(resultHighlight);
      if (runtimeRef.current === runtime) runtimeRef.current = null;
      controls.dispose();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.Line || object instanceof THREE.LineSegments) {
          object.geometry.dispose();
          const materials = Array.isArray(object.material) ? object.material : [object.material];
          materials.forEach((material) => {
            if ("map" in material && material.map instanceof THREE.Texture) material.map.dispose();
            material.dispose();
          });
        }
      });
      renderer.dispose();
    };
  }, [layout, assets, viewMode, cameraPreset, viewResetToken, layers, referenceLayout, referenceOverlay, productionProjections, palletEditingOnly, rackEditingEnabled, featureEditingEnabled, aisleEditingEnabled, mapPanLocked, allowPalletSelection, preferStorageSelection, hideRackLocationMarkers, draggablePalletIds, palletSnapEnabled, palletSnapThresholdMm, effectiveDrawMode, drawPoints, drawPointLabels, measureMode, measurePoints, readOnly, visualTheme, showInternalCodes]);

  useEffect(() => {
    const runtime = runtimeRef.current;
    if (!runtime) return;
    syncEntityHighlights(runtime, selected, focusTarget, moveLocationStates);
    if (!focusTarget) return;
    const nextFocusKey = `${focusTarget.token}:${runtime.layoutId}:${runtime.viewMode}`;
    if (lastFocusKeyRef.current !== nextFocusKey && animateFocus(runtime, focusTarget)) {
      lastFocusKeyRef.current = nextFocusKey;
    }
  }, [selected, focusTarget, moveLocationStates]);

  useEffect(() => {
    const runtime = runtimeRef.current;
    if (!runtime) return;
    syncResultHighlights(runtime, highlightFeatureIds, highlightedPalletIds, mergeTargetPalletId, moveLocationStates, selectedAreaFeatureId, sourcePalletIds);
  }, [highlightFeatureIds, highlightedPalletIds, mergeTargetPalletId, moveLocationStates, selectedAreaFeatureId, sourcePalletIds]);

  const realEastCompass = usesRealEastCompass(layout);
  useEffect(() => {
    if (runtimeRef.current) syncProductQuantities(runtimeRef.current, productQuantityLabels);
  }, [productQuantityLabels]);
  const floor4CalibratingCompass = layout.floor_code.toUpperCase() === "4F" && calibrationMode;
  const compassCode = floor4CalibratingCompass ? "3F" : realEastCompass ? "E" : "N";
  const compassLabel = floor4CalibratingCompass ? "对齐3F" : realEastCompass ? "现实东向" : "图纸北向";
  return <div className={`editor-canvas ${visualTheme === "warehouse" ? "warehouse-theme" : ""} ${effectiveDrawMode || measureMode ? "drawing" : ""}`} ref={containerRef}>
    <div className="canvas-mount" ref={canvasMountRef} />
    {!readOnly && showInternalCodes && viewMode === "2d" && <small className="map-edit-keyboard-hint">{MAP_KEYBOARD_HINT}。货架拖近120mm内吸附，Alt取消吸附；调整后按原流程保存/应用。</small>}
    <div className="map-compass" aria-label={compassLabel}><span ref={northArrowRef}>↑</span><b>{compassCode}</b><small>{compassLabel}</small></div>
    {referenceLayout && referenceOverlay?.enabled && layout.floor_code.toUpperCase()==="1F" && <div className="reference-overlay-badge">{referenceOverlay.shared_coordinates ? "3F 左半区柱墙 · 同坐标复核" : "3F 左半区柱墙参照 · 草稿"}</div>}
    <div className="map-scale"><span ref={scaleBarRef} /><b ref={scaleLabelRef}>—</b></div>
    <small className="map-coordinate" hidden={!showInternalCodes} ref={coordinateRef}>X — · Y — mm</small>
  </div>;
}
