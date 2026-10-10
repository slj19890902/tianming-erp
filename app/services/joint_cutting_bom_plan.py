"""Frozen joint-cut planning over real BOM identities, without stock writes.

This is an explicit candidate contract, not an enabled procurement/receipt API.
Callers must authorize and CAS-lock the compiled master before persisting it.
Paper stock and output stock must already be qualified by the ledger adapter.
"""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json

from app.services.joint_cutting_contract import (
    _dimension, _encoded, _fail, _integer, _quantities,
    freeze_cutting_group, load_cutting_group, plan_cutting_group,
)
from app.services.multilevel_bom_plan import plan_bom, plan_assembly
from app.services.multilevel_bom_snapshot import dump_graph, load_graph, _unique_pairs
from app.services.sheet_cutting_settings import component_settings


_PAPER_FIELDS = (
    'material_id', 'material', 'supplier_name', 'layer_count', 'flute_type',
    'crease_type', 'crease_left_mm', 'crease_middle_mm', 'crease_right_mm', 'report_notes',
)
_BINDING_FIELDS = {'frozen_group', 'paper_identity', 'output_versions'}
_MAX_BYTES = 2_000_000


def _paper(snapshot):
    # Every source field is captured, including intentional NULL. Never fill
    # an old order from the current master or match on a material code alone.
    result = {}
    for field in _PAPER_FIELDS:
        value = getattr(snapshot, 'snapshot_component_' + field)
        result[field] = format(value.normalize(), 'f') if isinstance(value, Decimal) else value
    _validate_paper(result)
    return result


def _validate_paper(paper):
    if type(paper) is not dict or set(paper) != set(_PAPER_FIELDS):
        _fail('共同模切纸板身份字段不完整')
    _integer(paper['material_id'], '纸板材质身份', 1)
    _integer(paper['layer_count'], '纸板层数', 1)
    for key in ('material', 'flute_type'):
        if type(paper[key]) is not str or not paper[key].strip():
            _fail('共同模切纸板材质或楞型缺失')
    for key in ('supplier_name', 'crease_type', 'report_notes'):
        if paper[key] is not None and type(paper[key]) is not str:
            _fail('共同模切纸板文字依据无效')
    for key in ('crease_left_mm', 'crease_middle_mm', 'crease_right_mm'):
        if paper[key] is not None:
            if type(paper[key]) not in (int, str):
                _fail('共同模切压线依据无效')
            try:
                value = Decimal(paper[key])
                if not value.is_finite() or value < 0 or value > 100000:
                    raise ValueError()
            except (ValueError, ArithmeticError):
                _fail('共同模切压线依据无效')


def _validate_bindings(graph, bindings):
    nodes, children, _ = graph.validated()
    if type(bindings) is not list or not 1 <= len(bindings) <= 99:
        _fail('共同模切冻结组数量无效')
    assigned, keys = set(), set()
    for binding in bindings:
        if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
            _fail('共同模切冻结依据字段无效')
        group = load_cutting_group(binding['frozen_group'])
        if group['customer_id'] != graph.customer_id or group['key'] in keys:
            _fail('共同模切客户不一致或组身份重复')
        keys.add(group['key'])
        _validate_paper(binding['paper_identity'])
        versions = binding['output_versions']
        if type(versions) is not list or len(versions) != len(group['outputs']):
            _fail('共同模切零件版本依据不完整')
        expected_versions = []
        for output in group['outputs']:
            pid = output['product_id']
            node = nodes.get(pid)
            if pid in assigned or node is None:
                _fail('共同模切零件重复归组或不属于冻结BOM')
            assigned.add(pid)
            if (node.source != 'manufactured' or node.unit != '片' or children[pid]
                    or len(node.routes) != 1 or node.routes[0].key != 'whole'
                    or node.routes[0].pieces_per_unit != 1):
                _fail('共同模切产出须为独立单片自制零件')
            if node.routes[0].pieces_per_sheet != (
                    group['length_parts'] * group['width_parts'] * output['quantity_per_input']):
                _fail('共同模切产出与冻结零件开料换算不一致')
            expected_versions.append({'product_id': pid, 'version': node.version})
        if versions != expected_versions or any(
                type(r.get('product_id')) is not int or type(r.get('version')) is not int
                for r in versions if isinstance(r, dict)):
            _fail('共同模切零件版本与冻结BOM不一致')
    return assigned


