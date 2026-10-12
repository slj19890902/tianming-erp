"""Read-only comparison of an authenticated retained backup and stopped preview.

This is NOT an approval, credential authenticator, activation or source audit.
The protected caller must authenticate the original backup/receipts/scripts,
hold the original maintenance lock and file leases, and independently compare
all plaintext credentials. In particular the existing id=1 mailbox rewrap is
normalized here; accepting this comparison never verifies that credential.
Supplied historical receipts prove byte/shape consistency only: this module
cannot turn caller-provided bytes into a root approval.

Only successful current-schema LOGIN rows are supported. Failed/throttled
login, logout, user changes and unknown historical formats fail closed. The
complete private manifest must be retained; public hashes are not a substitute.
No database/file writes, repair, arbitrary SQL or ignore-table options exist.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct

from desktop_assistant.factory_target_contract import canonical, digest, _keys, _hash, _time
from desktop_assistant.factory_activation_contract import _snapshot

LOG_COLUMNS = ('id', 'user_id', 'action', 'resource', 'details', 'ip_address', 'created_at',
    'username', 'role', 'entity_type', 'entity_id', 'description', 'user_agent', 'extra_json',
    'event_category', 'result', 'source', 'module_code', 'action_code', 'actor_user_id_snapshot',
    'operator_name_snapshot', 'object_ref', 'customer_id_snapshot', 'customer_name_snapshot',
    'request_id', 'batch_id', 'schema_version')
HOME_FIELDS = frozenset(('ERP_ENVIRONMENT', 'ERP_HOME_REHEARSAL', 'ERP_PRODUCTION_TRANSPORT',
    'ERP_BIND_HOST', 'ERP_PORT', 'ERP_ALLOWED_ORIGINS', 'ERP_TRUSTED_HOSTS', 'ERP_TRUSTED_PROXY_IPS',
    'ERP_SESSION_COOKIE_SECURE', 'ERP_LAN_HTTP_ORIGIN', 'ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS',
    'ERP_BROWSER_URL', 'ERP_HEALTH_URL', 'ERP_SHELF_LABEL_ORIGIN'))
LAN_FIELDS = frozenset(('ERP_TRUSTED_HOSTS', 'ERP_ALLOWED_ORIGINS',
    'ERP_SHELF_LABEL_ORIGIN', 'ERP_MOBILE_QR_ORIGIN'))
LIMIT = 65536


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _decode(raw):
    _require(isinstance(raw, bytes) and 0 < len(raw) <= LIMIT, '环境证据长度无效')
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, '环境证据含重复字段')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('环境证据含非有限数')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    _require(isinstance(value, dict), '环境证据必须是对象')
    return value


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _legacy_time(value):
    # The two original scripts used datetime.now(UTC).isoformat(), not v2 Z.
    _require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{6})?\+00:00', value), '旧回执时间格式未知')
    parsed = datetime.fromisoformat(value)
    _require(parsed.isoformat() == value, '旧回执时间不规范')
    return parsed


def _metadata(info):
    return tuple(getattr(info, key) for key in ('st_dev', 'st_ino', 'st_mode', 'st_uid',
        'st_gid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _inventory(root):
    root = Path(root).absolute()
    _require(root.resolve() == root and root.is_dir(), '共享根缺失或通过链接访问')
    result = {}; pending = [root]
    while pending:
        path = pending.pop(); before = path.lstat()
        _require(not stat.S_ISLNK(before.st_mode) and not getattr(before, 'st_file_attributes', 0) & 0x400,
                 '共享树含链接')
        name = path.relative_to(root).as_posix()
        if stat.S_ISDIR(before.st_mode):
            result[name] = (_metadata(before), None)
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        else:
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, '共享树含特殊文件或硬链接')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as inp:
                _require(_metadata(os.fstat(inp.fileno())) == _metadata(before), '读取前文件身份变化')
                h = hashlib.sha256()
                for block in iter(lambda: inp.read(1024 * 1024), b''):
                    h.update(block)
                _require(_metadata(os.fstat(inp.fileno())) == _metadata(before), '读取期间文件变化')
            _require(_metadata(path.lstat()) == _metadata(before), '读取后文件身份变化')
            result[name] = (_metadata(before), h.hexdigest())
    return result


def _offline(root):
    db = root / 'data/carton_erp.sqlite3'
    _require(db.is_file(), '缺少固定静态数据库')
    _require(not any(os.path.lexists(str(db) + s) for s in ('-wal', '-shm', '-journal')),
             '静态比较不接受任何数据库日志旁文件')
    return db


def _capture(root, recorded_root):
    # Reuse the full existing attachment/path/schema snapshot, without IO at import.
    from desktop_assistant.factory_cutover import capture_snapshot
    return capture_snapshot(root, recorded_root=recorded_root)


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _typed(value):
    if value is None: return ['null', None]
    if type(value) is int: return ['integer', str(value)]
    if type(value) is float:
        _require(math.isfinite(value), '非有限SQLite数值不受支持')
        return ['real', struct.pack('>d', value).hex()]
    if type(value) is str: return ['text', value]
    if type(value) is bytes: return ['blob', value.hex()]
    raise ValueError('未知SQLite存储类型')


def _rows(db, table, columns):
    return list(db.execute('SELECT ' + ','.join(map(_quote, columns)) + ' FROM ' + _quote(table)))


def _typed_rows(rows):
    return [[_typed(v) for v in row] for row in rows]


def _multiset(rows):
    return Counter(canonical(row) for row in _typed_rows(rows))


def _optional(value, limit):
    _require(value is None or type(value) is str, '登录用户字段类型未知')
    if value is None: return None
    value = value.strip()
    _require(len(value) <= limit, '不支持需要截断的登录用户字段')
    # Such names go through application redaction. Reject rather than guess it.
    _require(not re.search(r'(?i)(?:Bearer\s|\bsk-|\beyJ|(?:password|passwd|pwd|token|secret|api[_-]?key|authorization|cookie)\s*[:=]|://[^/\s]*@)', value),
             '不支持需要脱敏转换的登录用户字段')
    return value or None


def _login(row, users, lower, upper, old_requests):
    fixed = dict(action='LOGIN', action_code='LOGIN', resource='User', entity_type='user',
        description='用户登录', event_category='security', result='success', source='web',
        module_code='auth', schema_version=1)
    _require(all(type(row[k]) is type(v) and row[k] == v for k, v in fixed.items()), '仅支持成功LOGIN的精确结构')
    _require(all(row[k] is None for k in ('extra_json', 'customer_id_snapshot', 'customer_name_snapshot', 'batch_id')),
             '成功LOGIN含未知业务字段')
    identity = row['user_id']
    _require(type(identity) is int and identity in users, '登录关联用户不是保留的原用户')
    user = users[identity]
    _require(type(user['is_active']) is int and user['is_active'] == 1, '登录用户不是原启用用户')
    _require(all(type(row[k]) is int and row[k] == identity for k in ('entity_id', 'actor_user_id_snapshot')),
             '登录关联身份不一致')
    name, role = _optional(user['username'], 50), _optional(user['role'], 30)
    operator = _optional(user['display_name'] or user['real_name'] or user['username'], 100) or name
    _require(name is not None and role is not None and all(row[k] == v for k, v in
        dict(username=name, role=role, object_ref=name, operator_name_snapshot=operator).items()), '登录身份快照与原用户不符')
    # Exact original serializer spelling rejects duplicate fields/NaN/extra data.
    _optional(user['username'], 2000); _optional(user['role'], 2000)
    details = json.dumps({'username': user['username'], 'role': user['role']}, ensure_ascii=False, sort_keys=True)
    _require(row['details'] == details, '登录details不是原精确两字段结构')
    request = row['request_id']
    _require(isinstance(request, str) and re.fullmatch(r'[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}', request)
             and request not in old_requests, '登录请求标识格式无效或重复')
    old_requests.add(request)
    for field, limit in (('ip_address', 64), ('user_agent', 256)):
        value = row[field]
        _require(value is None or (type(value) is str and 0 < len(value) <= limit and value.strip() == value),
                 '登录网络字段格式无效')
    value = row['created_at']
    _require(type(value) is str and re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', value), 'LOGIN时间不是SQLite UTC秒格式')
    stamp = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    # SQLite CURRENT_TIMESTAMP has whole-second precision. Compare at that
    # same resolution; never invent fractional precision or a timezone shift.
    _require(lower.replace(microsecond=0) <= stamp <= upper, 'LOGIN时间不在已批准预演窗口')


def _sequence(rows):
    result = {}
    for name, value in rows:
        _require(type(name) is str and name not in result and type(value) is int and 0 <= value < 2**63,
                 'sqlite_sequence含未知类型或重复条目')
        result[name] = value
    return result


def _slot(mapping, key):
    return {'present': key in mapping, 'value': mapping.get(key)}


def _compare_databases(baseline, target, recorded_roots, lower, upper):
    from desktop_assistant.source_paths import relative_source_path
    with closing(sqlite3.connect(_offline(baseline).as_uri() + '?mode=ro', uri=True)) as left, \
         closing(sqlite3.connect(_offline(target).as_uri() + '?mode=ro', uri=True)) as right:
        for db in (left, right):
            db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        def tables(db):
            return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
        table_sql = tables(left)
        _require(table_sql == tables(right), '数据库表结构变化')
        _require({'operation_logs', 'users'} <= table_sql.keys(), '缺少明确审计或用户结构')
        columns = {name: [r[1] for r in left.execute('PRAGMA table_xinfo(' + _quote(name) + ')')] for name in table_sql}
        _require(set(columns['operation_logs']) == set(LOG_COLUMNS) and len(columns['operation_logs']) == len(LOG_COLUMNS),
                 'operation_logs字段不是受支持的完整27列')
        _require({'id', 'username', 'role', 'display_name', 'real_name', 'is_active'} <= set(columns['users']), '用户结构缺少登录核验字段')
        left_rows = {}; right_rows = {}
        for table, names in columns.items():
            a, b = _rows(left, table, names), _rows(right, table, names)
            left_rows[table], right_rows[table] = a, b
            if table in ('operation_logs', 'sqlite_sequence'): continue
            def normalize(rows, recorded_root):
                result = []
                for original in rows:
                    row = list(original)
                    if table == 'email_intake_settings':
                        _require({'id', 'encrypted_secret'} <= set(names), '邮箱结构未知')
                        if type(row[names.index('id')]) is int and row[names.index('id')] == 1:
                            row[names.index('encrypted_secret')] = None
                    if table == 'pdf_order_training_samples':
                        _require({'file_path', 'file_sha256'} <= set(names), 'PDF结构未知')
                        index = names.index('file_path')
                        if row[index] not in (None, ''):
                            row[index] = relative_source_path(row[index], recorded_root).as_posix()
                    result.append(tuple(row))
                return result
            _require(_multiset(normalize(a, recorded_roots[0])) == _multiset(normalize(b, recorded_roots[1])),
                     '非预演事实发生变化：' + table)
        names = columns['operation_logs']
        def logs(rows):
            result = {}
            for values in rows:
                row = dict(zip(names, values)); identity = row['id']
                _require(type(identity) is int and 0 < identity < 2**63 and identity not in result, '日志主键类型或唯一性无效')
                result[identity] = row
            return result
        a, b = logs(left_rows['operation_logs']), logs(right_rows['operation_logs'])
        for identity, row in a.items():
            _require(identity in b and _typed_rows([tuple(row[k] for k in names)]) ==
                     _typed_rows([tuple(b[identity][k] for k in names)]), '原日志被删除或改变')
        added = sorted(set(b) - set(a)); old_max = max(a, default=0)
        _require(all(identity > old_max for identity in added), '新增日志复用了历史ID范围')
        users = {row[columns['users'].index('id')]: dict(zip(columns['users'], row)) for row in left_rows['users']}
        requests = {row['request_id'] for row in a.values() if row['request_id'] is not None}
        for identity in added: _login(b[identity], users, lower, upper, requests)
        present = 'sqlite_sequence' in columns
        if present: _require(columns['sqlite_sequence'] == ['name', 'seq'], 'sqlite_sequence结构未知')
        sa = _sequence(left_rows.get('sqlite_sequence', [])); sb = _sequence(right_rows.get('sqlite_sequence', []))
        auto = bool(re.search(r'\bAUTOINCREMENT\b', table_sql['operation_logs'], re.I))
        _require({k:v for k,v in sa.items() if k != 'operation_logs'} ==
                 {k:v for k,v in sb.items() if k != 'operation_logs'}, '其他序列发生变化')
        if auto:
            _require(present and (not a or sa.get('operation_logs', -1) >= old_max), '原AUTOINCREMENT序列缺失或倒退')
            expected = max(sa.get('operation_logs', 0), max(added, default=0))
            _require(_slot(sb, 'operation_logs') == ({'present': True, 'value': expected} if added else _slot(sa, 'operation_logs')),
                     '日志序列不是对应追加的精确推进')
            floor = max(old_max, sa.get('operation_logs', 0))
        else:
            _require('operation_logs' not in sa and 'operation_logs' not in sb, '非AUTOINCREMENT表出现日志序列')
            floor = old_max
        _require(added == list(range(floor + 1, floor + 1 + len(added))), 'LOGIN追加ID不是连续正常分配')
        before = _typed_rows([tuple(a[i][k] for k in names) for i in sorted(a)])
        after = _typed_rows([tuple(b[i][k] for k in names) for i in sorted(b)])
        appended = _typed_rows([tuple(b[i][k] for k in names) for i in added])
        sequence_before = _typed_rows(sorted(sa.items())); sequence_after = _typed_rows(sorted(sb.items()))
        summary = {'operation_logs': {'columns_sha256': digest(names), 'baseline_count': len(a), 'target_count': len(b),
            'baseline_sha256': digest(before), 'target_sha256': digest(after), 'appended_count': len(added),
            'appended_sha256': digest(appended), 'first_appended_id': added[0] if added else None,
            'last_appended_id': added[-1] if added else None},
            'sqlite_sequence': {'table_present': present, 'baseline_sha256': digest(sequence_before),
                'target_sha256': digest(sequence_after), 'before': _slot(sa, 'operation_logs'), 'after': _slot(sb, 'operation_logs')}}
        private = {'type': 'tianming.retained-preview-private.v1', 'columns': names, 'appended_rows': appended,
                   'sqlite_sequence_before': sequence_before, 'sqlite_sequence_after': sequence_after,
                   'credential_comparison_required': True}
        return summary, private


def _receipt(raw, kind, restore_sha):
    value = _decode(raw)
    common = {'type', 'at', 'environment_before_sha256', 'environment_after_sha256', 'changed_keys',
              'credentials_changed', 'factory_activated'}
    if kind == 'home':
        _keys(value, common | {'checkpoint_files', 'checkpoint_bytes', 'state_sha256_preserved',
            'restore_receipt_sha256_preserved', 'program_bytes_changed', 'business_database_written', 'server_started'})
        _require(value['type'] == 'tianming.home-network-preparation.v1', 'home回执版本未知')
        _require(value['restore_receipt_sha256_preserved'] == restore_sha, 'home回执未绑定原恢复')
        _hash(value['state_sha256_preserved'])
        for key in ('checkpoint_files', 'checkpoint_bytes'):
            _require(type(value[key]) is int and value[key] > 0, 'home检查点计数未知')
        false_fields = ('program_bytes_changed', 'business_database_written', 'server_started')
    else:
        _keys(value, common | {'address', 'port', 'source_subnet', 'upstream', 'relay_uid',
            'all_other_environment_values_preserved', 'relay_before_sha256', 'relay_after_sha256',
            'plist_before_sha256', 'plist_after_sha256', 'restore_receipt_sha256', 'runtime_package',
            'supervisor_package', 'home_rehearsal', 'signed_manager_running', 'data_restore_called',
            'old_windows_touched', 'address_192_168_3_80_assigned', 'business_database_directly_written',
            'other_computer_login_verified'})
        _require(value['type'] == 'tianming.factory-preview-lan.v1', 'LAN回执版本未知')
        _require(value['restore_receipt_sha256'] == restore_sha, 'LAN回执未绑定原恢复')
        for key in ('relay_before_sha256', 'relay_after_sha256', 'plist_before_sha256', 'plist_after_sha256',
                    'runtime_package', 'supervisor_package'): _hash(value[key])
        for key in ('all_other_environment_values_preserved', 'home_rehearsal', 'signed_manager_running'):
            _require(value[key] is True, 'LAN回执声明不一致')
        false_fields = ('data_restore_called', 'old_windows_touched', 'address_192_168_3_80_assigned',
                        'business_database_directly_written', 'other_computer_login_verified')
        address = ipaddress.IPv4Address(value['address'])
        _require(address.is_private and str(address) == value['address'] and type(value['port']) is int
                 and value['port'] == 8000 and value['upstream'] == '127.0.0.1:8000' and value['relay_uid'] == 'nobody'
                 and value['source_subnet'] == str(ipaddress.IPv4Network(str(address) + '/24', strict=False)), 'LAN固定转发边界不符')
    _require(all(value[k] is False for k in (*false_fields, 'credentials_changed', 'factory_activated')), '回执布尔声明无效')
    for key in ('environment_before_sha256', 'environment_after_sha256'): _hash(value[key])
    _require(isinstance(value['changed_keys'], list) and all(type(k) is str for k in value['changed_keys'])
             and value['changed_keys'] == sorted(set(value['changed_keys'])), '回执变更键格式无效')
    return value, _legacy_time(value['at'])


def _environments(raws, approvals, restore_sha, upper):
    _require(type(approvals) is list and len(approvals) == 2, '需要原home及LAN两段证据')
    values = [_decode(raw) for raw in raws]
    _require(all(all(type(k) is str and type(v) is str for k, v in item.items()) for item in values), '环境必须保留精确字符串键值')
    public = []; private = []; times = []
    for index, kind in enumerate(('home', 'lan')):
        approval = approvals[index]; _keys(approval, {'kind', 'receipt_raw', 'script_sha256'})
        _require(approval['kind'] == kind, '环境证据顺序错误'); _hash(approval['script_sha256'])
        receipt, at = _receipt(approval['receipt_raw'], kind, restore_sha); times.append(at)
        before, after = values[index:index+2]
        _require(receipt['environment_before_sha256'] == _sha(raws[index]) and
                 receipt['environment_after_sha256'] == _sha(raws[index+1]), '环境原字节与批准回执不符')
        changed = sorted(k for k in before.keys() | after.keys() if _slot(before, k) != _slot(after, k))
        allowed = HOME_FIELDS if kind == 'home' else LAN_FIELDS
        _require(set(changed) <= allowed and receipt['changed_keys'] == changed, '环境有未批准字段或记录不完整')
        _require(all(k in after for k in allowed), '环境阶段缺少明确字段')
        required = {'ERP_ENVIRONMENT': 'production', 'ERP_HOME_REHEARSAL': '1', 'ERP_PRODUCTION_TRANSPORT': 'lan_http',
                    'ERP_BIND_HOST': '127.0.0.1', 'ERP_PORT': '8000', 'ERP_TRUSTED_PROXY_IPS': ''}
        _require(all(after.get(k) == v for k, v in required.items()), '预演隔离环境变化')
        if kind == 'lan':
            address = receipt['address']; origin = 'http://' + address + ':8000'
            expected = {'ERP_TRUSTED_HOSTS': '127.0.0.1,' + address + ',localhost',
                'ERP_ALLOWED_ORIGINS': 'http://127.0.0.1:8000,' + origin,
                'ERP_SHELF_LABEL_ORIGIN': origin, 'ERP_MOBILE_QR_ORIGIN': origin}
            _require(set(changed) == LAN_FIELDS and all(after[k] == v for k, v in expected.items()), 'LAN不是原固定四键变更')
        changes = [{'key': k, 'before': _slot(before, k), 'after': _slot(after, k)} for k in changed]
        step = {'kind': kind, 'receipt_sha256': _sha(approval['receipt_raw']), 'script_sha256': approval['script_sha256'],
                'before_sha256': _sha(raws[index]), 'after_sha256': _sha(raws[index+1]),
                'changes_sha256': digest(changes), 'changed_fields_count': len(changes)}
        public.append(step); private.append(dict(step, changes=changes))
    _require(times[0] <= times[1] <= upper, '环境原批准时间链无效')
    return public, private, times[0]


def compare_preview(baseline_shared, target_shared, *, baseline_snapshot, target_snapshot,
                    home_environment_raw, approved_environment_chain, restore_receipt_sha256,
                    prepared_at, recorded_roots=None):
    """Return {summary, private_manifest}; require external authentication/locks.

    Inputs are two offline local shared trees and authenticated expected full
    snapshots. Only the exact two historical receipt formats are supported.
    Their root provenance, script identity and original restore identity must
    be checked by the protected caller; a matching SHA alone is not approval.
    The intermediate environment may be reconstructed by that caller using
    the original script, but its exact bytes must match both old receipts.
    """
    roots = (Path(baseline_shared).absolute(), Path(target_shared).absolute())
    _require(roots[0] != roots[1] and not roots[0].is_relative_to(roots[1]) and not roots[1].is_relative_to(roots[0]),
             '必须是两个独立静态共享树')
    _snapshot(baseline_snapshot); _snapshot(target_snapshot)
    _hash(restore_receipt_sha256); upper = _time(prepared_at)
    recorded_roots = roots if recorded_roots is None else recorded_roots
    _require(isinstance(recorded_roots, (tuple, list)) and len(recorded_roots) == 2, '来源路径绑定不完整')
    for root in roots: _offline(root)
    pins = [_inventory(root) for root in roots]
    actual = [_capture(root, recorded) for root, recorded in zip(roots, recorded_roots)]
    _require(actual == [baseline_snapshot, target_snapshot], '完整快照与认证输入不符')
    stable = set(actual[0]) - {'database_sha256', 'facts_sha256', 'preserved_environment_sha256'}
    _require(all(actual[0][k] == actual[1][k] for k in stable), '结构、附件或其他完整事实变化')
    raws = (roots[0].joinpath('environment.json').read_bytes(), home_environment_raw,
            roots[1].joinpath('environment.json').read_bytes())
    public_env, private_env, lower = _environments(raws, approved_environment_chain, restore_receipt_sha256, upper)
    summary, private = _compare_databases(*roots, recorded_roots, lower, upper)
    summary['environment_chain'] = public_env; private['environment_chain'] = private_env
    for index, (root, recorded) in enumerate(zip(roots, recorded_roots)):
        _offline(root)
        _require(_capture(root, recorded) == actual[index] and _inventory(root) == pins[index], '比较期间完整共享树发生变化')
    return {'summary': summary, 'private_manifest': private}
