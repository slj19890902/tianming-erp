"""Explicit isolated-copy check. Never accepts a production/source database path."""
from pathlib import Path
import json
import os
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.api.drawing_design import DesignWrite, PublishWrite, save_design, publish_design, download_release, get_design
from app.models.drawing_design import DrawingDesign
from app.services.drawing_geometry import custom_parameter_suggestions
from app.models.product import Product
from app.models.user import User


def main():
    root = Path(__file__).resolve().parents[1]
    isolated = root / '.uat' / 'reference_copy_20260921.sqlite3'
    if not isolated.is_file() or isolated.is_symlink() or isolated.resolve().parent != (root / '.uat').resolve():
        raise RuntimeError('Expected explicit local isolated reference copy')
    os.environ['ERP_FILE_STORAGE_DIR'] = str(root / '.uat' / 'reference_files')
    engine = create_engine(f'sqlite:///{isolated.as_posix()}')
    result = {'database': str(isolated), 'storage': os.environ['ERP_FILE_STORAGE_DIR'], 'checks': []}
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.role == 'admin').order_by(User.id))
        if user is None:
            raise RuntimeError('No admin in isolated copy')
        for code in ['21301204', '21301023', '21301634']:
            product = db.scalar(select(Product).where(Product.product_code == code, Product.customer_id == 5))
            if product is None:
                raise RuntimeError(f'Missing source reference {code}')
            params = dict(top_cover_mm=308,bottom_cover_mm=308,top_fold_mm=28,bottom_fold_mm=28,
                          left_fold_mm=25,right_fold_mm=25,left_wing_mm=40,right_wing_mm=40) if code == '21301634' else {}
            if code == '21301204':
                params = {**custom_parameter_suggestions(product.length_mm, product.width_mm, product.height_mm),
                          'top_fold_mm': 28, 'bottom_fold_mm': 28, 'left_wing_mm': 40, 'right_wing_mm': 40}
            approval_suffix = '-approved-corners-20260922' if code in ('21301204', '21301634') else ''
            request_key = f'reference-copy-save-{code}' + approval_suffix
            existing = db.get(DrawingDesign, product.id)
            saved = get_design(product.id, db, user) if existing and existing.last_save_key == request_key else save_design(product.id, DesignWrite(expected_product_version=product.version,
                          expected_design_version=existing.version if existing else None,
                          template_key='custom_21301634_v1', parameters=params,
                          idempotency_key=request_key,
                          thickness_mm=7 if code != '21301023' else 3,
                          thickness_source='工厂默认AB瓦7mm' if code != '21301023' else '工厂默认B瓦3mm'), db, user)
            check = {'product': code, 'product_id': product.id, 'draft_version': saved['draft']['version'],
                     'geometry_ready': saved['geometry_ready'], 'geometry_error': saved['geometry_error'],
                     'source_product_version': product.version}
            if code in ('21301634', '21301204'):
                released = publish_design(product.id, PublishWrite(expected_product_version=product.version,
                         expected_design_version=saved['draft']['version'], idempotency_key=f'reference-copy-publish-{code}{approval_suffix}'), db, user)
                check['isolated_release'] = released
                check['pdf_exists'] = Path(download_release(product.id, released['id'], db, user).path).is_file()
                check['basis'] = '21301634参考稿' if code == '21301634' else '2026-09-22用户确认620×470×26/28及本款40mm侧翼；不据采购尺寸反推'
                check['sample_shape_confirmed'] = True
                check['production_approved'] = False
                release_manifest = json.loads(db.execute(text('SELECT manifest_json FROM drawing_releases WHERE id=:id'), {'id': released['id']}).scalar_one())
                check['cut_segments'] = len(release_manifest['geometry']['cut'])
                check['net_dimensions_mm'] = [release_manifest['geometry']['width_mm'],release_manifest['geometry']['height_mm']]
                assert check['cut_segments'] == 12
                assert check['net_dimensions_mm'] == (['752','996'] if code == '21301204' else ['1295','1287'])
            else:
                assert not saved['geometry_ready'], 'Missing segments must not be inferred'
                check['basis'] = '真实主数据；结构分段未证明，未发布'
            result['checks'].append(check)
    engine.dispose()
    target = root / 'output/drawing_v2_uat_20260921/reference_uat_result.json'
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
