import * as THREE from "three";
import { accessDirectionVectors, classifyEquipment } from "./factoryVisuals.mjs";
import { palletStatusInfo } from "./palletStatus";
import type { AssetTemplate, Pallet, Placement, Rack, ViewMode } from "./types";

function finishMaterial(color: string | number, opacity = 1) {
  return new THREE.MeshStandardMaterial({
    color: new THREE.Color(color),
    roughness: 0.72,
    metalness: 0.08,
    transparent: opacity < 1,
    opacity
  });
}

function addBox(
  group: THREE.Group,
  size: [number, number, number],
  position: [number, number, number],
  color: string | number,
  opacity = 1
) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(...size), finishMaterial(color, opacity));
  mesh.position.set(...position);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  group.add(mesh);
  return mesh;
}

function addCylinder(
  group: THREE.Group,
  radius: number,
  length: number,
  position: [number, number, number],
  color: string | number
) {
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, length, 18),
    finishMaterial(color)
  );
  mesh.rotation.z = Math.PI / 2;
  mesh.position.set(...position);
  mesh.castShadow = true;
  group.add(mesh);
  return mesh;
}

export function createGroundArrow(length: number, color: string | number) {
  const safeLength = Math.max(length, 300);
  const halfWidth = Math.max(safeLength * 0.12, 80);
  const shape = new THREE.Shape();
  shape.moveTo(0, -halfWidth * 0.28);
  shape.lineTo(safeLength * 0.66, -halfWidth * 0.28);
  shape.lineTo(safeLength * 0.66, -halfWidth);
  shape.lineTo(safeLength, 0);
  shape.lineTo(safeLength * 0.66, halfWidth);
  shape.lineTo(safeLength * 0.66, halfWidth * 0.28);
  shape.lineTo(0, halfWidth * 0.28);
  shape.closePath();
  const mesh = new THREE.Mesh(
    new THREE.ShapeGeometry(shape),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9, side: THREE.DoubleSide })
  );
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = 34;
  return mesh;
}

export function addGroupOutlines(group: THREE.Group, color: string | number) {
  const meshes: THREE.Mesh[] = [];
  group.traverse((object) => {
    if (object instanceof THREE.Mesh) meshes.push(object);
  });
  for (const mesh of meshes) {
    const outline = new THREE.LineSegments(
      new THREE.EdgesGeometry(mesh.geometry),
      new THREE.LineBasicMaterial({ color })
    );
    outline.renderOrder = 12;
    mesh.add(outline);
  }
}

