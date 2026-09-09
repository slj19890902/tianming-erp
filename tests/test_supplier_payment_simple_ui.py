from pathlib import Path
from html.parser import HTMLParser


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.controls = []

    def handle_starttag(self, tag, attrs):
        node = (tag, dict(attrs))
        self.controls.append((node, self.stack.copy()))
        if tag not in {'input', 'br', 'hr', 'img', 'meta', 'link'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                return


def test_supplier_workflow_discloses_sources_and_keeps_write_guards():
    html = Path('static/index.html').read_text(encoding='utf-8')
    section = html[html.index('<details v-for="row in supplierSettlements"'):html.index('<div v-if="canViewFinanceCosts"', html.index('<details v-for="row in supplierSettlements"'))]
    parser = Controls()
    parser.feed(section)
    def control(key, value):
        return next((node, parents) for node, parents in parser.controls if node[1].get(key) == value)
    _, source_parents = control('v-for', 'line in row.lines')
    details = [n for n in source_parents if n[0] == 'details']
    assert len(details) == 2 and 'open' not in details[-1][1]
    _, review_parents = control('v-model.trim', 'row._reviewNumber')
    assert len([n for n in review_parents if n[0] == 'details']) == 1
    assert '尚未确认付款金额' in section
    assert "['draft','difference'].includes(row.status)" in section
    assert "per_square_meter:'平方米'" in section
    assert '2 · 登记收到的发票' in section and '3 · 历史余额抵扣' in section
    confirm, _ = control('@click', 'confirmSupplierSettlement(row)')
    assert 'canFinance' in confirm[1]['v-if']
    assert "row.completeness?.status==='blocked'" in confirm[1][':disabled']
    assert 'row.supplier_statement_amount==null' in confirm[1][':disabled']
    payment, _ = control('@click', 'addSupplierPaymentBatch(row)')
    assert 'supplierSettlementBusy(row)' in payment[1][':disabled']
    assert 'supplierSettlementSuggestedBank(row)' in section
