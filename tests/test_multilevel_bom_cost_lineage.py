import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.bom_subkit import SubkitConversion
from app.services.bom_subkit_costs import source_cost
from app.services.bom_subkits import SubkitError


def test_graph_receipt_portion_rounding_is_nonnegative_and_conserved(monkeypatch):
    from app.services import multilevel_bom_cost_lineage as lineage
    portions = [{"purchase_receipt_fact_id": index, "amount": Decimal(amount)}
                for index, amount in enumerate(["0.0002", "0.0001", "0.0002"])]
    monkeypatch.setattr(lineage, "graph_material_sources", lambda *_: portions)
    db = SimpleNamespace(get=lambda *_: SimpleNamespace(quantity=5))
    lot = SimpleNamespace(source_ref_type="bom_assembly", source_ref_id=1)
    slices = [lineage.graph_material_cost_slice(db, lot, used=i, take=1) for i in range(5)]
    assert all(row["amount"] >= 0 for chunk in slices for row in chunk)
    for index, portion in enumerate(portions):
        assert sum(chunk[index]["amount"] for chunk in slices) == portion["amount"]


class SourceSession:
    def __init__(self, conversion, used=0):
        self.conversion = conversion
        self.used = used

    def get(self, model, key):
        assert model is SubkitConversion
        assert key == 17
        return self.conversion

    def scalar(self, _statement):
        return self.used


def assembled_lot():
    return SimpleNamespace(id=90, source_ref_type="subkit_conversion", source_ref_id=17,
                           estimated_unit_cost_snapshot=Decimal("0.3333"))


def conversion():
    return SimpleNamespace(status="posted", quantity=3, total_cost=Decimal("1.0000"),
        cost_detail_json=json.dumps({"actual": True, "currency": "CNY",
            "sources": [{"purchase_receipt_fact_id": 45, "cost": "1.0000"}]}))


def test_assembled_input_preserves_exact_total_and_purchase_lineage():
    amounts = []
    for used in range(3):
        amount, detail = source_cost(SourceSession(conversion(), used), assembled_lot(), 1)
        amounts.append(amount)
        assert detail["actual"] is True
        assert detail["currency"] == "CNY"
        assert detail["sources"][0]["purchase_receipt_fact_id"] == 45
    assert amounts == [Decimal("0.3333"), Decimal("0.3334"), Decimal("0.3333")]
    assert sum(amounts) == Decimal("1.0000")


def test_second_level_input_is_not_downgraded_to_rounded_estimate():
    amount, detail = source_cost(SourceSession(conversion()), assembled_lot(), 3)
    assert amount == Decimal("1.0000")
    assert detail["actual"] is True


@pytest.mark.parametrize("source", [None, SimpleNamespace(status="reversed")])
def test_missing_or_reversed_assembly_cannot_be_consumed(source):
    with pytest.raises(SubkitError, match="成本来源"):
        source_cost(SourceSession(source), assembled_lot(), 1)


def test_stocktake_increase_does_not_invent_original_purchase_cost():
    amount, detail = source_cost(SourceSession(conversion(), used=3), assembled_lot(), 1)
    assert amount == Decimal("0.3333")
    assert detail["actual"] is False
