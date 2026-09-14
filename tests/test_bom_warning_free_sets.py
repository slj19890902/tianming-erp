from decimal import Decimal

from sqlalchemy import select

from test_common_box_low_stock_alert import _factory, _add_finished_lot
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.stock_replenishment import InventoryStockPolicy
from app.models.warehouse_inventory import WarehouseLocation, FinishedGoodsInventoryDetail
from app.services.finished_stock_identity import product_basis
from app.services.stock_replenishment import stock_policy_dict, virtual_composite_replenishment_demand_plan


def test_internal_assembled_parent_counts_free_matching_sets_not_same_code_pieces(tmp_path):
    _, factory, ids = _factory(tmp_path)
    with factory() as db:
        location = db.get(WarehouseLocation, ids['standard_location'])
        parent = Product(customer_id=ids['customer_a'], product_code='KIT', customer_material_code='KIT', product_name='套件',
                         is_composite=True, is_internal_component=True, unit='套')
        components = [Product(customer_id=ids['customer_a'], product_code='KIT', customer_material_code=name, product_name=name,
                      is_internal_component=True) for name in ['长片', '短片']]
        db.add_all([parent, *components]); db.flush()
        for index, (component, ratio, quantity) in enumerate(zip(components, [3, 4], [4680, 6240])):
            db.add(ProductBomComponent(parent_product_id=parent.id, component_product_id=component.id,
                quantity_per_set=Decimal(ratio), display_order=index, internal_component_code=str(index),
                is_required=True))
            _add_finished_lot(db, lot_number=f'FREE-{index}', product=component,
                              location=location, quantity_available=quantity)
            db.flush()
            detail = db.scalar(select(FinishedGoodsInventoryDetail).where(FinishedGoodsInventoryDetail.product_id == component.id))
            detail.physical_basis_json = product_basis(component)
        policy = InventoryStockPolicy(policy_name='套件预警', target_inventory_type='finished',
            product_id=parent.id, customer_id=parent.customer_id, warning_quantity=1500,
            target_quantity=1800, active=True)
        db.add(policy); db.flush()
        summary = stock_policy_dict(db, policy)
        assert summary['available_quantity'] == 1560
        assert summary['assembled_quantity'] == 0
        assert summary['unassembled_available_set_quantity'] == 1560
        assert summary['allocatable_available_quantity'] == 0
        assert summary['warning_triggered'] is False
        assert summary['suggested_replenishment_quantity'] == 240
        plan = virtual_composite_replenishment_demand_plan(db, product=parent, finished_quantity=240)
        assert [d['suggested_component_piece_quantity'] for d in plan['component_demands']] == [720, 960]
        # Different physical identity must not cover the current recipe.
        components[1].production_notes = '新工艺'
        db.flush()
        summary = stock_policy_dict(db, policy)
        assert summary['unassembled_available_set_quantity'] == 0
        assert summary['warning_triggered'] is True
        plan = virtual_composite_replenishment_demand_plan(db, product=parent, finished_quantity=1800)
        assert [d['suggested_component_piece_quantity'] for d in plan['component_demands']] == [720, 7200]
