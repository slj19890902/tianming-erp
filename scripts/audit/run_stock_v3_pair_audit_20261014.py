"""Invoke existing run_task with canonical Python and explicit local dependencies."""
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
SITE=Path('/Users/kimiyf/ERP-Migration/venv/lib/python3.12/site-packages')
sys.path[:0]=[str(ROOT),str(SITE)]
from scripts.uat.run_task import run_task
SOURCE=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/managed-tasks/task-2a4eba8403ba40fba6eff95f2961e553/copies/carton_erp.sqlite3')
BASE=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/stock-governance-sol/managed-tasks')
code='import sys,runpy;sys.path[:0]='+repr([str(ROOT),str(SITE)])+';runpy.run_path('+repr(str(ROOT/'scripts/audit/stock_v3_pair_rollback_20261014.py'))+',run_name="__main__")'
report=run_task(SOURCE,BASE,[str(Path(sys.executable).resolve()),'-I','-B','-c',code],port=18992)
print(json.dumps(report,ensure_ascii=False))
raise SystemExit(report.get('returncode',1))
