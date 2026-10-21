from decimal import Decimal
import pytest
from sqlalchemy import select
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.printing_plate import PrintingPlate
from app.models.processing_cost import ProductProcessingProfile
from app.services import shared_product_master as master
from app.services.product_drawings import shared_product_drawings
from tests.test_customer_sharing import reservation_db, args, save
from tests.test_yg_gy_shared_master import edit


def catalog(db, data):
    source = data['product']
    source.sale_unit_price = source.sale_unit_price_no_tax = Decimal('0.733')
    source.cost_unit_price = Decimal('0.31')
    db.commit()
    save(db, data, args(data))
    peer = db.scalar(select(Product).where(Product.customer_id == data['other_customer'].id,
        Product.product_code == source.product_code))
    return source, peer


def test_quotes_share_original_precision_both_directions(reservation_db):
    db, data = reservation_db
    source, peer = catalog(db, data)
    assert peer.sale_unit_price_no_tax == source.sale_unit_price_no_tax == Decimal('0.733')
    assert peer.cost_unit_price == Decimal('0.31')
    edit(db, data, peer, {'sale_unit_price_no_tax': Decimal('0.73329'), 'sale_unit_price': Decimal('0.73329')})
    db.commit()
    assert source.sale_unit_price_no_tax == peer.sale_unit_price_no_tax == Decimal('0.73329')


def test_drawings_use_existing_originals_and_exclude_unrelated_products(reservation_db):
    db, data = reservation_db
    source, peer = catalog(db, data)
    original = ProductDrawing(product_id=source.id, image_path='private:drawings/shared.webp',
        thumbnail_path='private:drawings/shared-thumb.webp', uploaded_by=data['admin'].id)
    unrelated = ProductDrawing(product_id=data['other_product'].id, image_path='private:drawings/private.webp',
        thumbnail_path='private:drawings/private-thumb.webp', uploaded_by=data['admin'].id)
    db.add_all([original, unrelated]);db.commit()
    assert [d.id for d in shared_product_drawings(peer)] == [original.id]
    second = ProductDrawing(product_id=peer.id, image_path='private:drawings/new.webp',
        thumbnail_path='private:drawings/new-thumb.webp', uploaded_by=data['admin'].id)
    db.add(second);db.commit()
    assert [d.id for d in shared_product_drawings(source)] == [second.id, original.id]
    assert db.query(ProductDrawing).count() == 3


def test_foreign_plate_must_belong_to_the_exact_shared_product(reservation_db):
    db, data = reservation_db
    source, peer = catalog(db, data)
    # Transient plate objects are sufficient to check the exact assignment rule.
    assigned = PrintingPlate(id=6701,customer_id=source.customer_id)
    other = PrintingPlate(id=6702,customer_id=source.customer_id)
    source.printing_plate_1_id = assigned.id
    with db.no_autoflush:
        assert master.plate_allowed(db, peer, assigned)
        assert not master.plate_allowed(db, peer, other)
    db.rollback()


def test_labour_exception_sync_is_atomic_and_keeps_one_profile_per_product(reservation_db):
    db, data = reservation_db
    source, peer = catalog(db, data)
    profile = ProductProcessingProfile(product_id=source.id,printer_mode='new',die_cut_mode='small_complex',
        assembly_worker_days_per_1000=Decimal('0.4'),version=1)
    db.add(profile);db.flush()
    master.sync_processing_profile(db,source,profile,data['admin']);db.commit()
    target=db.scalar(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id==peer.id))
    assert target.assembly_worker_days_per_1000 == Decimal('0.4')
    target.die_cut_mode='large';target.version+=1;db.flush()
    master.sync_processing_profile(db,peer,target,data['admin']);db.rollback()
    db.refresh(profile)
    assert profile.die_cut_mode=='small_complex'


def test_mobile_shared_drawing_uses_original_and_scope_remains_product_specific(reservation_db):
    from app.services.mobile_product_drawings import product_drawing_metadata,require_drawing_source
    from fastapi import HTTPException
    db,data=reservation_db;source,peer=catalog(db,data)
    original=ProductDrawing(product_id=source.id,image_path='private:shared.pdf',thumbnail_path='private:shared.pdf')
    other=ProductDrawing(product_id=data['other_product'].id,image_path='private:other.pdf',thumbnail_path='private:other.pdf')
    db.add_all([original,other]);db.commit()
    metadata=product_drawing_metadata(db,[peer.id],user=data['admin'],visible_customer_ids=[peer.customer_id])
    assert metadata[peer.id]['items'][0]['id']=='attachment-'+str(original.id)
    assert require_drawing_source(db,peer,'attachment-'+str(original.id)).reference==original.image_path
    with pytest.raises(HTTPException):require_drawing_source(db,peer,'attachment-'+str(other.id))
