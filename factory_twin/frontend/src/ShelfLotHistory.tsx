import { useEffect, useState } from 'react';

interface History {
  id: number;
  time_archive?: { formed_on?: string | null; formation_accuracy?: string; entered_current_location_at?: string | null };
  reservations?: Array<{id: number; order_number?: string; remaining_reserved_stock_quantity: number; status: string}>;
  shelf_deliveries?: Array<{delivery_id: number; delivery_number: string; dispatched_at: string}>;
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
    <dl><div><dt>库存形成日期</dt><dd>{archive.formed_on || '历史日期待确认'}{archive.formed_on && archive.formation_accuracy !== 'exact' ? '（非精确日期）' : ''}</dd></div>
      <div><dt>进入当前货位</dt><dd>{archive.entered_current_location_at || '尚无可靠记录'}</dd></div>
      <div><dt>最近实际送货</dt><dd>{deliveries[0]?.dispatched_at || '无可见正式发货记录'}</dd></div></dl>
    <h4>当前占用订单</h4>
    {reservations.length ? reservations.map(row => <p key={row.id}>{row.order_number || '关联订单待确认'} · 占用 {row.remaining_reserved_stock_quantity}</p>) : <p>无可见未消耗预占记录</p>}
    <details><summary>实际送货记录（{deliveries.length}）</summary>{deliveries.map(row => <p key={row.delivery_id}>{row.delivery_number} · {row.dispatched_at}</p>)}</details>
    <small>以上为当前批次关联事实，不代表同款全部库存；扫码拿齐和集货不算发货。</small>
  </section>;
}
