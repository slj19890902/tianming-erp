import pytest
from sqlalchemy import select
from tests.test_shared_bom_stock import composite_requisition_app,_p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph
from app.models.product import Product
from app.models.customer import Customer
from app.models.user import User
from app.models.shared_finished_stock import SharedFinishedGroup,SharedFinishedMember
from app.services import yg_gy_bom_alignment as alignment,shared_finished_stock as shared
from app.services.shared_catalog_bom import clone_product


def test_reviewed_ordinary_peer_to_bom_keeps_original_member_and_no_stock_writes(composite_requisition_app,_p181_published_map_identity):
    app,factory=composite_requisition_app;seed_graph(factory)
    with factory() as db:
        admin=db.get(User,1);source=db.get(Product,1)
        from app.models.multilevel_bom import ProductBomProfile
        profile=db.get(ProductBomProfile,1);profile.delivery_mode='parent';profile.material_mode='expand_children'
        source.unit='套';source.is_virtual_composite_parent=False;source.composite_fulfillment_mode='parent_delivery';db.flush()
        target_customer=Customer(customer_number=222,customer_code='GY',name='虚构光洋');db.add(target_customer);db.flush()
        db.get(Customer,1).customer_code='YG'
        target=clone_product(db,source,target_customer.id,admin)
        target.is_composite=False;target.unit='只';target.box_style='模切内盒';db.flush()
        group=SharedFinishedGroup(operation_key='historical-fixture',request_json='{}',evidence='测试旧关联',actor_id=admin.id)
        db.add(group);db.flush()
        # The fixture captures the old state before the source recipe changed.
        for p in (source,target):
            identity=shared.member_identity(target)
            db.add(SharedFinishedMember(product_id=p.id,customer_id=p.customer_id,group_id=group.id,identity_json=identity,product_basis_json=shared.product_basis(target)))
        db.commit();original=db.get(SharedFinishedMember,target.id).identity_json
        plan=alignment.preview(db,source.id,target.id)
        result=alignment.align(db,source_id=source.id,target_id=target.id,preview_hash=plan['sha256'],operation_key='reviewed-alignment',evidence='老板确认两边沿用研光有效配方',actor=admin)
        db.commit()
        assert not result['replayed'] and len(result['children'])==2 and target.is_composite
        assert db.get(SharedFinishedMember,target.id).identity_json==original
        assert shared.preview(db,product_ids=[source.id,target.id],group_id=group.id,lot_ids=[])
