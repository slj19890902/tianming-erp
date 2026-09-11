"""Versioned, atomically saved non-business mobile search preferences."""
import json, os, threading
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile
from app.core.config import load_settings

_lock=threading.Lock()
def path():
    configured=os.getenv('ERP_MOBILE_DIMENSION_SETTINGS_PATH')
    return Path(configured) if configured else load_settings().database_path.parent/'mobile_dimension_settings.json'

def read():
    target=path()
    if not target.exists():return {'version':0,'near_mm':5,'expanded_mm':10,'history':[]}
    data=json.loads(target.read_text(encoding='utf8'))
    validate(data['near_mm'],data['expanded_mm'])
    return data

def validate(near,expanded):
    if type(near) is not int or type(expanded) is not int or not 1<=near<expanded<=50:
        raise ValueError('接近范围须为1至49mm，扩大范围须大于接近范围且不超过50mm')

def public(data):return {k:data[k] for k in ('version','near_mm','expanded_mm')}

@contextmanager
def locked():
    target=path();target.parent.mkdir(parents=True,exist_ok=True)
    with _lock, target.with_suffix('.lock').open('a+b') as stream:
        if stream.seek(0,2)==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)

def save(*,near_mm,expanded_mm,expected_version,operation_key,user_id):
    validate(near_mm,expanded_mm)
    if type(expected_version) is not int or expected_version<0:raise ValueError('设置版本无效')
    if not isinstance(operation_key,str) or not 8<=len(operation_key)<=80:raise ValueError('保存标识无效')
    with locked():
        current=read()
        for entry in current['history']:
            if entry['operation_key']==operation_key:
                if (entry['near_mm'],entry['expanded_mm'],entry['user_id'])!=(near_mm,expanded_mm,user_id):raise ValueError('保存标识已被其他内容使用')
                return public(current)
        if current['version']!=expected_version:raise LookupError('范围设置已变化，请重新读取后保存')
        entry={'version':current['version']+1,'near_mm':near_mm,'expanded_mm':expanded_mm,'user_id':user_id,'operation_key':operation_key,'saved_at':datetime.now(timezone.utc).isoformat()}
        updated={**public(entry),'history':[*current['history'],entry]}
        temporary=None
        try:
            with NamedTemporaryFile(mode='w',encoding='utf8',dir=path().parent,delete=False) as stream:
                temporary=Path(stream.name);json.dump(updated,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path())
        finally:
            if temporary and temporary.exists():temporary.unlink()
        return public(updated)
