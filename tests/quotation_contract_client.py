"""Current-page client for existing workflow tests; CAS/retry tests use raw TestClient."""
import re
from uuid import uuid4
from fastapi.testclient import TestClient


class QuotationContractClient(TestClient):
    def request(self, method, url, **kwargs):
        path = str(url).split("?", 1)[0]
        if method.upper() in {"POST", "PUT"} and path.startswith("/api/quotations") and path != "/api/quotations/preview":
            body = dict(kwargs.pop("json", None) or {})
            body.setdefault("idempotency_key", str(uuid4()))
            target = re.fullmatch(r"/api/quotations/(\d+)(?:/(?:generate|accept|void))?", path)
            item = re.fullmatch(r"/api/quotations/items/(\d+)/convert-to-product", path)
            if "expected_version" not in body and (target or item):
                response = super().request("GET", "/api/quotations")
                rows = response.json().get("items", []) if response.status_code == 200 else []
                row = next((q for q in rows if (target and q["id"] == int(target[1])) or
                            (item and any(i["id"] == int(item[1]) for i in q["items"]))), None)
                body["expected_version"] = row["version"] if row else 1
            kwargs["json"] = body
        return super().request(method, url, **kwargs)
