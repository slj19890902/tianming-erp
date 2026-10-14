"""Exact public authorization for one known interrupted monitor CAS only."""
import math
import re

from desktop_assistant import mac_system_upgrade_contract as original

TYPE = 'tianming.system-upgrade-split-recovery-plan.v1'
CAPABILITY = 'system_upgrade_split_recovery_v1'
EVIDENCE = frozenset({'plan.json', 'prepared.json', 'pending.json', 'old-control.json',
    'old.plist', 'stop.json', 'old-exited.json', 'old-service-processes.json', 'failed.json'})
BINDINGS = frozenset({'code_manifest_sha256', 'old_package_sha256', 'new_package_sha256',
    'current_package_sha256', 'context_sha256', 'publisher_sha256', 'registry_sha256',
    'activation_sha256', 'owner_publication_sha256', 'state_stopped_sha256'})
FIELDS = BINDINGS | {'type', 'recovery_id', 'attempt_id', 'created_at', 'expires_at',
                     'evidence_sha256', 'upgrade_plan_sha256', 'source'}
PROCESS_FIELDS = frozenset({'pid', 'ppid', 'uid', 'ruid', 'gid', 'rgid', 'birth_sec', 'birth_usec', 'exe'})
ERROR = '仅允许核实完整的监护split恢复；保留现场，不覆盖或自动重试'


def require(ok):
    if not ok: raise ValueError(ERROR)


def validate(value, *, now):
    require(type(value) is dict and set(value) == FIELDS and value['type'] == TYPE)
    require(original.token(value['recovery_id']) and original.token(value['attempt_id'])
            and all(original.sha(value[key]) for key in BINDINGS | {'upgrade_plan_sha256'}))
    require(type(value['source']) is str and re.fullmatch('[0-9a-f]{40}', value['source']) is not None)
    require(type(value['evidence_sha256']) is dict and set(value['evidence_sha256']) == EVIDENCE
            and all(original.sha(v) for v in value['evidence_sha256'].values())
            and value['evidence_sha256']['plan.json'] == value['upgrade_plan_sha256'])
    require(all(type(value[k]) is int and 0 <= value[k] <= original.MAX_TIMESTAMP
                for k in ('created_at', 'expires_at')))
    require(0 < value['expires_at'] - value['created_at'] <= original.MAX_AGE
            and type(now) in (int, float) and math.isfinite(now)
            and value['created_at'] <= now < value['expires_at'])
    require(len(original.canonical(value)) <= original.LIMIT)
    return value


def process(value, executable):
    require(type(value) is dict and set(value) == PROCESS_FIELDS)
    require(all(type(value[k]) is int for k in PROCESS_FIELDS - {'exe'}))
    require(1 < value['pid'] < 2**31 and value['ppid'] == 1
            and all(value[k] == 499 for k in ('uid', 'ruid', 'gid', 'rgid'))
            and 0 <= value['birth_sec'] <= original.MAX_TIMESTAMP
            and 0 <= value['birth_usec'] < 1000000 and value['exe'] == executable)
    return value
