"""Explicit local storage adapter for portable recovery tests, never production.

These tests exercise archives/DB contracts, not Darwin NFS/root approval. Mac's
actual storage policy is tested separately with labeled native metadata adapters.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from desktop_assistant import mac_backup_storage as storage, nas_probe
from desktop_assistant.storage import write_json


@contextmanager
def approved_local_storage(root):
    def bound(path):
        path = Path(path)
        if not path.is_absolute() or (path != root and root not in path.parents):
            raise ValueError('Synthetic storage escaped its test-owned root')
    def check(path):
        bound(path)
        if nas_probe._write_probe(path): raise ValueError('NAS无法完成读写验证')
    def publish(path, receipt, proof):
        bound(path); path.parent.mkdir(exist_ok=True); write_json(path, receipt)
        return datetime.now(timezone.utc).isoformat()
    with ExitStack() as stack:
        for name, value in [('before_write', bound), ('after_write', lambda anchor, path, *a: bound(path)),
                            ('publish_receipt', publish),
                            ('verified_at', lambda root, state, **kw: state.get('last_backup_at'))]:
            stack.enter_context(patch.object(storage, name, value))
        stack.enter_context(patch.object(nas_probe, 'check_before_stop', check))
        yield