def freeze_joint_bom(compiled, groups):
    """Bind explicit patterns to detached, versioned compiler output.

    No database lookup: later master edits must not rewrite the captured job.
    This function never changes the ordinary BOM graph wire format.
    """
    graph = compiled.graph
    nodes, _, _ = graph.validated()
    if type(groups) not in (list, tuple):
        _fail('共同模切配置列表无效')
    snapshots = {row.component_product_id: row for row in compiled.snapshots}
    if len(snapshots) != len(compiled.snapshots) or set(snapshots) != set(nodes):
        _fail('BOM冻结子件缺失或重复')
    from app.services.multilevel_bom_orders import validate_compiled_order_rows
    try:
        validate_compiled_order_rows(graph, compiled.snapshots)
    except ValueError as error:
        _fail(str(error))
    bindings = []
    for raw in groups:
        frozen = freeze_cutting_group(raw)
        group = frozen['group']
        papers, versions = [], []
        for output in group['outputs']:
            pid = output['product_id']
            if pid not in nodes:
                _fail('共同模切产出不属于当前BOM')
            row = snapshots[pid]
            if (type(row.component_product_version) is not int
                    or row.component_product_version != nodes[pid].version
                    or type(row.parent_product_version) is not int
                    or row.parent_product_version != nodes[graph.root_id].version):
                _fail('共同模切子件快照版本与BOM不一致')
            if row.is_die_cut is not True or row.snapshot_mold_tool_id != group['mold_tool_id']:
                _fail('共同模切模具与子件快照不一致')
            if row.spare_sheet_quantity != 0:
                _fail('共同模切加放须在组上明确，不能重复叠加子件加放')
            if (_dimension(row.snapshot_component_report_length_mm) != group['theory_length_mm']
                    or _dimension(row.snapshot_component_report_width_mm) != group['theory_width_mm']):
                _fail('共同模切投入尺寸与冻结子件不一致')
            setting = component_settings(row.sheet_cutting_settings_snapshot)
            if (setting is None or not setting.is_die_cut
                    or (setting.length_parts, setting.width_parts, setting.mold_count) != (
                        group['length_parts'], group['width_parts'], output['quantity_per_input'])):
                _fail('共同模切需要明确且一致的冻结分切及出片口径')
            papers.append(_paper(row))
            versions.append({'product_id': pid, 'version': nodes[pid].version})
        if any(paper != papers[0] for paper in papers[1:]):
            _fail('同次模切的零件必须来自同一纸板材质、楞型及压线依据')
        bindings.append({'frozen_group': frozen, 'paper_identity': papers[0],
                         'output_versions': versions})
    bindings.sort(key=lambda b: b['frozen_group']['group']['key'])
    _validate_bindings(graph, bindings)
    body = {'schema_version': 1, 'kind': 'joint_cut_bom',
            'bom_document': dump_graph(graph), 'bindings': bindings}
    body['sha256'] = hashlib.sha256(_encoded(body)).hexdigest()
    document = _encoded(body).decode('utf-8')
    load_joint_bom(document)
    return document


def load_joint_bom(document):
    """Read saved bytes and validate before calculation, never current master."""
    if type(document) is not str or len(document.encode('utf-8')) > _MAX_BYTES:
        _fail('共同模切冻结文件格式或大小无效')
    try:
        data = json.loads(document, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError, RecursionError):
        _fail('共同模切冻结文件不是有效JSON')
    if type(data) is not dict or set(data) != {
            'schema_version', 'kind', 'bom_document', 'bindings', 'sha256'}:
        _fail('共同模切冻结文件字段无效')
    if type(data['schema_version']) is not int or data['schema_version'] != 1 or data['kind'] != 'joint_cut_bom':
        _fail('不支持的共同模切冻结文件版本')
    checksum = data.pop('sha256')
    if checksum != hashlib.sha256(_encoded(data)).hexdigest():
        _fail('共同模切冻结文件校验失败')
    graph = load_graph(data['bom_document'])
    _validate_bindings(graph, data['bindings'])
    return graph, data['bindings']


def plan_joint_bom(document, quantity, *, eligible_stock=None, eligible_pieces=None):
    graph, bindings = load_joint_bom(document)
    ordinary = plan_bom(graph, quantity, eligible_stock=eligible_stock,
                        eligible_pieces=eligible_pieces)
    materials = {row.product_id: row for row in ordinary.materials}
    assigned, operations = set(), []
    for binding in bindings:
        group = load_cutting_group(binding['frozen_group'])
        ids = {o['product_id'] for o in group['outputs']}
        assigned.update(ids)
        needs = {pid: materials[pid].required_pieces - materials[pid].credited_pieces for pid in ids}
        operations.append({**plan_cutting_group(group, needs),
                           'paper_identity': binding['paper_identity']})
    return {'products': [asdict(row) for row in ordinary.products],
            'operations': operations,
            'ordinary_materials': [asdict(row) for row in ordinary.materials if row.product_id not in assigned],
            'picking': list(ordinary.picking)}


def plan_joint_batch(document, group_key, *, input_sheets, good_outputs):
    """Actual small-sheet input and individually counted good/rejected pieces.

    Does not reserve/debit paper, create lots, or infer an assembly completion.
    Zero good output is a valid all-scrap batch; an overrun needs a separate
    approved contract, never silent conversion of one shape into another.
    """
    _, bindings = load_joint_bom(document)
    group = next((load_cutting_group(b['frozen_group']) for b in bindings
                  if b['frozen_group']['group']['key'] == group_key), None)
    if group is None:
        _fail('模切加工组不属于冻结配方')
    _integer(input_sheets, '实际投入小片张数', 1)
    ids = {row['product_id'] for row in group['outputs']}
    good = _quantities(good_outputs, ids, '实际合格产出', require_all=True)
    rows = []
    for output in group['outputs']:
        pid = output['product_id']
        theoretical = input_sheets * output['quantity_per_input']
        if good[pid] > theoretical:
            _fail('实收零件高于本模切配方理论产出，须先核对出片口径')
        rows.append({'product_id': pid, 'theoretical_output': theoretical,
                     'good_output': good[pid], 'rejected_output': theoretical - good[pid]})
    return {'group_key': group_key, 'input_sheets': input_sheets, 'outputs': rows}


def plan_joint_assembly(document, quantity, *, eligible_stock, fulfilled_stock=None):
    graph, _ = load_joint_bom(document)
    return plan_assembly(graph, quantity, eligible_stock=eligible_stock,
                         fulfilled_stock=fulfilled_stock)
