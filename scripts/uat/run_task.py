"""Run a test against a newly owned UAT copy; keep evidence, recycle success.

The command must stay in the foreground. A detached child keeps its data via
the idle guard. Existing UAT roots, checkouts, and source DBs are never removed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from app.core.uat_isolation import prepare_uat_layout, isolated_child_environment
from desktop_assistant.cleanup import local_path, remove_owned_tree
from desktop_assistant.storage import write_json


def run_task(source: Path, base: Path, command: list[str], *, port=18990, keep_copy=False,
             layout_source: Path | None = None, timeout_seconds=1800) -> dict:
    base = local_path(base)
    if not 18001 <= port <= 19999:
        raise ValueError('测试端口必须在 18001..19999')
    if not 0 < timeout_seconds <= 7200:
        raise ValueError('测试等待必须有界：大于0且不超过7200秒')
    for protected in (PROJECT, Path('D:/TianmingERP'), Path('D:/纸箱厂erp软件搭建')):
        if base == protected or protected in base.parents:
            raise ValueError('测试副本不能建立在正式安装或代码工作区内')
    if any((p / '.git').exists() for p in (base, *base.parents)):
        raise ValueError('测试副本不能建立在 Git 工作区内')
    job = base / ('task-' + uuid.uuid4().hex)
    job.mkdir(mode=0o700, parents=True, exist_ok=False)
    copies = job / 'copies'
    report = {'schema': 'tianming.test-task.v1', 'status': 'preparing',
              'job': str(job), 'source': str(source), 'copies': str(copies),
              'created_at': datetime.now(timezone.utc).isoformat(), 'cleanup': 'retained',
              'keep_copy': bool(keep_copy), 'manual_acceptance_pending': bool(keep_copy)}
    receipt = job / 'result.json'
    write_json(receipt, report)
    try:
        layout = prepare_uat_layout(
            root=copies, database=copies/'carton_erp.sqlite3', port=port,
            layout_source=layout_source or PROJECT/'static/factory_maps/twin_layout_v1.json',
            source_database=source, forbidden_roots=[PROJECT, Path('D:/TianmingERP')],
        )
        overrides = dict(layout['environment'])
        overrides.update(ERP_ENVIRONMENT='test', ERP_DATABASE_PATH=layout['paths']['database'],
                         ERP_UAT_ROOT=layout['root'], ERP_UAT_ISOLATION_ID=layout['isolation_id'],
                         ERP_PORT=str(port), ERP_BIND_HOST='127.0.0.1',
                         ERP_SESSION_COOKIE_NAME=layout['cookie_name'],
                         ERP_SESSION_COOKIE_SECURE='false', ERP_WORKERS='1')
        env = isolated_child_environment(overrides=overrides, temp_dir=layout['temp_dir'], python=Path(sys.executable))
        # The runner owns copies only. stdout/stderr and the receipt survive it.
        with (job/'stdout.log').open('wb') as stdout, (job/'stderr.log').open('wb') as stderr:
            process = subprocess.Popen(command, cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            report.update(status='running', pid=process.pid)
            write_json(receipt, report)
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                report.update(status='timed_out',returncode=124,
                              cleanup_reason='bounded wait expired; child not killed; owned copy retained')
                return report
        report.update(returncode=returncode, status='passed' if returncode == 0 else 'failed')
        if returncode == 0 and not keep_copy:
            try:
                report['deleted_bytes'] = remove_owned_tree(copies, job)
                report['cleanup'] = 'removed'
            except (OSError, ValueError) as error:
                report['cleanup_reason'] = str(error)
                try:
                    from desktop_assistant.task_retention import copy_identity
                    report['copy_identity'] = copy_identity(copies)
                except (OSError, ValueError):
                    pass
        else:
            report['cleanup_reason'] = 'failed test or explicit keep-copy; evidence retained'
    except BaseException as error:
        report.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=str(error), cleanup_reason='incomplete task; preserved for diagnosis')
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(receipt, report)
        try:
            from desktop_assistant.task_retention import reconcile
            reconcile(base, apply=True, exclude_jobs=(job,))
        except Exception:
            pass  # Housekeeping cannot change the test result or stop ERP.
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-database', type=Path, required=True)
    default = Path('D:/ERP-UAT/managed-tasks') if os.name == 'nt' and Path('D:/').exists() else Path(tempfile.gettempdir())/'tm-erp-tasks'
    parser.add_argument('--base-dir', type=Path, default=default)
    parser.add_argument('--port', type=int, default=18990)
    parser.add_argument('--keep-copy', action='store_true')
    parser.add_argument('--layout-source', type=Path,
                        help='Use the captured published map when verifying live map bindings')
    parser.add_argument('--timeout-seconds', type=int, default=1800)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('provide a foreground test command after --')
    result = run_task(args.source_database, args.base_dir, command, port=args.port,
                      keep_copy=args.keep_copy, layout_source=args.layout_source,
                      timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False))
    return result['returncode']


if __name__ == '__main__':
    raise SystemExit(main())
