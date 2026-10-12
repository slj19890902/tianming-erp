"""Pure, bounded current-Mac publication receipt; no authority or IO."""
from desktop_assistant import current_mac_worker_contract as c
from desktop_assistant import current_mac_cutover_contract as v3

NAME = 'current-mac-activation-publication.json'
FAILED = 'current-mac-activation-failed.json'
TYPE = 'tianming.current-mac-activation-publication.v1'
FIELDS = frozenset({'type', 'attempt_id', 'plan_sha256', 'activation_sha256',
    'request_sha256', 'ready'})


def validate(value, *, activation_raw=None, ready=None):
    c.exact(value, FIELDS)
    c.require(value['type'] == TYPE and c.token(value['attempt_id'])
        and type(value['ready']) is bool
        and all(c.sha(value[key]) for key in ('plan_sha256','activation_sha256','request_sha256')))
    if ready is not None:
        c.require(type(ready) is bool and value['ready'] is ready)
    if activation_raw is not None:
        record = v3.decode(activation_raw); v3.validate_record(record)
        c.require(value['activation_sha256'] == c.raw_digest(activation_raw)
            and value['request_sha256'] == v3.digest(record['request']))
    return value
