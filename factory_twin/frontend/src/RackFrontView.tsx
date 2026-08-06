import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { buildRackFrontSlots } from "./rackFront.mjs";
import type { Rack } from "./types";
import type { RackFrontSlot } from "./rackFront.mjs";

interface Props {
  rack: Rack;
  onClose: () => void;
}

export function RackFrontView({ rack, onClose }: Props) {
  const tiers = useMemo(() => buildRackFrontSlots(rack), [rack]);
  const [selectedCargo, setSelectedCargo] = useState<RackFrontSlot | null>(null);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  return <div className="rack-focus-overlay" role="dialog" aria-modal="true" aria-label={`${rack.rack_code} 货架正视图`}>
    <div className="rack-focus-backdrop" onClick={onClose} />
    <section className="rack-focus-stage">
      <header className="rack-focus-header">
        <div><span className="rack-focus-kicker">INDUSTRIAL RACK ELEVATION</span><h2>{rack.rack_code} · {rack.name}</h2><p>{rack.width_mm} × {rack.depth_mm} × {rack.height_mm} mm · {rack.levels} 层 · 正面 {rack.access_side}</p></div>
        <button type="button" className="rack-focus-close" onClick={onClose}>返回平面图 <kbd>Esc</kbd></button>
      </header>

      <div className="rack-focus-content">
        <div className="rack-elevation-wrap">
          <div className="rack-height-rule"><b>{rack.height_mm} mm</b><span /></div>
          <div className="rack-elevation" style={{ "--rack-color": rack.color } as CSSProperties}>
            <i className="rack-post left" /><i className="rack-post right" />
            {tiers.map((tier) => <div className={`rack-tier ${tier.isGround ? "ground" : "shelf"}`} key={tier.tier}>
              <div className="rack-tier-meta"><strong>{tier.title}</strong><small>{tier.isGround ? "地面 0 mm" : `横梁离地 ${tier.heightMm} mm`}</small></div>
              <div className="rack-cargo-row" style={{ gridTemplateColumns: `repeat(${tier.slots.length}, minmax(0, 1fr))` }}>
                {tier.slots.map((slot) => <button type="button" className={`rack-cargo ${slot.isGround ? "pallet-cargo" : "box-cargo"} ${selectedCargo?.id === slot.id ? "selected" : ""}`} key={slot.id} onClick={() => setSelectedCargo(slot)}>
                  <span className="cargo-mark">TM</span><b>{slot.label}</b><small>{slot.quantity}{slot.unit}</small>
                </button>)}
              </div>
              <span className="rack-beam" />
            </div>)}
          </div>
          <div className="rack-width-rule"><span /><b>{rack.width_mm} mm</b><span /></div>
        </div>

        <aside className="cargo-label-panel">
          <div className="cargo-label-status">只读演示标签 · 未绑定正式库存</div>
          {!selectedCargo ? <div className="cargo-label-empty"><span>＋</span><b>点击一件货物</b><p>查看货架、层级、格位和模拟纸箱标签。正式库存请在隔离ERP库存地图预览中查看。</p></div> : <article className="cargo-ticket">
            <header><span>货物标签</span><b>{selectedCargo.id}</b></header>
            <dl><div><dt>货架</dt><dd>{rack.rack_code}</dd></div><div><dt>存放层</dt><dd>{selectedCargo.isGround ? "地面栈板" : `${selectedCargo.tier} 层`}</dd></div><div><dt>格位</dt><dd>{selectedCargo.column}</dd></div><div><dt>模拟品名</dt><dd>{selectedCargo.sampleSku}</dd></div><div><dt>模拟数量</dt><dd>{selectedCargo.quantity} {selectedCargo.unit}</dd></div><div><dt>坐标</dt><dd>X {rack.x_mm} / Y {rack.y_mm} mm</dd></div></dl>
            <footer><span className="ticket-barcode">|||| ||| || |||||</span><small>候选可视化，不生成正式库位，不修改库存</small></footer>
          </article>}
        </aside>
      </div>
    </section>
  </div>;
}
