"""Preview/apply a reviewed unchanged receipt-staging map transition.

Apply only in the approved maintenance window after a standard recovery backup
has been verified. Does not stop services, replace the DB or receive inventory.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser=argparse.ArgumentParser()
    for name in ('database','source-map','target-map','plan'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--actor-id',type=int)
    parser.add_argument('--operation-key')
    parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--verified-backup',type=Path)
    parser.add_argument('--verified-backup-sha256')
    args=parser.parse_args()
    if not args.database.is_file() or args.database.name.lower()=='erp.db':
        parser.error('Use the verified ERP database path')
    source=json.loads(args.source_map.read_text(encoding='utf-8-sig'))['floors']['1F']
    target_hash=digest(args.target_map)
    target=json.loads(args.target_map.read_text(encoding='utf-8-sig'))['floors']['1F']
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    os.environ['ERP_DATABASE_PATH']=str(args.database.resolve())
    os.environ['ERP_TWIN_LAYOUT_RUNTIME_PATH']=str(args.target_map.resolve())
    from app.core.database import create_sqlite_engine
    from app.models.user import User
    from app.services.receipt_staging_map import preview_staging_transition,apply_staging_transition,fingerprint
    from sqlalchemy.orm import Session
    with Session(create_sqlite_engine(args.database)) as db:
        if not args.apply:
            plan=preview_staging_transition(db,source_floor=source,target_floor=target)
            with args.plan.open('x',encoding='utf-8') as file:
                json.dump(plan,file,ensure_ascii=False,indent=2)
            print(json.dumps({'plan_sha256':fingerprint(plan),'area_count':len(plan['areas'])}))
            return
        if not all((args.actor_id,args.operation_key,args.approved_plan_sha256,args.verified_backup,args.verified_backup_sha256)):
            parser.error('Apply requires actor, operation key, approved plan and verified backup hash')
        if not args.verified_backup.is_file() or digest(args.verified_backup)!=args.verified_backup_sha256:
            parser.error('Verified backup hash mismatch')
        expected=json.loads(args.plan.read_text(encoding='utf-8'))
        if fingerprint(expected)!=args.approved_plan_sha256:
            parser.error('Approved plan hash mismatch')
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        actor=db.get(User,args.actor_id)
        if not actor or not actor.is_active or actor.role not in {'admin','boss'}:
            parser.error('Apply requires an active administrator')
        result=apply_staging_transition(db,source_floor=source,target_floor=target,
            expected_plan=expected,actor=actor,operation_key=args.operation_key)
        if digest(args.target_map)!=target_hash:
            raise RuntimeError('Runtime map changed; transition rolled back')
        db.commit()
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
