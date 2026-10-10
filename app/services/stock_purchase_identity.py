"""Purchase-owned production identity. Reading legacy evidence never rewrites it."""
import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.product import Product
from app.models.material import Material
from app.services.finished_stock_identity import FIELDS, product_basis, _product_basis
from app.services.warehouse_inventory import WarehouseInventoryError


VIEW_FIELDS = tuple(dict.fromkeys((*FIELDS, 'customer_id', 'unit', 'length_mm', 'width_mm',
    'height_mm', 'report_length_mm', 'report_width_mm', 'mold_tool_id', 'die_cut_path',
    'supply_mode', 'surface_paper_type', 'default_material_code', 'legacy_material_text',
    'flute_type', 'product_code', 'product_name')))
PURCHASE_FIELDS = ('customer_id', 'material_code_snapshot', 'layer_count', 'flute_type',
    'report_length_mm', 'report_width_mm', 'crease_type', 'crease_left_mm',
    'crease_middle_mm', 'crease_right_mm', 'component_type', 'pieces_per_box',
    'stock_yield_per_sheet')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _signature(item):
    def value(k):
        v = getattr(item, k, None)
        if v is not None and (k.endswith('_mm') or k in ('customer_id', 'layer_count', 'pieces_per_box', 'stock_yield_per_sheet')):
            return format(Decimal(str(v)).normalize(), 'f')
        return str(v)
    return hashlib.sha256(encode({k: value(k) for k in PURCHASE_FIELDS}).encode()).hexdigest()


def capture(product, item):
    """Called only while saving a new demand, before subsequent master edits."""
    fields = {k: getattr(product, k, None) for k in VIEW_FIELDS}
    if product.material is not None:
        from app.services.paper_color import material_face
        from sqlalchemy.orm import object_session
        fields['surface_paper_type'] = material_face(object_session(product), product.material)
    return json.loads(encode(dict(schema=1, product_id=product.id, customer_id=product.customer_id,
        product_version=product.version, fields=fields, physical_basis=product_basis(product),
        purchase_signature=_signature(item), source='purchase_snapshot')))


def _view(identity, item):
    fields = identity['fields']
    return Product(**{k: fields.get(k) for k in VIEW_FIELDS}, id=identity['product_id'],
        material=Material(code=item.material_code_snapshot, layer_count=item.layer_count,
                          flute_type=item.flute_type, supplier_name='',
                          is_white_face=fields.get('surface_paper_type') == 'white'))


def resolve(db, item, product=None):
    product = product or db.get(Product, item.reference_product_id or item.product_id)
    identity = json.loads(item.production_snapshot_json) if item.production_snapshot_json else None
    if identity is not None:
        if (identity.get('schema') != 1 or identity.get('product_id') != product.id
                or identity.get('customer_id') != item.customer_id
                or identity.get('purchase_signature') != _signature(item)
                or json.loads(identity.get('physical_basis') or '{}').get('product_id') != product.id):
            raise WarehouseInventoryError('报料冻结身份与原单不一致，请核对', 409)
        return identity
    # Legacy purchases already store material, dimensions and conversion. Recover
    # missing finished-product facts only from the append-only, checksummed history.
    revision = db.scalar(select(MasterDataObjectVersion).where(
        MasterDataObjectVersion.object_type == 'product',
        MasterDataObjectVersion.object_id == product.id,
        MasterDataObjectVersion.created_at <= item.created_at).order_by(
        MasterDataObjectVersion.created_at.desc(), MasterDataObjectVersion.version.desc()).limit(1)) if item.created_at else None
    if revision:
        if hashlib.sha256(revision.snapshot_json.encode()).hexdigest() != revision.snapshot_sha256:
            raise WarehouseInventoryError('报料时产品历史校验失败，请核对原单', 409)
        fields = json.loads(revision.snapshot_json)
        if fields.get('customer_id') != item.customer_id:
            raise WarehouseInventoryError('报料时产品客户范围与原单不一致', 409)
        identity = dict(schema=1, product_id=product.id, customer_id=item.customer_id,
            product_version=revision.version, fields={k: fields.get(k) for k in VIEW_FIELDS},
            purchase_signature=_signature(item), source='master_history', history_id=revision.id)
        identity['physical_basis'] = _product_basis(_view(identity, item))
        return identity
    if product.updated_at and item.created_at and product.updated_at > item.created_at:
        raise WarehouseInventoryError('旧报料缺少当时产品规格工艺凭据，请核对原加工身份', 409)
    return capture(product, item)


def validate_material(db, item, lot, product):
    """Compare physical receipt to its purchase, never to today's master size."""
    from app.services.warehouse_inventory import normalize_material_code
    from app.services.warehouse_goods import qualification_issues
    detail = lot.semi_finished_detail
    if (not detail or detail.owner_customer_id != item.customer_id
            or detail.flute_type != item.flute_type or detail.layer_count != item.layer_count
            or normalize_material_code(detail.material_code_snapshot) != normalize_material_code(item.material_code_snapshot)
            or detail.board_length_mm != item.report_length_mm or detail.board_width_mm != item.report_width_mm
            or detail.pieces_per_box != item.pieces_per_box or detail.stock_yield_per_sheet != item.stock_yield_per_sheet
            or detail.component_type != item.component_type):
        raise WarehouseInventoryError('实收材料与原报料冻结规格或换算不匹配，请核对收料记录', 409)
    if detail.sheet_type != item.sheet_type or (detail.sheet_type == 'creased_sheet' and any(
            getattr(detail, k) != getattr(item, k) for k in ('crease_type', 'crease_left_mm', 'crease_middle_mm', 'crease_right_mm'))):
        raise WarehouseInventoryError('实收材料压线形态与原报料不一致，请核对', 409)
    identity = resolve(db, item, product)
    issues = qualification_issues(db, lot, _view(identity, item), expected_material_code=item.material_code_snapshot)
    if issues:
        raise WarehouseInventoryError('材料不能用于该产品：' + '；'.join(issues), 409)
    return identity


def delivery_fields(allocations):
    """A selected old production lot keeps its dimensions on the delivery note."""
    if not any(row['lot'].source_ref_type == 'stock_preparation' for row in allocations):
        return None
    from app.services.product_specification import dimension_specification
    facts = []
    for row in allocations:
        detail = row['lot'].finished_detail
        basis = json.loads(detail.physical_basis_json or '{}')
        spec = basis.get('spec') or dimension_specification(detail.length_mm, detail.width_mm, detail.height_mm)
        facts.append((spec, detail.material_code_snapshot, detail.flute_type_snapshot))
    if len(set(facts)) != 1:
        raise WarehouseInventoryError('所选批次冻结规格不同，请按实物规格分行开送货单', 409)
    detail = allocations[0]['lot'].finished_detail
    return dict(product_code_snapshot=detail.inventory_code_snapshot, product_name_snapshot=detail.product_name_snapshot,
                specification_snapshot=facts[0][0])
