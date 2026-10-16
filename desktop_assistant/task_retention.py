"""Reap only explicitly sealed completed managed test copies; no root delegation.

Reports, candidates awaiting acceptance, unknown/failed/active jobs and every
source reference remain. Original path/process/ownership guards still apply.
"""
from datetime import datetime,timezone
import hashlib,json,os,re,shutil,stat
from pathlib import Path
from desktop_assistant.cleanup import local_path,inspect_tree,assert_idle,remove_owned_tree
from desktop_assistant.storage import sha,write_json
from desktop_assistant.operation_lock import operation_lock

JOB=re.compile(r'task-[a-f0-9]{32}')


def copy_identity(target):
    target=local_path(target);rows=[];size=0
    for p in (target,*sorted(inspect_tree(target))):
        s=p.lstat()
        if s.st_uid!=os.getuid() or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
            raise ValueError('task owner/type changed')
        rows.append([str(p.relative_to(target)),s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns])
        if p.is_file():size+=s.st_size
    return {'fingerprint':hashlib.sha256(json.dumps(rows).encode()).hexdigest(),'logical_bytes':size}


def reconcile(base, *, apply=False, exclude_jobs=()):
    base=local_path(base)
    if base.stat().st_uid!=os.getuid() or any((p/'.git').exists() for p in (base,*base.parents)):
        raise ValueError('managed task base must belong to current ordinary owner, outside Git')
    report=dict(type='tianming.task-retention.v1',at=datetime.now(timezone.utc).isoformat(),
                apply=apply,items=[],free_before=shutil.disk_usage(base).free)
    with operation_lock(base/'retention.lock'):
        records={};pins={};sources=[];unknown=False
        for job in sorted(base.iterdir()):
            if not JOB.fullmatch(job.name):continue
            try:
                path=local_path(job/'result.json');s=path.stat()
                if s.st_uid!=os.getuid() or s.st_size>1024*1024:raise ValueError('unknown result owner/size')
                v=json.loads(path.read_bytes())
                if v['schema']!='tianming.test-task.v1' or v['job']!=str(job) or v['copies']!=str(job/'copies'):
                    raise ValueError('unknown task contract')
                records[job]=v;pins[path]=sha(path)
                if v.get('status')!='passed' or v.get('keep_copy',True):sources.append(local_path(Path(v['source'])))
            except (OSError,ValueError,KeyError,TypeError):
                unknown=True;report['items'].append(dict(path=str(job),status='retained',reason='unknown owner/record; no inferred closure'))
        for job,v in records.items():
            target=job/'copies'
            if not target.exists():continue
            item=dict(path=str(target),status='retained');report['items'].append(item)
            if job in exclude_jobs:
                item['reason']='current task already attempted cleanup; do not repeat';continue
            if unknown or v.get('status')!='passed' or v.get('keep_copy',True) or v.get('manual_acceptance_pending',True) or not v.get('copy_identity'):
                item['reason']='unknown, active/failed, unsealed or awaiting acceptance';continue
            if any(s==target or target in s.parents for s in sources):item['reason']='source of another retained task';continue
            try:
                if copy_identity(target)!=v['copy_identity']:raise ValueError('sealed copy changed')
                assert_idle(target)
                if any(sha(p)!=h for p,h in pins.items()):raise ValueError('task registry changed after review')
                item.update(status='eligible',logical_bytes=v['copy_identity']['logical_bytes'])
                if apply:
                    item.update(status='removed',deleted_bytes=remove_owned_tree(target,job))
                    # The original result remains immutable evidence; disappearance
                    # of its declared disposable copy makes replay a no-op.
            except (OSError,ValueError,KeyError) as e:item.update(status='retained',reason=str(e))
    report['free_after']=shutil.disk_usage(base).free
    report['actual_free_delta_bytes']=report['free_after']-report['free_before']
    report['low_space']=report['free_after']<60*1024**3
    report['soft_budget_bytes']=30*1024**3
    sizes=[v.get('copy_identity',{}).get('logical_bytes',0)
           for job,v in records.items() if (job/'copies').exists()
           and isinstance(v.get('copy_identity'),dict)]
    report['tracked_copy_bytes']=sum(n for n in sizes if type(n) is int and n>=0)
    report['over_soft_budget']=report['tracked_copy_bytes']>report['soft_budget_bytes']
    report['budget_scope']='registered sealed test copies only; unknown artifacts remain uncounted/protected'
    write_json(base/'retention-latest.json',report)
    return report


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-dir',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    print(json.dumps(reconcile(args.base_dir,apply=args.apply),ensure_ascii=False))


if __name__=='__main__':main()
