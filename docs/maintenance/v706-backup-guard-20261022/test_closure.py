import importlib.util,json,hashlib
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
import pytest
from desktop_assistant import local_backup_only as local

@pytest.fixture
def m():
 spec=importlib.util.spec_from_file_location('closure_native',Path(__file__).with_name('native.py'));mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

@pytest.fixture
def scene(tmp_path,monkeypatch,m):
 root=tmp_path/'instance';(root/'backups').mkdir(parents=True);(root/'staging'/('local-code-update-'+'a'*32)).mkdir(parents=True);(root/'receipts').mkdir();install=tmp_path/'install';install.mkdir();m.ROOT=root;m.INSTALL=install
 current=root/'backups/current.tmbackup';old=root/'backups/old.tmbackup';current.write_bytes(b'current-cipher-fixture');old.write_bytes(b'owner-kept-cipher-fixture')
 digest=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest();current_hash=digest(current);old_hash=digest(old)
 auth={'cipher_sha256':current_hash};receipt={'sha256':current_hash,'authenticated_recovery':auth,'type':local.RECEIPT}
 verified={'backup_sha256':current_hash,'authenticated_recovery':auth}
 (root/'staging'/('local-code-update-'+'a'*32)/'verified.json').write_text('{}')
 for h in [current_hash,old_hash]:(root/'receipts'/h).write_text('{}')
 state=dict(last_backup_local={'sha256':current_hash},last_backup=str(current));(root/'state.json').write_text('{}')
 settings={'preserve_formal_backups':True,'legacy_backup_programs':{h:{'name':p.name,'programs':['b'*64]} for h,p in [(current_hash,current),(old_hash,old)]}}
 monkeypatch.setattr(local,'enabled',lambda _:True);monkeypatch.setattr(local,'_authority',lambda:(None,None,None));monkeypatch.setattr(local,'_owned_read',lambda p,_:verified if p.name=='verified.json' else receipt);monkeypatch.setattr(local,'_receipt_path',lambda _,h:root/'receipts'/h);monkeypatch.setattr(local,'_check_proof',lambda *a:current);monkeypatch.setattr(local,'_active_references',lambda *a:([],[]));m.config=lambda:settings
 monkeypatch.setattr(m.policy,'stable_file',lambda p,uid:{'fixture':str(p)})
 return SimpleNamespace(**locals())

@pytest.mark.parametrize('source',['state','protected_policy','no_pin'])
def test_all_formal_backups_protected_even_without_pin(scene,source):
 c=scene
 if source!='no_pin':(c.state if source=='state' else c.settings)['owner_preserved_backup_sha256']=[c.old_hash]
 proof=c.m.backup_probe(SimpleNamespace(state=c.state));assert all(r['protected'] for r in proof['backups']);assert proof['missing_owner_preserved_sha256']==[]
 assert c.old.read_bytes()==b'owner-kept-cipher-fixture'

def test_missing_original_is_reported_not_faked(scene):
 c=scene;c.state['owner_preserved_backup_sha256']=[c.old_hash,'d'*64];c.old.unlink();proof=c.m.backup_probe(SimpleNamespace(state=c.state))
 assert set(proof['missing_owner_preserved_sha256'])=={c.old_hash,'d'*64}
 assert not c.old.exists()

@pytest.mark.parametrize('pins',['bad-type',['bad-hash'],['a'*64]*129])
def test_invalid_pins_block(scene,pins):
 c=scene;c.state['owner_preserved_backup_sha256']=pins
 with pytest.raises(ValueError,match='preservation pins invalid'):c.m.backup_probe(SimpleNamespace(state=c.state))

def test_workspace_accounts_wal_and_all_files_without_reuse_discount(tmp_path,m,monkeypatch):
 (tmp_path/'data').mkdir();db=tmp_path/'data/carton_erp.sqlite3';db.write_bytes(b'x'*100);(tmp_path/'data/carton_erp.sqlite3-wal').write_bytes(b'x'*80)
 monkeypatch.setattr(m.file_points,'fingerprint',lambda p:{'a':[1,2,200,3,4],'b':[1,3,400,3,4]})
 a=m.backup_workspace_budget(tmp_path,kind='database');assert a['database_and_wal_bytes']==180 and a['business_file_bytes']==600
 assert a['temporary_budget_bytes']==6*180+2*600+2*400+1024**3+16*1024**2
 assert m.backup_workspace_budget(tmp_path,kind='data')['temporary_budget_bytes']==6*180+6*600+1024**3+16*1024**2

def test_workspace_refuses_symlink_and_unreadable_inventory(tmp_path,m,monkeypatch):
 (tmp_path/'data').mkdir();(tmp_path/'outside').write_bytes(b'x');db=tmp_path/'data/carton_erp.sqlite3';db.symlink_to(tmp_path/'outside')
 with pytest.raises(ValueError):m.backup_workspace_budget(tmp_path,kind='database')
 db.unlink();db.write_bytes(b'x')
 def bad(_):raise PermissionError('metadata unavailable')
 monkeypatch.setattr(m.file_points,'fingerprint',bad)
 with pytest.raises(PermissionError):m.backup_workspace_budget(tmp_path,kind='database')

