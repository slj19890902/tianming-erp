"""Offline preparation only: freezes exact stage A entry; never runs osascript."""
import ast
import hashlib
import json
import os
from pathlib import Path
import runpy
import shlex
import sys

REPO = Path(__file__).absolute().parents[2]
ENTRY = REPO / 'scripts/admin/controlled_uat_admission_v717.py'
OUT = Path('/Users/kimiyf/ERP-Migration/evidence/erp3-next-20261023/controlled-export-v717/admission')


def prepare():
    if os.getuid() != 501 or os.geteuid() != 501 or len(sys.argv) != 1:
        raise ValueError('Offline preparation is only for UID501 and accepts no parameters')
    raw = ENTRY.read_bytes()
    compile(raw, str(ENTRY), 'exec')
    ast.parse(raw, feature_version=(3, 9))
    scope = runpy.run_path(str(ENTRY), run_name='offline_candidate')
    if ENTRY != scope['SOURCE']:
        raise ValueError('Not the fixed source tree')
    for source, expected, size in scope['INPUTS'].values():
        if source.is_symlink() or scope['file_hash'](source) != expected or (size is not None and source.stat().st_size != size):
            raise ValueError('Candidate input changed')
    sha = hashlib.sha256(raw).hexdigest()
    # exec receives exactly the bytes whose digest was checked, including ingress.
    boot = ("import hashlib,os,pathlib,stat; "
            f"p=pathlib.Path({str(ENTRY)!r}); "
            "fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); f=os.fdopen(fd,'rb'); "
            "st=os.fstat(f.fileno()); raw=f.read(65537); "
            "assert stat.S_ISREG(st.st_mode) and st.st_uid==501 and st.st_nlink==1; "
            f"assert hashlib.sha256(raw).hexdigest()=={sha!r}; "
            "exec(compile(raw,str(p),'exec'),{'__name__':'__main__','__file__':str(p),'_LAUNCHER_ADMITTED':raw})")
    command = '/usr/bin/python3 -I -S -B -c ' + shlex.quote(boot)
    script = ('with timeout of 1800 seconds\n do shell script ' + json.dumps(command)
              + ' with administrator privileges with prompt "v717受控导出工具准入与计划审阅（不导出业务）"\nend timeout\n')
    OUT.mkdir(mode=0o700)  # Never replace an earlier approval entry.
    (OUT / 'authorize.applescript').write_text(script)
    record = {'status': 'offline_candidate_not_authorized_or_executed', 'entry': str(ENTRY),
              'entry_sha256': sha, 'stage': str(scope['STAGE']), 'tool_source': scope['TOOL_SOURCE'],
              'unsigned_sha256': scope['UNSIGNED_SHA'], 'manifest_sha256': scope['TOOL_MANIFEST'],
              'admin_sha256': scope['ADMIN_SHA'], 'admin_manifest_sha256': scope['ADMIN_MANIFEST'],
              'actual_signed_sha256': None, 'actual_export_plan_sha256': None,
              'scope': 'install exact admin, sign/publish fixed tool, inspect actual source and create plan',
              'excluded': ['capture', 'service start/stop/update', 'migration', 'business correction', 'Keychain setup'],
              'native_executed': False}
    (OUT / 'reviewed.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    prepare()
