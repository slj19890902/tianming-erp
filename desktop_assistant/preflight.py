"""Read-only attachment readiness report; does not import or start an ERP."""
from pathlib import Path
from contextlib import closing
import argparse, json, sqlite3
from desktop_assistant.storage import sha


def inspect(database: Path, source: Path):
    source=source.resolve();checks=[]
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables={row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        for table,columns in [('product_drawings',('image_path','thumbnail_path')),('pdf_order_training_samples',('file_path',))]:
            if table not in tables:continue
            for column in columns:
                for identity,reference in db.execute(f'SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL AND trim({column})<>\'\''):
                    if reference.startswith('private:'):
                        root=source/'data/private_uploads';path=root/reference.removeprefix('private:')
                    elif reference.startswith('/static/uploads/'):
                        root=source/'static/uploads';path=root/reference.removeprefix('/static/uploads/')
                    else:
                        root=source;path=Path(reference);path=path if path.is_absolute() else root/path
                    path=path.resolve()
                    state='external' if not path.is_relative_to(root.resolve()) else 'missing' if not path.is_file() else 'ok'
                    digest=sha(path) if state=='ok' else None
                    if table=='pdf_order_training_samples' and state=='ok':
                        expected=db.execute('select file_sha256 from pdf_order_training_samples where id=?',(identity,)).fetchone()[0]
                        if digest!=expected:state='hash_mismatch'
                    checks.append(dict(table=table,id=identity,field=column,status=state,sha256=digest))
    counts={key:sum(r['status']==key for r in checks) for key in ('ok','missing','external','hash_mismatch')}
    return {'read_only':True,'scope':'product_drawings and pdf_order_training_samples; standard storage paths only','counts':counts,'checks':checks,'ready_for_takeover':False,'remaining_gates':['external configured paths and other attachments','clean Windows restore, printing, OCR and LAN','signing key custody and cross-revision updater']}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--database',type=Path,required=True);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=inspect(args.database,args.source);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps({'read_only':True,'counts':result['counts'],'ready_for_takeover':False},ensure_ascii=False))
