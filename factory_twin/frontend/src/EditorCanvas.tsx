import { useEffect, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type {
  AssetTemplate,
  LayerVisibility,
  Layout,
  SelectedEntity,
  Structure,
  ViewMode
} from "./types";

interface Props {
  layout: Layout;
  assets: AssetTemplate[];
  viewMode: ViewMode;
  selected: SelectedEntity;
  layers: LayerVisibility;
  drawMode: "zone" | "aisle" | "no_go" | null;
  drawPoints: number[][];
  onSelect: (entity: SelectedEntity) => void;
  onMoveEquipment: (id: string, xMm: number, yMm: number) => void;
  onMoveRack: (id: string, xMm: number, yMm: number) => void;
  onDropAsset: (templateId: string, xMm: number, yMm: number) => void;
  onDropRack: (rack: Record<string, unknown>, xMm: number, yMm: number) => void;
  onDrawPoint: (xMm: number, yMm: number) => void;
}

function materialFor(kind: Structure["kind"]) {
  const colors: Record<Structure["kind"], number> = {
    exterior_wall: 0x1e293b,
    wall: 0x475569,
    column: 0x334155,
    door: 0xd97706,
    unknown: 0x94a3b8
  };
  return new THREE.LineBasicMaterial({ color: colors[kind] });
}

function textSprite(text: string, color = "#0f172a") {
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 96;
  const context = canvas.getContext("2d")!;
  context.fillStyle = "rgba(255,255,255,.9)";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = color;
  context.lineWidth = 6;
  context.strokeRect(2, 2, canvas.width - 4, canvas.height - 4);
  context.fillStyle = color;
  context.font = "bold 38px Microsoft YaHei, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text.slice(0, 24), canvas.width / 2, canvas.height / 2);
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false }));
  sprite.scale.set(3000, 560, 1);
  sprite.renderOrder = 30;
  return sprite;
}

function entityNode(object: THREE.Object3D | null): THREE.Object3D | null {
  let current = object;
  while (current) {
    if (current.userData.entityKind && current.userData.entityId) return current;
    current = current.parent;
  }
  return null;
}

