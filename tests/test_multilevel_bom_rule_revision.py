from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_rule_revision import (
    prepare_rule_revision, apply_rule_revision, validate_rule_revision_chain,
    project_rule_and_production_events,
)
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_rule_impact import frozen_order


def revision(previous, baseline=20, number=1, previous_id=None, prod=0):
    # Pure projection fixture. It does not assert any actual shipment occurred.
    snapshots = []
    for position, original in enumerate(previous.snapshots, 1):
        row = SalesOrderItemBomComponent(**{column.key: getattr(original, column.key)
            for column in SalesOrderItemBomComponent.__table__.columns})
        row.id = number * 100000 + position
        row.order_set_quantity = 100 - baseline
        row.required_piece_quantity = row.order_set_quantity * row.quantity_per_set
        snapshots.append(row)
    proposed = replace(previous, snapshots=tuple(snapshots))
    document, checksum = prepare_rule_revision(previous, proposed, order_quantity=100, delivered_before=baseline)
    stored = SimpleNamespace(id=number, order_item_id=snapshots[0].sales_order_item_id,
        revision=number, previous_id=previous_id, previous_revision=number - 1 if number > 1 else None,
        production_revision_before=prod, order_quantity=100, delivered_before=baseline,
        document_json=document, content_hash=checksum)
    products = [SimpleNamespace(revision_id=number, order_item_id=stored.order_item_id,
        product_id=node.product_id, product_version=node.version) for node in proposed.graph.nodes]
    return stored, snapshots, products


def test_structural_revision_preserves_original_and_tracks_remaining_boundary(factory_copy):
    _, _, original = frozen_order(factory_copy)
    original_ids = {row.id for row in original.snapshots}
    first, sources, products = revision(original)
    current = apply_rule_revision(original, first, sources, products, delivered_quantity=25)
    assert current.execution_window.execution_quantity == 80
    assert current.execution_window.delivered_since == 5
    assert current.execution_window.remaining_quantity == 75
    assert {row.id for row in original.snapshots} == original_ids
    assert all(row.order_set_quantity == 100 for row in original.snapshots)
    second, second_sources, second_products = revision(current, 30, 2, first.id, 1)
    assert validate_rule_revision_chain([first, second], order_item_id=first.order_item_id,
                                        production_revision_count=1) == (first, second)
    after = apply_rule_revision(current, second, second_sources, second_products, delivered_quantity=30)
    assert after.execution_window.execution_quantity == 70
    assert {row.id for row in after.snapshots}.isdisjoint(row.id for row in sources)
    with pytest.raises(BomPlanError, match="跨越"):
        apply_rule_revision(current, second, second_sources, second_products, delivered_quantity=29)


def test_rule_revision_checks_all_hashes_sources_and_product_versions(factory_copy):
    _, _, original = frozen_order(factory_copy)
    stored, sources, products = revision(original)
    bad = SimpleNamespace(**{**vars(stored), "content_hash": "a" * 64})
    with pytest.raises(BomPlanError, match="摘要"):
        apply_rule_revision(original, bad, sources, products, delivered_quantity=20)
    with pytest.raises(BomPlanError, match="身份不完整"):
        apply_rule_revision(original, stored, sources, products[:-1], delivered_quantity=20)
    products[0].product_version += 1
    with pytest.raises(BomPlanError, match="身份不完整"):
        apply_rule_revision(original, stored, sources, products, delivered_quantity=20)
    products[0].product_version -= 1
    sources[0].snapshot_component_production_notes = "tampered"
    with pytest.raises(BomPlanError, match="新来源不一致"):
        apply_rule_revision(original, stored, sources, products, delivered_quantity=20)
    with pytest.raises(BomPlanError, match="独立来源ID"):
        prepare_rule_revision(original, original, order_quantity=100, delivered_before=0)


@pytest.mark.parametrize("change", [dict(previous_id=None), dict(previous_revision=None),
    dict(revision=3), dict(order_item_id=999), dict(production_revision_before=2),
    dict(delivered_before=19), dict(order_quantity=101)])
def test_rule_chain_rejects_missing_reordered_or_cross_order_facts(factory_copy, change):
    _, _, original = frozen_order(factory_copy)
    first, sources, products = revision(original)
    current = apply_rule_revision(original, first, sources, products, delivered_quantity=20)
    second, _, _ = revision(current, 30, 2, first.id, 1)
    second = SimpleNamespace(**{**vars(second), **change})
    with pytest.raises(BomPlanError, match="版本链"):
        validate_rule_revision_chain([first, second], order_item_id=first.order_item_id,
                                     production_revision_count=1)


def test_production_amendments_before_and_after_structure_use_the_correct_basis(factory_copy):
    from app.services.multilevel_bom_production_revision import prepare_production_revision, apply_production_revision
    _, _, original = frozen_order(factory_copy)
    item_id = original.snapshots[0].sales_order_item_id
    first_doc, first_hash = prepare_production_revision(original, {"3771": {"production_notes": "first"}})
    first_production = SimpleNamespace(id=10, order_item_id=item_id, revision=1, previous_id=None,
        document_json=first_doc, content_hash=first_hash)
    amended = apply_production_revision(original, first_doc, expected_hash=first_hash)
    rule, sources, products = revision(amended, prod=1)
    switched = apply_rule_revision(amended, rule, sources, products, delivered_quantity=20)
    second_doc, second_hash = prepare_production_revision(switched, {"3771": {"production_notes": "second"}})
    second_production = SimpleNamespace(id=11, order_item_id=item_id, revision=2, previous_id=10,
        document_json=second_doc, content_hash=second_hash)
    args = dict(sources_by_revision={1: sources}, products_by_revision={1: products}, delivered_quantity=20)
    result = project_rule_and_production_events(original, [first_production, second_production], [rule], **args)
    long = next(row for row in result.snapshots if row.component_product_id == 3771)
    assert long.snapshot_component_production_notes == "second"
    assert long.production_revision == 2
    assert result.execution_window.execution_quantity == 80
    assert next(row for row in original.snapshots if row.component_product_id == 3771).snapshot_component_production_notes != "second"
    misplaced = SimpleNamespace(**{**vars(rule), "production_revision_before": 0})
    with pytest.raises(BomPlanError, match="原配方或新来源不一致"):
        project_rule_and_production_events(original, [first_production, second_production], [misplaced], **args)
    with pytest.raises(BomPlanError, match="归属不完整"):
        project_rule_and_production_events(original, [first_production], [rule],
            **{**args, "sources_by_revision": {}})
