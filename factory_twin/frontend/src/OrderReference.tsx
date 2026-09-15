import '../../../static/ui/order-reference.css';

export interface OrderIdentity {
  customer_po?: string | null;
  customer_order_number?: string | null;
  order_number?: string | null;
}

export function OrderReference({row}: {row?: OrderIdentity | null}) {
  const po = (row?.customer_po || row?.customer_order_number || '').trim();
  const erp = (row?.order_number || '').trim();
  return <span className="order-reference">
    <span className={po ? 'customer-po' : 'customer-po-missing'}>{po || '未填写客户单号'}</span>
    {erp && erp !== po && <small className="erp-order-no">ERP {erp}</small>}
  </span>;
}
