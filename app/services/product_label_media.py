"""Paper geometry and immutable media variants for product-label print jobs.

Media never participates in the business plan fingerprint. It is instead bound
to the print request and frozen payload, preserving old requests byte-for-byte.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PRESETS = {
    'roll_40x30': (40, 30, '40×30 mm'),
    'roll_40x80': (40, 80, '40×80 mm'),
    'roll_80x40': (80, 40, '80×40 mm'),
    'roll_65x45': (65, 45, '65×45 mm'),
    'a4_4up': (210, 297, 'A4 四格（2×2）'),
}


class ProductLabelPrintMedia(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    preset: Literal['roll_40x30', 'roll_40x80', 'roll_80x40', 'roll_65x45', 'a4_4up', 'custom']
    width_mm: float | None = Field(default=None, ge=20, le=300)
    height_mm: float | None = Field(default=None, ge=20, le=420)
    margin_top_mm: float = Field(default=0, ge=0, le=50)
    margin_right_mm: float = Field(default=0, ge=0, le=50)
    margin_bottom_mm: float = Field(default=0, ge=0, le=50)
    margin_left_mm: float = Field(default=0, ge=0, le=50)
    gap_x_mm: float = Field(default=0, ge=0, le=50)
    gap_y_mm: float = Field(default=0, ge=0, le=50)
    start_position: int = Field(default=1, ge=1, le=4, strict=True)
    # Clients may round-trip a normalized receipt. These are always recomputed.
    schema_version: Literal[1] = 1
    label_width_mm: float | None = Field(default=None, ge=20, le=300)
    label_height_mm: float | None = Field(default=None, ge=20, le=420)
    rows: int | None = Field(default=None, ge=1, le=2, strict=True)
    columns: int | None = Field(default=None, ge=1, le=2, strict=True)

    @field_validator('width_mm', 'height_mm', 'margin_top_mm', 'margin_right_mm',
                     'margin_bottom_mm', 'margin_left_mm', 'gap_x_mm', 'gap_y_mm',
                     'label_width_mm', 'label_height_mm', mode='before')
    @classmethod
    def numeric_only(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float, Decimal))):
            raise ValueError('纸张尺寸必须为有限数字')
        return value

    @field_validator('schema_version', mode='before')
    @classmethod
    def schema_integer(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError('纸张版本无效')
        return value

    @model_validator(mode='after')
    def geometry(self):
        if self.preset == 'custom':
            if self.width_mm is None or self.height_mm is None:
                raise ValueError('自定义纸张必须填写宽和高')
            width, height = self.width_mm, self.height_mm
        else:
            width, height, _ = PRESETS[self.preset]
            if ((self.width_mm is not None and self.width_mm != width)
                    or (self.height_mm is not None and self.height_mm != height)):
                raise ValueError('预设纸张尺寸不一致，请选择自定义纸张')
        cells = 2 if self.preset == 'a4_4up' else 1
        if cells == 1 and (self.gap_x_mm or self.gap_y_mm or self.start_position != 1):
            raise ValueError('卷式和自定义单张标签不使用格间距或起始格')
        if ((width - self.margin_left_mm - self.margin_right_mm - self.gap_x_mm) / cells < 20
                or (height - self.margin_top_mm - self.margin_bottom_mm - self.gap_y_mm) / cells < 20):
            raise ValueError('扣除边距和间隔后，每张标签的宽高至少为20毫米')
        return self


def normalize_print_media(value):
    if value is None:
        return None
    try:
        model = value if isinstance(value, ProductLabelPrintMedia) else ProductLabelPrintMedia.model_validate(value)
    except (ValueError, TypeError) as error:
        from app.services.production_label_operations import ProductionLabelOperationError
        raise ProductionLabelOperationError('标签纸张设置无效：' + str(error), 422) from error
    raw = model.model_dump(exclude={'label_width_mm', 'label_height_mm', 'rows', 'columns'})
    for field in ('margin_top_mm', 'margin_right_mm', 'margin_bottom_mm', 'margin_left_mm', 'gap_x_mm', 'gap_y_mm'):
        raw[field] = float(raw[field])
    width, height = ((model.width_mm, model.height_mm) if model.preset == 'custom'
                     else PRESETS[model.preset][:2])
    rows = columns = 2 if model.preset == 'a4_4up' else 1
    def floor3(number):
        return float(number.quantize(Decimal('.001'), rounding=ROUND_FLOOR))
    # Decimal avoids binary 19.999999 for an otherwise exact valid boundary.
    dec = lambda number: Decimal(str(number))
    label_width = (dec(width) - dec(model.margin_left_mm) - dec(model.margin_right_mm)
                   - dec(model.gap_x_mm) * (columns - 1)) / columns
    label_height = (dec(height) - dec(model.margin_top_mm) - dec(model.margin_bottom_mm)
                    - dec(model.gap_y_mm) * (rows - 1)) / rows
    raw.update(width_mm=float(width), height_mm=float(height), rows=rows, columns=columns,
               label_width_mm=floor3(label_width), label_height_mm=floor3(label_height))
    return raw


def bind_print_media(payload, print_media):
    """Append only for explicit media; old request hashes remain unchanged."""
    normalized = normalize_print_media(print_media)
    if normalized is not None:
        payload = dict(payload, print_media=normalized)
    return payload


def media_catalog():
    return dict(schema_version=1, presets=[dict(preset=key, name=label,
        **{k: v for k, v in normalize_print_media({'preset': key}).items() if k != 'preset'})
        for key, (_, _, label) in PRESETS.items()],
        custom=dict(width_min_mm=20, width_max_mm=300, height_min_mm=20, height_max_mm=420),
        margin_max_mm=50, gap_max_mm=50, minimum_label_dimension_mm=20)


def assert_media_reprint_source(db, package):
    """Validate a historical variant against immutable, already printed evidence."""
    from app.services.production_label_operations import ProductionLabelOperationError as Error, _load_job_package, _canonical_hash
    source = package.get('media_reprint_source')
    if not isinstance(source, dict) or source.get('kind') not in ('packaging', 'finished'):
        raise Error('换纸补打缺少原打印凭证')
    if source['kind'] == 'finished':
        from app.services.finished_product_labels import read_job
        original, frozen = read_job(db, source.get('id'))
    else:
        from app.models.production_label_print import ProductionPackagingLabelPrintJob
        original = db.get(ProductionPackagingLabelPrintJob, source.get('id'))
        if original is None:
            raise Error('原标签打印作业不存在', 404)
        frozen = _load_job_package(original, db)
    if original.status != 'printed' or original.payload_hash != source.get('payload_hash'):
        raise Error('原标签打印凭证已变化，已停止换纸补打')
    # The content and quantities must remain exactly the original snapshot.
    excluded = {'print_media', 'media_reprint_source'}
    if _canonical_hash({k:v for k,v in frozen.items() if k not in excluded}) != _canonical_hash(
            {k:v for k,v in package.items() if k not in excluded}):
        raise Error('换纸补打的产品内容或数量与原凭证不一致')
    return original, frozen


def prepare_media_reprint(db, *, kind, job_id, print_media, key, batch_hash, operator_id):
    """Clone one printed snapshot; caller applies access control and audit atomically."""
    from sqlalchemy import select
    from app.services.production_label_operations import (
        ProductionLabelOperationError as Error, _canonical_hash, _canonical_json,
        _load_job_package, _validate_job_task_links,
    )
    from app.models.production_label_print import ProductionPackagingLabelPrintJob, ProductionPackagingLabelPrintJobTask
    from app.models.finished_product_label import FinishedProductLabelPrintJob
    model = FinishedProductLabelPrintJob if kind == 'finished' else ProductionPackagingLabelPrintJob
    if kind not in ('finished', 'packaging'):
        raise Error('标签作业类型无效', 422)
    original = db.get(model, job_id)
    if original is None:
        raise Error('原标签打印作业不存在', 404)
    if kind == 'finished':
        from app.services.finished_product_labels import read_job
        original, package = read_job(db, job_id)
    else:
        package = _load_job_package(original, db)
    if original.status != 'printed':
        raise Error('仅已登记实际打印的标签可以换纸补打')
    request_hash = _canonical_hash(dict(batch_hash=batch_hash, kind=kind, id=job_id,
        payload_hash=original.payload_hash, print_media=normalize_print_media(print_media)))
    repeated = db.scalar(select(model).where(model.idempotency_key == key))
    if repeated is not None:
        if repeated.request_hash != request_hash or repeated.operator_id != operator_id:
            raise Error('幂等键已用于不同的换纸补打请求')
        if kind == 'finished':
            _, frozen = read_job(db, repeated.id)
        else:
            frozen = _load_job_package(repeated, db)
        assert_media_reprint_source(db, frozen)
        return repeated, frozen, True
    frozen = bind_print_media(deepcopy(package), print_media)
    frozen['media_reprint_source'] = dict(kind=kind, id=job_id, payload_hash=original.payload_hash)
    args = dict(idempotency_key=key, request_hash=request_hash, operator_id=operator_id,
        plan_fingerprint=original.plan_fingerprint, payload_json=_canonical_json(frozen),
        payload_hash=_canonical_hash(frozen), status='prepared')
    if kind == 'finished':
        args['inventory_lot_id'] = original.inventory_lot_id
    else:
        args.update(supplier_order_id=original.supplier_order_id,
            material_requisition_id=original.material_requisition_id,
            delivery_id=original.delivery_id, template_version=original.template_version)
    job = model(**args)
    db.add(job)
    db.flush()
    if kind == 'packaging':
        for link in _validate_job_task_links(db, original, package):
            db.add(ProductionPackagingLabelPrintJobTask(print_job_id=job.id,
                production_task_id=link.production_task_id,
                production_task_version=link.production_task_version, product_id=link.product_id,
                product_version=link.product_version, template_version=link.template_version,
                snapshot_json=link.snapshot_json))
        db.flush()
    assert_media_reprint_source(db, frozen)
    return job, frozen, False
