"""Supplier-facing equivalence; internal source/yield keys remain unchanged.

Length/width are ordered axes, never rotated. Versioned cutting contracts
describe the delivered sheet and subsequent factory cutting/mold yield. Only
explicit supplier work requirements belong in this projection. Incomplete
contracts stay separate with field-level reasons, never silently guessed.
"""
import json
from decimal import Decimal, InvalidOperation

from app.services.sheet_cutting_contract import SheetCuttingContract, SheetCuttingContractError


def delivery_contract(supplier_name, spec, *, typed_snapshot=None):
    source = typed_snapshot or {}
    issues = []
    def text(value):
        return str(value or '').strip()
    def dimension(field):
        try:
            value = Decimal(str(spec.get(field)))
            if not value.is_finite() or value <= 0:
                raise ValueError
            return format(value.normalize(), 'f')
        except (InvalidOperation, ValueError):
            issues.append(field)
            return None
    result = {key:text(spec.get(key)) for key in (
        'material_code', 'flute_type', 'crease_type', 'remark')}
    result.update(supplier_name=text(supplier_name), material_id=spec.get('material_id'),
        layer_count=spec.get('layer_count'), report_length_mm=dimension('report_length_mm'),
        report_width_mm=dimension('report_width_mm'), unit='sheets',
        # A null note means the existing ordered length/width convention.
        direction_note=text(source.get('direction_note') or spec.get('direction_note')),
        supplier_work=source.get('supplier_work') or spec.get('supplier_work') or 'delivered_sheet')
    for key in ('supplier_name','material_code','flute_type','layer_count','crease_type'):
        if not result[key]:
            issues.append(key)
    crease = result['crease_type']
    sheet_type = source.get('sheet_type') or ('net_sheet' if crease == '净料' else
        'raw_board' if crease == '毛片' else 'creased_sheet' if crease == '压线' else None)
    if not sheet_type:
        issues.append('sheet_type')
    result['sheet_type'] = sheet_type
    for key in ('crease_left_mm','crease_middle_mm','crease_right_mm'):
        result[key] = spec.get(key) if crease == '压线' else None
        if crease == '压线' and result[key] is None:
            issues.append(key)
    snapshot = spec.get('sheet_cutting_snapshot')
    if snapshot:
        try:
            contract = SheetCuttingContract.from_snapshot(snapshot)
            result['supplier_cutting'] = dict(length_parts=contract.length_parts,
                width_parts=contract.width_parts,
                theoretical_length_mm=format(contract.theoretical_length_mm.normalize(), 'f'),
                theoretical_width_mm=format(contract.theoretical_width_mm.normalize(), 'f'))
            supplied = tuple(format(v.normalize(), 'f') for v in contract.supplier_size_mm)
            if supplied != (result['report_length_mm'],result['report_width_mm']):
                issues.append('sheet_cutting_snapshot.supplier_size_mm')
        except SheetCuttingContractError as exc:
            issues.append(f'sheet_cutting_snapshot: {exc}')
    else:
        # Unversioned old cutting text may mean supplier work. Keep it in the
        # key, rather than reinterpreting it as internal mold yield.
        result['legacy_cutting_mode'] = text(spec.get('cutting_mode'))
        if typed_snapshot is not None:
            issues.append('sheet_cutting_snapshot')
    key = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',',':'))
    return key, result, sorted(set(issues))
