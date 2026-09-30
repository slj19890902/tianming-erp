"""Defence-in-depth price projection for scoped business accounts."""
import json
import gzip
import re
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

COST_KEYS={"board_price","quote_price","rule_base_price","suggested_price","base_price","processing_price","paper_price",
 "purchase_currency","purchase_tax_rate","purchase_tax_included","price_source","supplier_quote","supplier_quotes",
 "frozen_unit_price","material_unit_price","board_unit_price","estimated_inventory_value","actual_inventory_value",
 "external_packaging_candidate_snapshot_json"}
PURCHASE_KEYS={"unit_price","price","amount","total_amount","subtotal","tax_amount","amount_without_tax","amount_with_tax","currency","price_unit"}
def public_business_payload(value, purchase=False):
    if isinstance(value,list):return [public_business_payload(v,purchase) for v in value]
    if not isinstance(value,dict):return value
    result={}
    for key,item in value.items():
        normalized=re.sub(r"([a-z])([A-Z])",r"\1_\2",str(key)).lower()
        if normalized in COST_KEYS or any(token in normalized for token in ("cost","profit","margin","purchase_price","purchase_amount","supplier_price")):continue
        if purchase and (normalized in PURCHASE_KEYS or any(word in normalized for word in ("price", "amount", "currency", "tax_rate"))):continue
        if isinstance(item,str) and normalized.endswith("_json"):
            try:item=json.dumps(public_business_payload(json.loads(item),purchase),ensure_ascii=False)
            except (ValueError,TypeError):pass
        nested_purchase=purchase or any(word in normalized for word in ("purchase", "procurement", "supplier_quote"))
        result[key]=public_business_payload(item,nested_purchase)
    return result

class BusinessVisibilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        response=await call_next(request)
        if not getattr(request.state,"restricted_business",False):return response
        path=request.url.path
        purchase=path.startswith(("/api/requisition/","/api/incoming/","/api/master/materials","/api/master/suppliers"))
        content_type=response.headers.get("content-type","")
        if purchase and response.status_code<400 and "json" not in content_type:
            if hasattr(response.body_iterator,"aclose"):
                await response.body_iterator.aclose()
            return Response('{"detail":"该账号不可下载含采购信息的原始单据"}',status_code=403,media_type="application/json")
        if "application/json" not in content_type:return response
        body=b"".join([chunk async for chunk in response.body_iterator])
        if response.headers.get("content-encoding")=="gzip": body=gzip.decompress(body)
        data=public_business_payload(json.loads(body),purchase)
        headers={k:v for k,v in response.headers.items() if k.lower() not in {"content-length","etag","content-encoding"}}
        headers["cache-control"]="no-store"
        return Response(json.dumps(data,ensure_ascii=False,default=str),status_code=response.status_code,headers=headers,media_type="application/json",background=response.background)
