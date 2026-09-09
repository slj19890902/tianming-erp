import { useEffect, useState } from 'react';
import { beijingDisplay } from './beijingDisplay.mjs';

interface History {
  id: number;
  time_archive?: { formed_on?: string | null; formation_accuracy?: string; entered_current_location_at?: string | null };
  reservations?: Array<{id: number; order_number?: string; remaining_reserved_stock_quantity: number; status: string}>;
  shelf_deliveries?: Array<{delivery_id: number; delivery_number: string; dispatched_at: string}>;
  shelf_related_inventory?: {
    source_order?: {order_number: string} | null;
    same_product_locations: Array<{lot_id: number; location_id: number; location_name: string;
      floor: number; physical_quantity: number; available_quantity: number; reserved_quantity: number; status: string; unit: string}>;
  };
}

export function ShelfLotHistory({lotId, load}: {lotId: number; load: (url: string) => Promise<History>}) {
  const [result, setResult] = useState<{lotId: number; data?: History; error?: string} | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    setResult(null);
    load(`/api/warehouse/lots/${lotId}`).then(data => {
      if (data.id !== lotId) throw new Error('批次身份不一致，请刷新');
      if (current) setResult({lotId, data});
    }).catch(error => { if (current) setResult({lotId, error: String(error.message || error)}); });
    return () => { current = false; };
  }, [lotId, load, retry]);
  if (!result || result.lotId !== lotId) return <p role="status">读取批次时间与订单…</p>;
  if (result.error) return <div role="alert">读取失败：{result.error}<button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></div>;
  const data = result.data!;
  const archive = data.time_archive || {};
  const reservations = (data.reservations || []).filter(row => row.remaining_reserved_stock_quantity > 0);
  const deliveries = data.shelf_deliveries || [];
  return <section className="shelf-lot-history" aria-label="当前批次时间和订单">
    <dl className="shelf-lot-times"><div><dt>入库日期</dt><dd>{beijingDisplay(archive.formed_on)}{archive.formed_on && archive.formation_accuracy !== 'exact' ? '（约）' : ''}</dd></div>
      <div><dt>进入货位</dt><dd>{beijingDisplay(archive.entered_current_location_at)}</dd></div></dl>
    <details><summary>关联订单与送货</summary>
    <p>来源订单：{data.shelf_related_inventory?.source_order?.order_number || '—'}</p>
    {reservations.length ? reservations.map(row => <p key={row.id}>{row.order_number || '关联订单待确认'} · 占用 {row.remaining_reserved_stock_quantity}</p>) : <p>无可见未消耗预占记录</p>}
    <details><summary>送货记录（{deliveries.length}）</summary>{deliveries.map(row => <p key={row.delivery_id}>{row.delivery_number} · {beijingDisplay(row.dispatched_at)}</p>)}</details>
    </details>
    <details><summary>同款库存位置</summary>
      {(data.shelf_related_inventory?.same_product_locations || []).map(row => <p key={row.lot_id}>
        <a href={`/warehouse.html?floor=${row.floor}F&location_id=${row.location_id}&lot_id=${row.lot_id}`} target="_blank" rel="noopener noreferrer">{row.location_name}</a>
        {' · '}实物 {row.physical_quantity} · 可用 {row.available_quantity} · 占用 {row.reserved_quantity}{row.status === 'frozen' ? ' · 已冻结' : ''}
      </p>)}
      <small>库存抵扣须在订单中确认。</small>
    </details>
  </section>;
}
