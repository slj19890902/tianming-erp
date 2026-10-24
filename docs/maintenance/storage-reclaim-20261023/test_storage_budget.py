import contextlib,fcntl,importlib.util,io,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('budget_under_test',Path(__file__).with_name('storage_budget.py'));budget=importlib.util.module_from_spec(spec);spec.loader.exec_module(budget)

class BudgetGate(unittest.TestCase):
 def invoke(self,root,available,args):
  with patch.object(budget,'PROJECT',root),patch.object(budget,'LOCK',root/'lock'),patch.object(budget.shutil,'disk_usage',return_value=SimpleNamespace(free=available)),contextlib.redirect_stdout(io.StringIO()):return budget.run(args)
 def test_insufficient_space_never_starts_command(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve();marker=root/'must-not-exist'
   code='from pathlib import Path;Path('+repr(str(marker))+').write_text("bad")'
   self.assertEqual(self.invoke(root,204_999_999_999,['--budget-gb','5','--',sys.executable,'-c',code]),78);self.assertFalse(marker.exists())
 def test_admitted_command_preserves_exit_code(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve();marker=root/'ran'
   code='from pathlib import Path;Path('+repr(str(marker))+').write_text("yes");raise SystemExit(17)'
   self.assertEqual(self.invoke(root,205_000_000_000,['--budget-gb','5','--',sys.executable,'-c',code]),17);self.assertEqual(marker.read_text(),'yes')
 def test_second_heavy_job_cannot_spend_same_space(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve()
   with (root/'lock').open('w+') as held:
    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
    self.assertEqual(self.invoke(root,400_000_000_000,['--budget-gb','5','--check-only']),75)
 def test_symlink_lock_refused(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve();(root/'real').touch();(root/'lock').symlink_to(root/'real')
   with self.assertRaises(ValueError):self.invoke(root,400_000_000_000,['--budget-gb','5','--check-only'])
if __name__=='__main__':unittest.main()
