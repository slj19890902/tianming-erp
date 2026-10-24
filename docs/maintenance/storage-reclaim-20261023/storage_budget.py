#!/usr/bin/env python3
"""Reserve 200 decimal GB before a foreground ERP development storage job."""
from __future__ import annotations
import argparse,fcntl,json,os,shutil,stat,subprocess,sys
from decimal import Decimal,InvalidOperation
from pathlib import Path
RESERVE_BYTES=200_000_000_000
PROJECT=Path('/Users/kimiyf/ERP-Migration')
LOCK=PROJECT/'.heavy-storage-job.lock'

def run(argv=None):
 parser=argparse.ArgumentParser(description='ERP large-copy/build/rehearsal budget check; never runs as root or changes business data.')
 parser.add_argument('--budget-gb',required=True,help='Measured maximum additional workspace, decimal GB; include backup/restore phases.')
 parser.add_argument('--check-only',action='store_true')
 parser.add_argument('command',nargs=argparse.REMAINDER)
 args=parser.parse_args(argv)
 try:
  budget=Decimal(args.budget_gb)
  if not budget.is_finite() or budget<0 or budget>200:raise ValueError()
  requested=int(budget*1_000_000_000)
 except (InvalidOperation,ValueError,OverflowError):parser.error('budget must be a finite number between 0 and 200 GB')
 if os.geteuid()==0:parser.error('Run the budget check as the developer before requesting a privileged maintenance/release entry.')
 command=args.command[1:] if args.command[:1]==['--'] else args.command
 if not args.check_only and (not command or requested==0):parser.error('A command and positive measured budget are required.')
 if LOCK.is_symlink() or any(p.is_symlink() for p in LOCK.parents):raise ValueError('Budget lock path contains a link')
 fd=os.open(LOCK,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'r+') as lock:
  info=os.fstat(lock.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1:raise ValueError('Budget lock identity changed')
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:
   print(json.dumps({'status':'another-heavy-job-active','command_started':False,'reserve_bytes':RESERVE_BYTES}),flush=True);return 75
  available=shutil.disk_usage(PROJECT).free
  admitted=available-requested>=RESERVE_BYTES
  print(json.dumps({'status':'admitted' if admitted else 'insufficient-storage-budget','available_bytes':available,'requested_additional_bytes':requested,'reserve_bytes':RESERVE_BYTES,'projected_available_bytes':available-requested,'command_started':bool(admitted and not args.check_only)}),flush=True)
  if not admitted:return 78
  if args.check_only:return 0
  # Holding one shared lock prevents simultaneous full-copy jobs from each
  # spending the same free-space budget. The command must stay foreground.
  result=subprocess.run(command,check=False)
  print(json.dumps({'status':'command-finished','returncode':result.returncode,'available_bytes':shutil.disk_usage(PROJECT).free}),flush=True)
  return result.returncode
if __name__=='__main__':raise SystemExit(run())
