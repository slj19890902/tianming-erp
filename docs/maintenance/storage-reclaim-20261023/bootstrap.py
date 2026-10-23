import os,sys,stat,hashlib
from pathlib import Path
assert os.getuid()==os.geteuid()==0 and sys.flags.isolated and sys.flags.no_site
SOURCE=Path('/Users/kimiyf/ERP-Migration/storage-reclaim-20261023/docs/maintenance/storage-reclaim-20261023')
DEST=Path('/Library/TianmingERP-StorageReclaim-20261023')
PINS={'worker.py': '8c6618f5fcf1963bd3c3ec38d57e05773cdff1f8b6709e5a27804eb9cda8e1da', 'native.py': '952f85be1464ad004c8d03bc2efda3ec1869489a36ed6235c263e2efbf9c4929', 'reviewed.json': '5e680fafba2282d6d2eaece0108d42618f1fd5f01e551ae447fc8c1e9c6c58e3'}
payload={}
for name,expected in PINS.items():
 p=SOURCE/name;st=p.lstat();assert stat.S_ISREG(st.st_mode) and st.st_uid==501 and st.st_nlink==1
 raw=p.read_bytes();assert hashlib.sha256(raw).hexdigest()==expected;payload[name]=raw
assert not DEST.exists();DEST.mkdir(mode=0o755)
for name,raw in payload.items():
 with (DEST/name).open('xb') as f:os.fchmod(f.fileno(),0o644);f.write(raw);f.flush();os.fsync(f.fileno())
os.execve('/usr/bin/python3',['/usr/bin/python3','-I','-S','-B',str(DEST/'worker.py')],{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':'/var/root','LANG':'en_US.UTF-8'})