def test_preincident_online_points_survive_normal_rotation(m,monkeypatch):
 points=[{'sha256':'a'*64},{'sha256':'b'*64},{'sha256':'c'*64}]
 monkeypatch.setattr(m.policy,'point_retirements',lambda *a,**k:points[:2])
 assert m.online_rotation_candidates(points,settings={'owner_preserved_online_point_sha256':['a'*64]},keep=3)==[points[1]]
 with pytest.raises(ValueError):m.online_rotation_candidates(points,settings={'owner_preserved_online_point_sha256':['bad']},keep=3)

def test_root_cleanup_rejects_unprotected_backup_even_if_worker_regresses(tmp_path,m,monkeypatch):
 from desktop_assistant import manager as manager_mod,operation_lock,mac_release_publish
 base=tmp_path/'base';base.mkdir();(base/'published').mkdir();(base/'publisher-public.pem').write_text('fixture')
 root=base/'instance';root.mkdir();install=tmp_path/'maintenance';install.mkdir();identity='a'*64
 state={'current':identity,'manual_stop':False};(root/'state.json').write_text(json.dumps(state));m.BASE=base;m.ROOT=root;m.INSTALL=install;m._runtime_tree=base/'published'/identity/'migration'
 manager=SimpleNamespace(state=state,public_key=b'fixture',_process=lambda:(True,{'pid':123}))
 monkeypatch.setattr(manager_mod,'Manager',lambda *a:manager);monkeypatch.setattr(operation_lock,'operation_lock',lambda *a,**k:nullcontext());monkeypatch.setattr(mac_release_publish,'lock',lambda:nullcontext());monkeypatch.setattr(m.os,'geteuid',lambda:0)
 settings={'cleanup_enabled':True,'apply_cleanup':True};m.config=lambda:settings;m.health=lambda:True
 monkeypatch.setattr(m.policy,'maintenance_admitted',lambda *a,**k:True)
 old=root/'old.tmbackup';old.write_bytes(b'protected-original')
 proof={'state_sha256':m.policy.digest(root/'state.json'),'backups':[{'path':str(old),'sha256':'b'*64,'protected':False,'recognized':True,'programs':None}],'missing_owner_preserved_sha256':[]}
 m.worker_child=lambda _:SimpleNamespace(returncode=0,stdout=json.dumps(proof));m.delivery_receipts=lambda *a,**k:{};m.process_values=lambda:[];m.development_program_references=lambda:set()
 removed=[];monkeypatch.setattr(m.policy,'unlink_verified',lambda *a:removed.append(a))
 with pytest.raises(ValueError,match='Automatic deletion of formal backups is forbidden'):m.root_cleanup()
 assert not removed and old.read_bytes()==b'protected-original'

@pytest.mark.parametrize('available_delta,created',[(0,True),(-1,False)])
def test_snapshot_admission_uses_measured_budget_and_keeps_reserve(tmp_path,m,monkeypatch,available_delta,created):
 from desktop_assistant import manager as manager_mod,credential_store,data_recovery
 base=tmp_path/'base';base.mkdir();(base/'publisher-public.pem').write_text('fixture');root=base/'instance';root.mkdir();(root/'shared/data').mkdir(parents=True);(root/'shared/data/carton_erp.sqlite3').write_bytes(b'x'*100)
 (root/'preferences.json').write_text(json.dumps({'protected_password':'fixture-reference'}));points=tmp_path/'points';points.mkdir()
 identity='a'*64;state={'current':identity};manager=SimpleNamespace(state=state,public_key=b'fixture',lock=lambda:nullcontext(),_process=lambda:(True,{'pid':123}))
 m.BASE=base;m.ROOT=root;m.POINTS=points;m._runtime_tree=base/'published'/identity/'migration'
 monkeypatch.setattr(manager_mod,'Manager',lambda *a:manager);monkeypatch.setattr(credential_store,'unprotect_backup',lambda *a:'fixture-only');monkeypatch.setattr(m.os,'geteuid',lambda:499)
 m.check_point_directory=lambda:None;m.health=lambda:True;monkeypatch.setattr(m.policy,'maintenance_admitted',lambda *a,**k:True)
 settings={'backup_enabled':True,'database_interval_seconds':900,'data_interval_seconds':0,'backup_minimum_free_bytes':150*10**9,'minimum_free_bytes':200*10**9};m.config=lambda:settings
 monkeypatch.setattr(m.file_points,'fingerprint',lambda *a:{});budget=m.backup_workspace_budget(root/'shared',kind='database')['temporary_budget_bytes']
 free=150*10**9+budget+available_delta;assert free<200*10**9
 monkeypatch.setattr(m.shutil,'disk_usage',lambda *a:SimpleNamespace(free=free))
 m.completed_points=lambda:[json.loads(p.read_text()) for p in points.glob('*.tmdb.json')]
 monkeypatch.setattr(m.file_points,'capture',lambda *a:({'name':'fixture'}, {},{}));monkeypatch.setattr(m.file_points,'garbage_collect',lambda *a:[])
 monkeypatch.setattr(m.policy,'point_retirements',lambda *a,**k:[])
 calls=[]
 def create(shared,package,output,*args,**kwargs):
  calls.append(str(output));output.write_bytes(b'fixture-cipher')
  return {'restored_verified':True,'backup_sha256':'b'*64,'backup_bytes':14,'program_sha256':identity,'format':'fixture'}
 monkeypatch.setattr(data_recovery,'create',create)
 result=m.service_action('service-backup')
 assert bool(calls)==created
 assert result['results'][0]['status']==('verified' if created else 'insufficient-workspace')
 assert len(list(points.glob('*.tmdb')))==int(created)
