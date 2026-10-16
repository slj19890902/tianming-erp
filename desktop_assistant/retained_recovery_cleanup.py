"""Receipt-bound closure of one verified disposable recovery copy, under lock.

No service control, new capture, database restore, backup unlink or credentials.
The caller supplies the existing authenticated-cipher verifier and healthy check.
A protected administrator entry is needed when normal process inspection fails.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from desktop_assistant.cleanup import local_path, inspect_tree, remove_owned_tree
from desktop_assistant.storage import sha, database_info, signed_release_manifest


def require(ok, message):
    if not ok:raise ValueError('Retained recovery closure: '+message)


def canonical(v):
    return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def owned_read(path, owner, limit=1024*1024):
    local_path(path);s=path.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid==owner['uid'] and s.st_gid==owner['gid']
            and stat.S_IMODE(s.st_mode)==0o600 and s.st_nlink==1 and s.st_size<limit,
            'owner record protection changed')
    return json.loads(path.read_bytes())


def owned_write(path, value, owner):
    temporary=path.with_name(path.name+'.cleanup-pending')
    with temporary.open('xb') as stream:
        os.fchmod(stream.fileno(),0o600)
        if os.geteuid()==0:os.fchown(stream.fileno(),owner['uid'],owner['gid'])
        stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def close_locked(manager, owner, expected_current, *, verify_cipher, healthy):
    """Caller holds the original operation lock; never clears an unproved flag."""
    root=local_path(manager.root);state_path=root/'state.json'
    before_raw=state_path.read_bytes();state=owned_read(state_path,owner)
    require(state.get('current')==expected_current and state.get('operation')=='update'
            and state.get('manual_stop') is False and healthy(), 'healthy completed release required')
    paths=state.get('backup_work_retained') or []
    if not paths:
        index=state.get('last_backup_local') or {}
        identity=index.get('sha256','')
        if re.fullmatch('[a-f0-9]{64}',identity):
            audit_path=root/'control'/('recovery-cleanup-'+identity+'.json')
            if audit_path.exists():
                audit=owned_read(audit_path,owner)
                if audit.get('status')=='removed':
                    require(audit.get('state_after_sha256')==hashlib.sha256(before_raw).hexdigest()
                            and not Path(audit['path']).exists(), 'interrupted closure state differs')
                    audit.update(status='closed',finished_at=datetime.now(timezone.utc).isoformat())
                    owned_write(audit_path,audit,owner)
        return {'status':'already_closed','deleted_bytes':0}
    require(len(paths)==1,'exactly one retained owner required')
    target=local_path(Path(paths[0]));job=target.parent
    require(target==job/'independent-recovery' and job.parent==root/'staging'
            and re.fullmatch(r'local-backup-[a-f0-9]{32}|local-code-update-[a-f0-9]{32}',job.name),
            'retained work escaped its fixed owner')
    proof=owned_read(job/'verified.json',owner);index=state.get('last_backup_local') or {}
    identity=proof.get('backup_sha256','')
    require(re.fullmatch('[a-f0-9]{64}',identity) is not None
            and proof.get('job_id')==job.name and index.get('sha256')==identity
            and index.get('receipt')=='local-backup-'+identity+'.json', 'owner and published index differ')
    receipt=owned_read(root/'control'/index['receipt'],owner)
    require(receipt.get('type')=='tianming.local-only-backup-receipt.v1'
            and receipt.get('sha256')==identity
            and receipt.get('authenticated_recovery')==proof.get('authenticated_recovery')
            and receipt.get('path')==state.get('last_backup')==str(root/'backups'/proof['backup_filename'])
            and receipt.get('verified_at')==index.get('source_time')==state.get('last_backup_at'),
            'published complete recovery receipt differs')
    cipher=verify_cipher(manager,proof,owner)
    require(cipher==Path(receipt['path']) and sha(cipher)==identity,'authenticated cipher changed')
    audit_path=root/'control'/('recovery-cleanup-'+identity+'.json')
    intent=dict(type='tianming.retained-recovery-cleanup.v1',current=expected_current,
        path=str(target),backup_sha256=identity,owner_proof_sha256=sha(job/'verified.json'),
        state_sha256=hashlib.sha256(before_raw).hexdigest())
    after=dict(state);after.pop('backup_work_retained',None)
    intent['state_after_sha256']=hashlib.sha256(canonical(after)).hexdigest()
    if target.exists():
        prior=owned_read(audit_path,owner) if audit_path.exists() else None
        if prior is not None:
            require(all(prior.get(k)==v for k,v in intent.items())
                    and prior.get('status')=='verified_intent', 'cleanup intent changed')
        journal=job/'cleanup-manifest.json'
        manifest=(owned_read(journal,owner,16*1024*1024) if prior is not None
                  else json.loads((target/'manifest.json').read_bytes()))
        if (target/'manifest.json').exists():
            require(json.loads((target/'manifest.json').read_bytes())==manifest,
                    'retained manifest changed during cleanup')
        require(hashlib.sha256(canonical(manifest)).hexdigest()==proof['authenticated_recovery']['manifest_sha256'],
                'retained payload manifest differs from authenticated recovery')
        files=manifest['files'];entries=inspect_tree(target)
        actual={p.relative_to(target).as_posix() for p in entries if p.is_file()}
        expected=set(files)|{'manifest.json'}
        require(actual<=expected if prior is not None else actual==expected,
                'retained payload inventory differs')
        for p in (target,*entries):
            s=p.lstat();require(s.st_uid==owner['uid'] and s.st_gid==owner['gid'], 'retained payload owner changed')
            if p.is_file():require(s.st_nlink==1, 'retained payload has hard links')
        require(all(sha(target/name)==files[name] for name in actual-{'manifest.json'}),
                'retained payload bytes changed')
        database=target/'shared/data/carton_erp.sqlite3'
        if database.exists():
            require(database_info(database)==proof['authenticated_recovery']['database'],
                    'retained database differs from verified source')
        if (target/'release.zip').exists():
            signed_release_manifest(target/'release.zip',manager.public_key)
        require(state_path.read_bytes()==before_raw and healthy(),'state changed before cleanup')
        if prior is None:
            require(len(canonical(manifest))<16*1024*1024,'recovery cleanup journal too large')
            if journal.exists():
                require(owned_read(journal,owner,16*1024*1024)==manifest,'cleanup manifest changed')
            else:owned_write(journal,manifest,owner)
            owned_write(audit_path,{**intent,'status':'verified_intent'},owner)
        deleted=remove_owned_tree(target,job)
    else:
        old=owned_read(audit_path,owner)
        require(all(old.get(k)==v for k,v in intent.items()) and old.get('status') in ('verified_intent','removed'),
                'missing payload without original verified cleanup intent')
        deleted=old.get('deleted_bytes',0)
    require(not target.exists() and sha(cipher)==identity and healthy()
            and state_path.read_bytes()==before_raw,'closure recheck failed; preserve original intent')
    # Metadata closure follows proven removal; no business/release status rewritten.
    owned_write(audit_path,{**intent,'status':'removed','deleted_bytes':deleted},owner)
    owned_write(state_path,after,owner)
    result={**intent,'status':'closed','deleted_bytes':deleted,
            'finished_at':datetime.now(timezone.utc).isoformat(),'backup_preserved':True}
    owned_write(audit_path,result,owner)
    return result
