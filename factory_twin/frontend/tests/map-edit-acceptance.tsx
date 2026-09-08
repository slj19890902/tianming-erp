import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { EditorCanvas } from '../src/EditorCanvas';
import type { Layout, SelectedEntity, ViewMode } from '../src/types';
import '../src/styles.css';
// Anonymous in-memory geometry only; no API, storage, database or formal map writes.
const noop = () => {};
const empty: any[] = [];
const layers = { structures: false, equipment: false, racks: true, pallets: false, zones: false, aisles: false, noGo: false, customStructures: false, labels: true, production: false };
const initial: any = { id: 'isolated-fixture', name: '匿名几何样本', floor_code: 'TEST', bounds_mm: { min_x: 0, min_y: 0, max_x: 10000, max_y: 7000 },
  structures: [], placements: [], features: [], pallets: [], violations: [], warnings: [], rule_defaults: {},
  racks: [2000, 6000].map((x, i) => ({ id: `fixture-${i}`, rack_code: `样本${i+1}`, name: `样本货架${i+1}`, x_mm: x, y_mm: 3500, z_mm: 0,
    width_mm: 2000, depth_mm: 1000, height_mm: 2000, levels: 3, bays: 3, level_heights_mm: [], access_side: 'north', rotation_deg: 0, is_locked: false })) };
function App() {
  const [layout, setLayout] = useState<Layout>(initial);
  const [selected, setSelected] = useState<SelectedEntity>({ kind: 'rack', id: 'fixture-0' });
  const [mode, setMode] = useState<ViewMode>('2d');
  const [commits, setCommits] = useState(0);
  return <main style={{ padding: 12 }}><h3>隔离验收 · 匿名样本，不连接正式系统</h3>
    <button onClick={() => setMode(mode === '2d' ? '25d' : '2d')}>切换2D/2.5D</button>
    <output style={{ marginLeft: 20 }}>提交次数 {commits}；样本1 X={Math.round(layout.racks[0].x_mm)} Y={Math.round(layout.racks[0].y_mm)}</output>
    <div style={{ height: '75vh', marginTop: 12, display: 'flex' }}><EditorCanvas layout={layout} assets={empty} viewMode={mode} cameraPreset="fit" viewResetToken={0}
      selected={selected} layers={layers} palletSnapEnabled={false} palletSnapThresholdMm={0} drawMode={null} drawPoints={empty} measureMode={false} measurePoints={empty}
      onSelect={setSelected} onMoveRack={(id,x,y) => { setLayout(previous => ({ ...previous, racks: previous.racks.map(rack => rack.id === id ? { ...rack, x_mm: x, y_mm: y } : rack) })); setCommits(count => count + 1); }}
      onMoveEquipment={noop} onMovePallet={noop} onMoveFeature={noop} onDropAsset={noop} onDropRack={noop} onDropPallet={noop} onDrawPoint={noop} onMeasurePoint={noop}
      rackEditingEnabled visualTheme="warehouse" /></div>
  </main>;
}
createRoot(document.getElementById('root')!).render(<App />);
