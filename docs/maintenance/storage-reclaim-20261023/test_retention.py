"""Exercise the exact candidate decision block with synthetic identities only."""
import ast, importlib.util, unittest
from pathlib import Path
D=Path(__file__).parent
spec=importlib.util.spec_from_file_location('installed_policy','/Library/TianmingERP-LocalMaintenance/storage_policy.py')
policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
source=(D/'native.py').read_text()
start=source.index("            missing = proof.get('missing_owner_preserved_sha256', [])")
end=source.index('            points = completed_points()',start)
block='\n'.join(line[12:] for line in source[start:end].splitlines())
code=compile(block,'candidate-native-retention','exec')
A,B,C,D0,E,F=('a'*64,'b'*64,'c'*64,'d'*64,'e'*64,'f'*64)
def decide(*,missing=(),unknown=False,protected=True):
 manifests={h:{} for h in (A,B,C,D0,E,F)}
 row={'protected':protected,'sha256':'1'*64,'path':'/fixture/old.tmbackup','programs':None if unknown else [E],'recognized':True}
 scope={'proof':{'missing_owner_preserved_sha256':list(missing),'path':'/fixture/latest.tmbackup','backups':[row]},'report':{'retained':[]},'settings':{},'extra':{C},'runtime_extra':{C},'manifests':manifests}
 exec(code,scope)
 originals,expanded=policy.program_retention_sets({'current':A,'previous':B},manifests,runtime_dependencies=scope['runtime_extra'],archive_dependencies=scope['extra'],point_programs=[D0])
 return scope,originals,expanded
class RetentionTests(unittest.TestCase):
 def test_missing_historical_preserves_all_signed_originals(self):
  _,originals,_=decide(missing=['2'*64]);self.assertEqual(originals,{A,B,C,D0,E,F})
 def test_missing_historical_not_an_extra_live_runtime(self):
  _,_,expanded=decide(missing=['2'*64]);self.assertEqual(expanded,{A,B,C,D0})
 def test_missing_history_incident_remains_visible(self):
  s,_,_=decide(missing=['2'*64]);self.assertEqual(s['report']['missing_owner_preserved_sha256'],['2'*64]);self.assertEqual(s['report']['retained'][0]['kind'],'missing-historical-backup-originals')
 def test_existing_unknown_backup_still_protects_every_expansion(self):
  _,o,x=decide(unknown=True);self.assertEqual(o,x);self.assertEqual(x,{A,B,C,D0,E,F})
 def test_missing_plus_unknown_still_protects_every_expansion(self):
  _,_,x=decide(missing=['2'*64],unknown=True);self.assertEqual(x,{A,B,C,D0,E,F})
 def test_known_existing_backup_preserves_its_original(self):
  _,o,x=decide();self.assertIn(E,o);self.assertNotIn(E,x)
 def test_unprotected_backup_is_rejected(self):
  with self.assertRaisesRegex(ValueError,'Automatic deletion'):decide(protected=False)
 def test_current_previous_activation_and_online_roles_stay_expanded(self):
  _,_,x=decide(missing=['2'*64]);self.assertTrue({A,B,C,D0}<=x)
 def test_entire_candidate_parses(self):ast.parse(source)
if __name__=='__main__':unittest.main()
