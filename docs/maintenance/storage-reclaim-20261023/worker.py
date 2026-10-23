"""Fixed, one-shot maintenance install and native cleanup. No business writes."""
import os,sys,json,stat,hashlib,subprocess,time,fcntl,urllib.request,shutil,traceback
from pathlib import Path
from datetime import datetime,timezone
S=Path('/Library/TianmingERP-StorageReclaim-20261023');M=Path('/Library/TianmingERP-LocalMaintenance');I=Path('/Library/TianmingERP/instance')
assert os.getuid()==os.geteuid()==0 and sys.flags.isolated and sys.flags.no_site
assert Path(__file__).absolute()==S/'worker.py'
for p in (S,*S.parents):
 st=p.lstat();assert stat.S_ISDIR(st.st_mode) and st.st_uid==0 and not st.st_mode&0o022
C=json.loads((S/'reviewed.json').read_bytes());paused=[];installed=False;phase='admission';settings_before=None

def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(4*1024**2),b''):h.update(b)
 return h.hexdigest()
def atomic(p,v):
 raw=v if isinstance(v,bytes) else json.dumps(v,ensure_ascii=False,sort_keys=True).encode();t=p.with_name(p.name+'.storage-20261023.pending')
 with t.open('xb') as f:os.fchmod(f.fileno(),0o644);f.write(raw);f.flush();os.fsync(f.fileno())
 os.replace(t,p);fd=os.open(p.parent,os.O_RDONLY);os.fsync(fd);os.close(fd)
def event(phase,**data):
 with (S/'events.jsonl').open('a') as f:os.fchmod(f.fileno(),0o644);f.write(json.dumps(dict(phase=phase,at=datetime.now(timezone.utc).isoformat(),**data),ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
def lc(*args):return subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True,timeout=20)
def health():
 with urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=10) as r:return r.status==200 and json.load(r).get('ok') is True
def pause(label):
 assert lc('disable','system/'+label).returncode==0;paused.append(label)
 end=time.monotonic()+60
 while True:
  r=lc('print','system/'+label)
  if r.returncode!=0 or '\tstate = running\n' not in r.stdout:break
  if time.monotonic()>end:raise RuntimeError('Maintenance active; no force stop')
  time.sleep(2)
 r=lc('bootout','system/'+label);assert r.returncode==0
 event('maintenance_paused',label=label)
def call(receipt):
 with (S/(receipt+'.stdout')).open('xb') as out,(S/(receipt+'.stderr')).open('xb') as err:
  os.fchmod(out.fileno(),0o644);os.fchmod(err.fileno(),0o600)
  p=subprocess.Popen(['/usr/bin/python3','-I','-S','-B',str(M/'launcher.py'),'cleanup'],stdin=subprocess.DEVNULL,stdout=out,stderr=err,cwd='/',env={'HOME':'/Library/TianmingERP/home','PATH':'/usr/bin:/bin:/usr/sbin:/sbin'},start_new_session=True)
  event('native_started',receipt=receipt,pid=p.pid)
  code=p.wait()
 assert code==0,'Native cleanup failed; inspect protected stderr'
 result=json.loads((S/(receipt+'.stdout')).read_text());atomic(S/(receipt+'.json'),result)
 assert result.get('status')=='completed' and result.get('backup_proof_verified') is True
 return result
