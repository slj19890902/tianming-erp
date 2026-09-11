import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from app.services.multilevel_bom_snapshot import (
    dump_graph, graph_hash, load_graph, verify_current_identities,
)
from tests.test_multilevel_bom_plan import liner_graph


def test_round_trip_preserves_real_liner_hierarchy_and_procurement():
    graph = liner_graph()
    document = dump_graph(graph)
    frozen = load_graph(document, expected_hash=graph_hash(document))
    assert plan_bom(frozen, 100) == plan_bom(graph, 100)
    assert {(e.parent_id, e.child_id) for e in frozen.edges} == {(1, 2), (2, 3), (2, 4)}
    assert dict(plan_bom(frozen, 100).picking) == {1: 100, 2: 100}


def test_canonical_hash_does_not_depend_on_list_order():
    graph = liner_graph()
    assert dump_graph(graph) == dump_graph(replace(graph, nodes=graph.nodes[::-1], edges=graph.edges[::-1]))


def test_name_change_does_not_rewrite_frozen_order():
    graph = liner_graph()
    document = dump_graph(graph)
    changed = replace(graph, nodes=tuple(replace(n, name="新版内衬", version=2) if n.product_id == 2 else n for n in graph.nodes))
    assert next(n for n in load_graph(document).nodes if n.product_id == 2).name == "内衬"
    assert graph_hash(dump_graph(changed)) != graph_hash(document)
    assert plan_bom(changed, 100) == plan_bom(load_graph(document), 100)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version=True),
    lambda d: d.update(schema_version=99),
    lambda d: d.update(extra="ignored?"),
    lambda d: d["nodes"][0].update(product_id=True),
    lambda d: d["nodes"][0].update(name=12),
    lambda d: d["nodes"][0].update(unit=None),
    lambda d: d["nodes"][0].update(routes={}),
    lambda d: d["nodes"][0]["routes"][0].update(pieces_per_sheet=1.5),
    lambda d: d["nodes"][0]["routes"][0].update(key=""),
    lambda d: d["edges"][0].update(quantity="2"),
    lambda d: d["edges"][0].update(relation=None),
    lambda d: d["edges"][0].update(child_id=999),
    lambda d: d.update(nodes=[]),
])
def test_malformed_snapshot_fails_closed(mutate):
    data = json.loads(dump_graph(liner_graph()))
    mutate(data)
    with pytest.raises(BomPlanError):
        load_graph(json.dumps(data))


def test_tampered_hash_and_duplicate_json_keys_are_rejected():
    document = dump_graph(liner_graph())
    with pytest.raises(BomPlanError, match="校验失败"):
        load_graph(document.replace("内衬", "伪造"), expected_hash=graph_hash(document))
    with pytest.raises(BomPlanError):
        load_graph(document.replace('"root_id":1', '"root_id":1,"root_id":2'))


def master_products(graph):
    return [SimpleNamespace(id=n.product_id, customer_id=n.customer_id,
        version=n.version, product_name=n.name, unit=n.unit, is_active=True,
        deleted_at=None, purged_at=None) for n in graph.nodes]


def test_freeze_checks_product_ids_even_when_material_codes_are_shared():
    graph = liner_graph()
    products = master_products(graph)
    for product in products:
        product.product_code = "Z.001.000148"
    verify_current_identities(graph, products)
    with pytest.raises(BomPlanError):
        verify_current_identities(graph, products[:-1])


@pytest.mark.parametrize("field,value", [
    ("customer_id", 999), ("version", 2), ("is_active", False),
    ("deleted_at", "2026-09-09"), ("purged_at", "2026-09-09"),
    ("product_name", "其他产品"), ("unit", "箱"),
])
def test_stale_or_incompatible_master_cannot_be_frozen(field, value):
    graph = liner_graph()
    products = master_products(graph)
    setattr(products[1], field, value)
    with pytest.raises(BomPlanError):
        verify_current_identities(graph, products)
