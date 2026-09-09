from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Groups(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.groups = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for group in self.stack:
            if attrs.get("id"):
                self.groups.setdefault(group, set()).add(attrs["id"])
        if tag == "div":
            self.stack.append(attrs.get("class", "") + str(len(self.groups)))
            self.groups.setdefault(self.stack[-1], set())

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            self.stack.pop()


def test_compact_rows_keep_controls_and_safety_confirmation():
    html = (ROOT / "static/mobile_stocktake.html").read_text(encoding="utf-8")
    parser = Groups()
    parser.feed(html)
    for prefix, ids in [
        ("field search-pair", {"inboundCustomerQuery", "inboundFindCustomer", "inboundCustomer"}),
        ("field search-pair", {"inboundProductQuery", "inboundFindProduct", "inboundProduct"}),
        ("inbound-pair", {"inboundQuantity", "inboundDate"}),
        ("inbound-actions", {"inboundSave", "inboundCancel", "inboundRefresh"}),
    ]:
        assert any(key.startswith(prefix) and ids <= values for key, values in parser.groups.items())
    assert 'id="inboundExistingAcknowledged"' in html
    assert ".sticky-submit{position:static;" in html


def test_print_entry_and_ordered_batch_fields():
    src = (ROOT / "factory_twin/frontend/src/WarehouseTwinApp.tsx").read_text(encoding="utf-8")
    fields = src.split('className="shelf-current-fields"', 1)[1].split("</dl>", 1)[0]
    labels = ["客户", "存货编码", "产品名称", "尺寸", "数量"]
    positions = [fields.index(f"<dt>{label}</dt>") for label in labels]
    assert positions == sorted(positions)
    assert ">打印货架标签</button>" in src
    assert "content=shelf-information&location_ids=" in src