try:
 for name,key in [('native.py','before_sha256'),('assets.json','assets_sha256'),('policy.json','policy_sha256')]:
  p=M/name;st=p.lstat();assert stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_uid==0 and not st.st_mode&0o022;assert digest(p)==C[key]
 for name in ('native.py','reviewed.json','worker.py'):
  p=S/name;st=p.lstat();assert stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_uid==0 and not st.st_mode&0o022
 assert digest(S/'native.py')==C['candidate_sha256']
 state=json.loads((I/'state.json').read_bytes());assert state['current']==C['current'] and not state.get('manual_stop') and health()
 free_before=shutil.disk_usage('/System/Volumes/Data').free
 for name in ('native.py','assets.json','policy.json'):atomic(S/(name+'.before'),(M/name).read_bytes())
 settings_before=(M/'policy.json').read_bytes();assets_before=(M/'assets.json').read_bytes()
 phase='pause_maintenance'
 for label in ('cn.tianming.erp.local-maintenance','cn.tianming.erp.online-backup'):pause(label)
 phase='install'
 with (I/'control/operation.lock').open('r+b') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  assert json.loads((I/'state.json').read_bytes())['current']==C['current'] and health()
  assert digest(M/'native.py')==C['before_sha256'] and digest(M/'policy.json')==C['policy_sha256']
  settings=json.loads(settings_before);assert settings['preserve_formal_backups'] is True and settings['minimum_free_bytes']==200000000000
  settings.update(apply_cleanup=False,cleanup_enabled=True)
  assets=json.loads(assets_before);assets['native.py']=C['candidate_sha256']
  installed=True
  atomic(M/'native.py',(S/'native.py').read_bytes());atomic(M/'assets.json',assets);atomic(M/'policy.json',settings)
  event('installed_preview_only',native_sha256=C['candidate_sha256'])
 phase='preview';preview=call('preview');assert preview['applied'] is False
 forbidden={'authenticated-backup-predecessor','closed-unreferenced-original'}
 for row in preview['planned']:
  p=str(row.get('path',''));assert row.get('kind') not in forbidden and not p.endswith(('.tmbackup','.tmdb','.tmdata','.tmobj','.tma','package.zip'))
 assert set(preview.get('protected_originals',[]))>=set(preview.get('protected_expansions',[]))
 atomic(S/'preview-summary.json',{'planned':preview['planned'],'missing_originals':preview.get('missing_owner_preserved_sha256',[]),'protected_expansions':preview.get('protected_expansions',[])})
 phase='apply'
 assert json.loads((I/'state.json').read_bytes())['current']==C['current'] and health()
 settings['apply_cleanup']=True;atomic(M/'policy.json',settings)
 actual=call('actual');assert actual['applied'] is True
 phase='verify';after=json.loads((I/'state.json').read_bytes());registry=json.loads(Path('/Library/TianmingERP/published.json').read_bytes())
 assert after['current']==C['current'] and not after.get('manual_stop') and health()
 for h in actual['protected_expansions']:
  # Some external role hashes are archives only and never registered. Every
  # surviving registered expansion must remain complete; current is mandatory.
  if h in registry['packages']:
   for kind in ('server','migration'):assert (Path('/Library/TianmingERP/published')/h/kind/'manifest.json').is_file()
 assert C['current'] in registry['packages']
 result={'status':'cleanup_completed','free_before':free_before,'free_after':shutil.disk_usage('/System/Volumes/Data').free,'target_bytes':200000000000,'native_sha256':C['candidate_sha256'],'missing_historical_originals':actual.get('missing_owner_preserved_sha256',[]),'formal_service_stopped':False,'business_data_written':False,'removed':actual['removed']}
 result['target_met']=result['free_after']>=result['target_bytes'];atomic(S/'result.json',result);event('complete',free_bytes=result['free_after'],target_met=result['target_met'])
except Exception as error:
 event('attention',step=phase,error_type=type(error).__name__,message=str(error))
 if installed:
  # Restore the previous guarded implementation on a failed maintenance run.
  atomic(M/'native.py',(S/'native.py.before').read_bytes());atomic(M/'assets.json',assets_before)
 if settings_before is not None:atomic(M/'policy.json',settings_before)
 atomic(S/'result.json',{'status':'needs_attention','step':phase,'error_type':type(error).__name__,'formal_service_stopped':False,'free_after':shutil.disk_usage('/System/Volumes/Data').free})
 traceback.print_exc()
finally:
 for label in reversed(paused):
  try:
   assert lc('enable','system/'+label).returncode==0
   if lc('print','system/'+label).returncode!=0:assert lc('bootstrap','system','/Library/LaunchDaemons/'+label+'.plist').returncode==0
   event('maintenance_resumed',label=label)
  except Exception as e:event('resume_attention',label=label,error_type=type(e).__name__)