export function EditorCanvas({
  layout,
  assets,
  viewMode,
  selected,
  layers,
  drawMode,
  drawPoints,
  onSelect,
  onMoveEquipment,
  onMoveRack,
  onDropAsset,
  onDropRack,
  onDrawPoint
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const width = Math.max(container.clientWidth, 400);
    const height = Math.max(container.clientHeight, 420);
    const bounds = layout.bounds_mm;
    const centerX = (bounds.min_x + bounds.max_x) / 2;
    const centerY = (bounds.min_y + bounds.max_y) / 2;
    const span = Math.max(bounds.max_x - bounds.min_x, bounds.max_y - bounds.min_y, 10000);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf8fafc);
    const camera = new THREE.OrthographicCamera(
      (-span * width) / height / 1.8,
      (span * width) / height / 1.8,
      span / 1.8,
      -span / 1.8,
      1,
      span * 10
    );
    if (viewMode === "2d") {
      camera.position.set(0, span * 2, 0.001);
      camera.up.set(0, 0, -1);
    } else {
      camera.position.set(span * 0.95, span * 0.9, span * 0.95);
      camera.up.set(0, 1, 0);
    }
    camera.lookAt(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(width, height);
    container.replaceChildren(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.enableRotate = viewMode === "25d";
    controls.screenSpacePanning = true;
    controls.maxZoom = 12;
    controls.minZoom = 0.25;

    scene.add(new THREE.AmbientLight(0xffffff, 1.7));
    const directional = new THREE.DirectionalLight(0xffffff, 1.4);
    directional.position.set(span, span * 1.5, span);
    scene.add(directional);
    const grid = new THREE.GridHelper(span * 2.2, 40, 0xcbd5e1, 0xe2e8f0);
    grid.position.y = -2;
    scene.add(grid);
    const worldPoint = (xMm: number, yMm: number, elevation = 0) =>
      new THREE.Vector3(xMm - centerX, elevation, -(yMm - centerY));
    const violationIds = new Set(
      layout.violations.flatMap((item) => [item.entity_id, item.related_id || ""])
    );
    const interactive: THREE.Object3D[] = [];

    if (layers.structures) {
      for (const structure of layout.structures) {
        const geometry = structure.geometry;
        if (geometry.type === "polyline" && geometry.points) {
          const points = geometry.points.map(([x, y]) => worldPoint(x, y, 8));
          if (geometry.closed && points.length) points.push(points[0].clone());
          scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), materialFor(structure.kind)));
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
            scene.add(column);
          }
        } else if (geometry.type === "circle") {
          const center = worldPoint(geometry.x_mm || 0, geometry.y_mm || 0);
          const radius = geometry.radius_mm || 250;
          const mesh = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, viewMode === "25d" ? 3000 : 40, 20),
            new THREE.MeshStandardMaterial({ color: structure.kind === "column" ? 0x475569 : 0x94a3b8 })
          );
          mesh.position.set(center.x, viewMode === "25d" ? 1500 : 20, center.z);
          scene.add(mesh);
          if (layers.labels && structure.column_code) {
            const label = textSprite(structure.column_code);
            label.position.set(center.x, viewMode === "25d" ? 3300 : 160, center.z);
            scene.add(label);
          }
        } else if (geometry.type === "insert") {
          const center = worldPoint(geometry.x_mm || 0, geometry.y_mm || 0);
          const marker = new THREE.Mesh(
            new THREE.BoxGeometry(500, 40, 500),
            new THREE.MeshBasicMaterial({ color: structure.kind === "door" ? 0xd97706 : 0x94a3b8 })
          );
          marker.position.set(center.x, 20, center.z);
          scene.add(marker);
        }
      }
    }

    for (const feature of layout.features) {
      const visible = feature.feature_kind === "zone" ? layers.zones : feature.feature_kind === "aisle" ? layers.aisles : layers.noGo;
      if (!visible || feature.points.length < 2) continue;
      const violated = violationIds.has(feature.id);
      const color = violated ? "#dc2626" : feature.color;
      const group = new THREE.Group();
      group.userData = { entityKind: "feature", entityId: feature.id, draggable: false };
      if (feature.feature_kind === "aisle") {
        for (let index = 0; index < feature.points.length - 1; index += 1) {
          const [x1, y1] = feature.points[index];
          const [x2, y2] = feature.points[index + 1];
          const start = worldPoint(x1, y1);
          const end = worldPoint(x2, y2);
          const length = start.distanceTo(end);
          const mesh = new THREE.Mesh(
            new THREE.BoxGeometry(length, viewMode === "25d" ? 30 : 18, feature.width_mm || 1),
            new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.34 })
          );
          mesh.position.copy(start).add(end).multiplyScalar(0.5);
          mesh.position.y = 12;
          mesh.rotation.y = Math.atan2(y2 - y1, x2 - x1);
          group.add(mesh);
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
        const mesh = new THREE.Mesh(
          featureGeometry,
          new THREE.MeshBasicMaterial({ color, transparent: true, opacity: feature.feature_kind === "no_go" ? 0.42 : 0.25, side: THREE.DoubleSide })
        );
        mesh.rotation.x = -Math.PI / 2;
        mesh.position.y = viewMode === "25d" && elevatedZone
          ? feature.elevation_mm
          : feature.feature_kind === "no_go" ? 22 : 10;
        group.add(mesh);
      }
      interactive.push(group);
      scene.add(group);
      if (layers.labels) {
        const average = feature.points.reduce((sum, point) => [sum[0] + point[0], sum[1] + point[1]], [0, 0]);
        const labelHeight = viewMode === "25d" && feature.feature_kind === "zone" && feature.storage_mode !== "floor"
          ? feature.elevation_mm + feature.storage_height_mm + 260
          : viewMode === "25d" ? 500 : 90;
        const labelAt = worldPoint(average[0] / feature.points.length, average[1] / feature.points.length, labelHeight);
        const levelLabel = feature.feature_kind === "zone" && feature.storage_mode !== "floor"
          ? `${feature.feature_code} ↑${feature.elevation_mm}mm`
          : feature.feature_code;
        const label = textSprite(levelLabel, color);
        label.position.copy(labelAt);
        scene.add(label);
      }
    }

    const textureLoader = new THREE.TextureLoader();
    if (layers.equipment) {
      for (const placement of layout.placements) {
        const template = assets.find((item) => item.id === placement.template_id);
        const isSelected = selected?.kind === "equipment" && selected.id === placement.id;
        const violated = violationIds.has(placement.id);
        const group = new THREE.Group();
        group.userData = { entityKind: "equipment", entityId: placement.id, draggable: !placement.is_locked };
        let mesh: THREE.Mesh;
        if (template?.render_type === "png" && template.image_url) {
          const texture = textureLoader.load(template.image_url);
          texture.colorSpace = THREE.SRGBColorSpace;
          mesh = new THREE.Mesh(
            new THREE.PlaneGeometry(placement.width_mm, placement.depth_mm),
            new THREE.MeshBasicMaterial({ map: texture, transparent: true, side: THREE.DoubleSide })
          );
          mesh.rotation.x = -Math.PI / 2;
          mesh.position.y = 45;
        } else {
          mesh = new THREE.Mesh(
            new THREE.BoxGeometry(placement.width_mm, viewMode === "25d" ? placement.height_mm : 80, placement.depth_mm),
            new THREE.MeshStandardMaterial({
              color: new THREE.Color(violated ? "#dc2626" : template?.color || "#2563eb"),
              emissive: isSelected ? 0x334155 : 0x000000,
              roughness: 0.72
            })
          );
          mesh.position.y = viewMode === "25d" ? placement.height_mm / 2 : 40;
        }
        group.add(mesh);
        const outline = new THREE.LineSegments(
          new THREE.EdgesGeometry(mesh.geometry),
          new THREE.LineBasicMaterial({ color: violated ? 0xdc2626 : placement.is_locked ? 0x166534 : isSelected ? 0x1d4ed8 : 0x0f172a })
        );
        outline.position.copy(mesh.position);
        outline.rotation.copy(mesh.rotation);
        group.add(outline);
        const position = worldPoint(placement.x_mm, placement.y_mm);
        group.position.set(position.x, 0, position.z);
        group.rotation.y = THREE.MathUtils.degToRad(-placement.rotation_deg);
        interactive.push(group);
        scene.add(group);
      }
    }

    if (layers.racks) {
      for (const rack of layout.racks) {
        const isSelected = selected?.kind === "rack" && selected.id === rack.id;
        const violated = violationIds.has(rack.id);
        const group = new THREE.Group();
        group.userData = { entityKind: "rack", entityId: rack.id, draggable: !rack.is_locked };
        const boxHeight = viewMode === "25d" ? rack.height_mm : 100;
        const frame = new THREE.Mesh(
          new THREE.BoxGeometry(rack.width_mm, boxHeight, rack.depth_mm),
          new THREE.MeshStandardMaterial({
            color: new THREE.Color(violated ? "#dc2626" : rack.color),
            transparent: true,
            opacity: viewMode === "25d" ? 0.58 : 0.72,
            roughness: 0.7
          })
        );
        frame.position.y = boxHeight / 2;
        group.add(frame);
        for (let level = 1; level < rack.levels; level += 1) {
          const shelf = new THREE.Mesh(
            new THREE.BoxGeometry(rack.width_mm, viewMode === "25d" ? 35 : 8, rack.depth_mm),
            new THREE.MeshBasicMaterial({ color: 0xffffff })
          );
          shelf.position.y = viewMode === "25d" ? (rack.height_mm * level) / rack.levels : 106 + level * 9;
          group.add(shelf);
        }
        for (let bay = 1; bay < rack.bays; bay += 1) {
          const divider = new THREE.Mesh(
            new THREE.BoxGeometry(22, boxHeight, rack.depth_mm),
            new THREE.MeshBasicMaterial({ color: 0xffffff })
          );
          divider.position.set(-rack.width_mm / 2 + (rack.width_mm * bay) / rack.bays, boxHeight / 2, 0);
          group.add(divider);
        }
        const outline = new THREE.LineSegments(
          new THREE.EdgesGeometry(frame.geometry),
          new THREE.LineBasicMaterial({ color: violated ? 0xdc2626 : rack.is_locked ? 0x166534 : isSelected ? 0x1d4ed8 : 0x4c1d95 })
        );
        outline.position.copy(frame.position);
        group.add(outline);
        const position = worldPoint(rack.x_mm, rack.y_mm);
        group.position.set(position.x, 0, position.z);
        group.rotation.y = THREE.MathUtils.degToRad(-rack.rotation_deg);
        interactive.push(group);
        scene.add(group);
        if (layers.labels) {
          const label = textSprite(rack.rack_code, violated ? "#dc2626" : "#4c1d95");
          label.position.set(position.x, viewMode === "25d" ? rack.height_mm + 380 : 150, position.z);
          scene.add(label);
        }
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
      for (const point of previewPoints) {
        const dot = new THREE.Mesh(new THREE.SphereGeometry(100, 12, 12), new THREE.MeshBasicMaterial({ color: 0x2563eb }));
        dot.position.copy(point);
        scene.add(dot);
      }
    }

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const ground = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    let dragging: { kind: "equipment" | "rack"; id: string; object: THREE.Object3D } | null = null;
    const setPointer = (event: PointerEvent | DragEvent) => {
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
      const root = entityNode(raycaster.intersectObjects(interactive, true)[0]?.object || null);
      if (drawMode) {
        const point = intersectGround();
        if (point) onDrawPoint(Math.round(point.x + centerX), Math.round(centerY - point.z));
        return;
      }
      if (!root) {
        onSelect(null);
        return;
      }
      const kind = root.userData.entityKind as "equipment" | "rack" | "feature";
      const id = String(root.userData.entityId);
      onSelect({ kind, id });
      if ((kind === "equipment" || kind === "rack") && root.userData.draggable) {
        dragging = { kind, id, object: root };
        controls.enabled = false;
        renderer.domElement.setPointerCapture(event.pointerId);
      }
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!dragging) return;
      setPointer(event);
      const point = intersectGround();
      if (point) dragging.object.position.set(point.x, 0, point.z);
    };
    const onPointerUp = (event: PointerEvent) => {
      if (!dragging) return;
      const current = dragging;
      dragging = null;
      controls.enabled = true;
      if (renderer.domElement.hasPointerCapture(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
      const xMm = Math.round(current.object.position.x + centerX);
      const yMm = Math.round(centerY - current.object.position.z);
      if (current.kind === "equipment") onMoveEquipment(current.id, xMm, yMm);
      else onMoveRack(current.id, xMm, yMm);
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
      if (templateId) onDropAsset(templateId, xMm, yMm);
      else if (rackJson) onDropRack(JSON.parse(rackJson), xMm, yMm);
    };
    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("pointermove", onPointerMove);
    renderer.domElement.addEventListener("pointerup", onPointerUp);
    renderer.domElement.addEventListener("dragover", onDragOver);
    renderer.domElement.addEventListener("drop", onDrop);

    let frame = 0;
    const animate = () => {
      controls.update();
      renderer.render(scene, camera);
      frame = requestAnimationFrame(animate);
    };
    animate();
    const resizeObserver = new ResizeObserver(() => {
      renderer.setSize(Math.max(container.clientWidth, 400), Math.max(container.clientHeight, 420));
    });
    resizeObserver.observe(container);
    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      renderer.domElement.removeEventListener("pointerup", onPointerUp);
      renderer.domElement.removeEventListener("dragover", onDragOver);
      renderer.domElement.removeEventListener("drop", onDrop);
      controls.dispose();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.Line || object instanceof THREE.LineSegments) {
          object.geometry.dispose();
          const materials = Array.isArray(object.material) ? object.material : [object.material];
          materials.forEach((material) => material.dispose());
        }
      });
      renderer.dispose();
    };
  }, [layout, assets, viewMode, selected, layers, drawMode, drawPoints, onSelect, onMoveEquipment, onMoveRack, onDropAsset, onDropRack, onDrawPoint]);

  return <div className={`editor-canvas ${drawMode ? "drawing" : ""}`} ref={containerRef} />;
}