export function createHatchTexture(color: string, crossed = false) {
  const canvas = document.createElement("canvas");
  canvas.width = 64;
  canvas.height = 64;
  const context = canvas.getContext("2d")!;
  context.clearRect(0, 0, 64, 64);
  context.strokeStyle = color;
  context.globalAlpha = 0.72;
  context.lineWidth = 8;
  context.beginPath();
  context.moveTo(-16, 64);
  context.lineTo(64, -16);
  context.moveTo(16, 80);
  context.lineTo(80, 16);
  if (crossed) {
    context.moveTo(-16, 0);
    context.lineTo(64, 80);
    context.moveTo(16, -16);
    context.lineTo(80, 48);
  }
  context.stroke();
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(1 / 1200, 1 / 1200);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

export function buildEquipmentVisual(
  placement: Placement,
  template: AssetTemplate | undefined,
  viewMode: ViewMode,
  displayColor: string
) {
  const group = new THREE.Group();
  const width = Math.max(placement.width_mm, 300);
  const depth = Math.max(placement.depth_mm, 300);
  const height = Math.max(placement.height_mm, 300);
  const mainColor = displayColor;
  const dark = "#334155";
  const metal = "#94a3b8";
  const safety = "#f59e0b";

  if (viewMode === "2d") {
    addBox(group, [width, 70, depth], [0, 35, 0], mainColor, 0.88);
    addBox(group, [Math.min(width * 0.34, 1200), 24, 90], [0, 84, depth / 2 - 45], safety);
    return group;
  }

  const kind = classifyEquipment(placement.name, template?.category || "");
  addBox(group, [width * 0.92, Math.max(height * 0.08, 120), depth * 0.9], [0, Math.max(height * 0.04, 60), 0], dark);

  if (kind === "printing") {
    addBox(group, [width * 0.62, height * 0.66, depth * 0.78], [0, height * 0.39, 0], mainColor);
    addBox(group, [width * 0.17, height * 0.16, depth * 0.72], [-width * 0.4, height * 0.18, 0], metal);
    addBox(group, [width * 0.17, height * 0.16, depth * 0.72], [width * 0.4, height * 0.18, 0], metal);
    addCylinder(group, depth * 0.12, width * 0.46, [0, height * 0.76, 0], dark);
  } else if (kind === "die_cutter") {
    addBox(group, [width * 0.58, height * 0.82, depth * 0.82], [width * 0.16, height * 0.45, 0], mainColor);
    addBox(group, [width * 0.34, height * 0.08, depth * 0.72], [-width * 0.34, height * 0.34, 0], metal);
    addBox(group, [width * 0.24, height * 0.34, depth * 0.22], [width * 0.31, height * 0.74, -depth * 0.28], dark);
    addBox(group, [width * 0.12, height * 0.2, depth * 0.12], [-width * 0.12, height * 0.76, -depth * 0.36], safety);
  } else if (kind === "forming") {
    addBox(group, [width * 0.86, height * 0.3, depth * 0.5], [0, height * 0.2, 0], mainColor);
    addBox(group, [width * 0.72, height * 0.1, depth * 0.15], [0, height * 0.62, 0], metal);
    addBox(group, [width * 0.05, height * 0.55, depth * 0.12], [-width * 0.3, height * 0.42, 0], dark);
    addBox(group, [width * 0.05, height * 0.55, depth * 0.12], [width * 0.3, height * 0.42, 0], dark);
  } else if (kind === "conveyor") {
    addBox(group, [width * 0.92, height * 0.18, depth * 0.82], [0, height * 0.45, 0], metal);
    const rollerCount = 7;
    for (let index = 0; index < rollerCount; index += 1) {
      const x = -width * 0.38 + (width * 0.76 * index) / (rollerCount - 1);
      const roller = addCylinder(group, Math.max(width * 0.018, 35), depth * 0.72, [x, height * 0.58, 0], dark);
      roller.rotation.z = 0;
      roller.rotation.x = Math.PI / 2;
    }
  } else {
    addBox(group, [width * 0.72, height * 0.72, depth * 0.74], [-width * 0.06, height * 0.42, 0], mainColor);
    addBox(group, [width * 0.18, height * 0.48, depth * 0.3], [width * 0.36, height * 0.3, -depth * 0.18], dark);
    addBox(group, [width * 0.34, height * 0.09, depth * 0.42], [-width * 0.24, height * 0.83, 0], metal);
  }
  return group;
}

export function buildRackVisual(
  rack: Rack,
  viewMode: ViewMode,
  violated: boolean,
  showCandidateCargo = true,
  warehouseTheme = false
) {
  const group = new THREE.Group();
  const width = Math.max(rack.width_mm, 400);
  const depth = Math.max(rack.depth_mm, 300);
  const height = Math.max(rack.height_mm, 500);
  const levels = Math.max(rack.levels, 1);
  const configuredHeights = (rack.level_heights_mm || []).filter((value) => value > 0 && value < height);
  const shelfHeights = configuredHeights.length === Math.max(0, levels - 1)
    ? configuredHeights
    : Array.from({ length: Math.max(0, levels - 1) }, (_, index) => (height * (index + 1)) / levels);
  const cargoRows = Math.min(5, Math.max(3, rack.cargo_rows || 4));
  const bays = Math.max(rack.bays, 1);
  const post = Math.min(Math.max(Math.min(width / bays, depth) * 0.055, 45), 100);
  const frameColor = violated ? "#dc2626" : warehouseTheme ? "#38bdf8" : "#1d4ed8";
  const beamColor = violated ? "#dc2626" : warehouseTheme ? "#5eead4" : "#c2410c";

  if (viewMode === "2d") {
    addBox(group, [width, 80, depth], [0, 40, 0], violated ? "#fecaca" : warehouseTheme ? "#0e7490" : "#dbeafe", warehouseTheme ? 0.58 : 0.88);
    for (let bay = 1; bay < bays; bay += 1) {
      addBox(group, [22, 92, depth], [-width / 2 + (width * bay) / bays, 46, 0], frameColor);
    }
  } else {
    for (let bay = 0; bay <= bays; bay += 1) {
      const x = -width / 2 + (width * bay) / bays;
      addBox(group, [post, height, post], [x, height / 2, -depth / 2 + post / 2], frameColor);
      addBox(group, [post, height, post], [x, height / 2, depth / 2 - post / 2], frameColor);
    }
    for (const y of shelfHeights) {
      addBox(group, [width, post, post], [0, y, -depth / 2 + post / 2], beamColor);
      addBox(group, [width, post, post], [0, y, depth / 2 - post / 2], beamColor);
      addBox(group, [width - post, 22, depth - post], [0, y + post / 2, 0], warehouseTheme ? "#164e63" : "#94a3b8", 0.58);
    }
    addBox(group, [width, post, post], [0, height, -depth / 2 + post / 2], beamColor);
    addBox(group, [width, post, post], [0, height, depth / 2 - post / 2], beamColor);
    if (showCandidateCargo) {
      // Ground pallet + upper pallet loads remain available in planning mode.
      // Candidate cargo is only for the editable planning scene. The ERP twin
      // hides it because formal inventory is currently mapped to areas, not to
      // an exact rack level or bay.
      const cargoRowWidth = width / cargoRows;
      const tierBases = [0, ...shelfHeights];
      for (let row = 0; row < cargoRows; row += 1) {
        const x = -width / 2 + cargoRowWidth * (row + 0.5);
        const palletWidth = Math.max(cargoRowWidth * 0.76, 180);
        const palletDepth = Math.max(depth * 0.72, 220);
        for (let tier = 0; tier < tierBases.length; tier += 1) {
          const base = tierBases[tier];
          const ceiling = tier + 1 < tierBases.length ? tierBases[tier + 1] : height;
          const baseY = tier === 0 ? 65 : base + post;
          const cargoHeight = Math.max(Math.min((ceiling - base) * 0.5, 620), 160);
          addBox(group, [palletWidth, 70, palletDepth], [x, baseY, 0], "#92400e");
          addBox(group, [palletWidth * 0.86, cargoHeight, palletDepth * 0.82], [x, baseY + 35 + cargoHeight / 2, 0], tier % 2 ? "#d6a55c" : "#c98a3a", 0.9);
        }
      }
    }
  }

  // Four inset rails distinguish racks without enlarging their physical footprint.
  const border = 35;
  const borderY = viewMode === "2d" ? 110 : height + post;
  for (const sign of [-1, 1]) {
    addBox(group, [width, 18, border], [0, borderY, sign * (depth - border) / 2], "#f97316");
    addBox(group, [border, 18, depth - border * 2], [sign * (width - border) / 2, borderY, 0], "#f97316");
  }
  if (!warehouseTheme) {
    for (const [dx, dz] of accessDirectionVectors(rack.access_side)) {
      const arrow = createGroundArrow(Math.min(Math.max(depth * 0.55, 500), 1200), beamColor);
      const outward = Math.max(depth * 0.68, 500);
      arrow.position.x = Number(dx) * outward;
      arrow.position.z = Number(dz) * outward;
      arrow.rotation.y = Math.atan2(Number(dz), Number(dx));
      group.add(arrow);
    }
  }
  return group;
}

export function buildPalletVisual(pallet: Pallet, viewMode: ViewMode, violated: boolean) {
  const group = new THREE.Group();
  const minimumFootprint = pallet.is_logical_anchor ? 180 : 400;
  const width = Math.max(pallet.width_mm, minimumFootprint);
  const depth = Math.max(pallet.depth_mm, minimumFootprint);
  const height = Math.max(pallet.height_mm, 90);
  const wood = violated ? "#dc2626" : pallet.color || "#b7793f";
  const darkWood = violated ? "#991b1b" : "#75431f";
  const state = palletStatusInfo(pallet.visual_status);
  const statusColor = violated ? "#dc2626" : pallet.candidate_status_color || state.color;
  const deckHeight = viewMode === "2d" ? 32 : Math.max(height * 0.22, 28);
  const topY = viewMode === "2d" ? 46 : height - deckHeight / 2;
  const slatCount = 7;
  const slatWidth = width / (slatCount + 1.3);
  for (let index = 0; index < slatCount; index += 1) {
    const x = -width / 2 + (width * (index + 1)) / (slatCount + 1);
    addBox(group, [slatWidth, deckHeight, depth], [x, topY, 0], index % 2 ? wood : "#c98a52");
  }
  if (viewMode === "25d") {
    const runnerHeight = Math.max(height * 0.34, 42);
    for (const x of [-width * 0.38, 0, width * 0.38]) {
      addBox(group, [Math.max(width * 0.1, 90), runnerHeight, depth * 0.92], [x, runnerHeight / 2, 0], darkWood);
      for (const z of [-depth * 0.38, 0, depth * 0.38]) {
        addBox(group, [Math.max(width * 0.16, 120), Math.max(height * 0.44, 48), Math.max(depth * 0.16, 110)], [x, height * 0.44, z], darkWood);
      }
    }
  } else {
    addBox(group, [width * 0.96, 14, 30], [0, 68, -depth * 0.34], darkWood);
    addBox(group, [width * 0.96, 14, 30], [0, 68, depth * 0.34], darkWood);
  }
  const hasTurnoverLoad = pallet.visual_status !== "empty";
  if (viewMode === "25d" && hasTurnoverLoad) {
    const cargoHeight = Math.max(360, Math.min(720, depth * 0.58));
    const boxWidth = width * 0.42;
    const boxDepth = depth * 0.4;
    for (const x of [-width * 0.23, width * 0.23]) {
      for (const z of [-depth * 0.22, depth * 0.22]) {
        addBox(
          group,
          [boxWidth, cargoHeight, boxDepth],
          [x, height + cargoHeight / 2, z],
          statusColor,
          pallet.is_simulated ? 0.72 : 0.88
        );
      }
    }
    addBox(group, [width * 0.9, 34, depth * 0.88], [0, height + cargoHeight + 22, 0], statusColor);
  } else {
    addBox(
      group,
      [width * 0.9, viewMode === "2d" ? 24 : 32, depth * 0.86],
      [0, viewMode === "2d" ? 84 : height + 20, 0],
      statusColor,
      hasTurnoverLoad ? 0.62 : 0.28
    );
  }
  return group;
}

export function palletMarkerSpec(pallet: Pallet, viewMode: ViewMode, violated: boolean) {
  const planningSlot = Boolean(pallet.is_planning_location_slot);
  const minimumFootprint = pallet.is_logical_anchor && !planningSlot ? 180 : 400;
  const width = Math.max(planningSlot ? Number(pallet.planning_slot_width_mm || 0) : pallet.width_mm, minimumFootprint);
  const depth = Math.max(planningSlot ? Number(pallet.planning_slot_depth_mm || 0) : pallet.depth_mm, minimumFootprint);
  const state = palletStatusInfo(pallet.visual_status);
  const statusColor = violated ? "#dc2626" : pallet.candidate_status_color || state.color;
  const baseColor = violated ? "#991b1b" : pallet.color || "#9a6a3a";
  const baseHeight = viewMode === "2d" ? 32 : 80;
  const loadHeight = viewMode === "2d" ? 26 : pallet.visual_status === "empty" ? 70 : 420;
  const loadY = viewMode === "2d" ? 66 : pallet.visual_status === "empty" ? 105 : 290;

  return {
    width,
    depth,
    baseColor,
    baseHeight,
    baseY: viewMode === "2d" ? 30 : 40,
    loadColor: statusColor,
    loadHeight,
    loadY,
    loadWidth: width * 0.88,
    loadDepth: depth * 0.84,
    pickHeight: Math.max(baseHeight + 20, loadY + loadHeight / 2) * 2
  };
}

export function buildPalletMarkerVisual(pallet: Pallet, viewMode: ViewMode, violated: boolean) {
  const group = new THREE.Group();
  const spec = palletMarkerSpec(pallet, viewMode, violated);

  if (pallet.is_logical_anchor) {
    const markerColor = violated ? 0xdc2626 : new THREE.Color(spec.loadColor).getHex();
    if (pallet.is_planning_location_slot) {
      const height = viewMode === "2d" ? 28 : 48;
      const geometry = new THREE.BoxGeometry(spec.width, height, spec.depth);
      const fill = new THREE.Mesh(
        geometry,
        new THREE.MeshBasicMaterial({ color: markerColor, transparent: true, opacity: 0.9 })
      );
      fill.position.y = height / 2;
      const outline = new THREE.LineSegments(
        new THREE.EdgesGeometry(geometry),
        new THREE.LineBasicMaterial({ color: violated ? 0xdc2626 : pallet.visual_status === "empty" ? 0x9b927d : markerColor, transparent: true, opacity: 0.95 })
      );
      outline.position.copy(fill.position);
      // Flat planning outlines must remain visible even under overlapping
      // translucent zones/equipment. This changes display only, not collision gates.
      if (viewMode === "2d") {
        fill.material.depthTest = false;
        fill.material.depthWrite = false;
        fill.material.opacity = 0.35;
        fill.renderOrder = 35;
        outline.material.depthTest = false;
        outline.material.depthWrite = false;
        outline.renderOrder = 36;
      }
      group.add(fill, outline);
      return group;
    }
    const diameter = Math.min(Math.max(spec.width, spec.depth, 180), 260);
    const base = new THREE.Mesh(
      new THREE.CylinderGeometry(diameter / 2, diameter / 2, viewMode === "2d" ? 28 : 42, 24),
      new THREE.MeshBasicMaterial({ color: markerColor, transparent: true, opacity: 0.82 })
    );
    base.position.y = viewMode === "2d" ? 14 : 21;
    const stem = new THREE.Mesh(
      new THREE.CylinderGeometry(18, 18, viewMode === "2d" ? 70 : 180, 12),
      new THREE.MeshBasicMaterial({ color: markerColor, transparent: true, opacity: 0.9 })
    );
    stem.position.y = viewMode === "2d" ? 62 : 125;
    group.add(base, stem);
    return group;
  }

  addBox(group, [spec.width, spec.baseHeight, spec.depth], [0, spec.baseY, 0], spec.baseColor, 0.84);
  addBox(
    group,
    [spec.loadWidth, spec.loadHeight, spec.loadDepth],
    [0, spec.loadY, 0],
    spec.loadColor,
    pallet.visual_status === "empty" ? 0.26 : pallet.is_simulated ? 0.64 : 0.82
  );
  return group;
}
