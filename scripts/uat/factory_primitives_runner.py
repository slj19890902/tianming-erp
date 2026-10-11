"""Run only the fixed synthetic tests, without ERP conftest or external plugins."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET


DEPENDENCIES = {'pytest': '8.4.2', 'cryptography': '50.0.2', 'psutil': '7.2.2',
                'iniconfig': '2.3.1', 'packaging': '26.3', 'pluggy': '1.6.0',
                'Pygments': '2.21.0', 'cffi': '2.1.1', 'pycparser': '3.11'}
MODULES = ('__init__', 'attachments', 'cleanup', 'import_existing', 'onboarding',
           'preflight', 'release_request', 'signing', 'source_paths', 'storage',
           'windows', 'windows_private_workspace')
TESTS = tuple('tests/desktop_assistant/test_' + name + '.py' for name in (
    'windows_private_workspace', 'signing', 'release_request', 'onboarding_powershell'))
MEMBERS = {f'desktop_assistant/{name}.py' for name in MODULES} | set(TESTS) | {
    'run_tests.py', 'README.md', 'requirements.txt', 'pytest.ini',
    'windows.ps1', 'windows-wheel-audit.json'}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _no_link(path):
    for part in (path, *path.parents):
        value = part.lstat()
        if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Test paths cannot contain links or reparse points')


def verify(root, source_commit, harness_commit):
    _no_link(root)
    if any(not re.fullmatch(r'[0-9a-f]{40}', value) for value in (source_commit, harness_commit)):
        raise ValueError('Exact source and harness Git commits are required')
    paths = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            _no_link(path)
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                paths.add(path.relative_to(root).as_posix())
            else:
                raise ValueError('Unexpected test kit filesystem object')
    if paths != MEMBERS | {'manifest.json'}:
        raise ValueError('Test kit has missing or extra files')
    manifest = json.loads((root / 'manifest.json').read_bytes())
    if (manifest.get('type') != 'tianming.factory-synthetic-tests.v1'
            or manifest.get('source_commit') != source_commit
            or manifest.get('harness_commit') != harness_commit
            or manifest.get('tests') != list(TESTS)
            or set(manifest.get('files', {})) != MEMBERS):
        raise ValueError('Test kit does not match the reviewed commits and tests')
    for name, evidence in manifest['files'].items():
        path = root / name
        if path.stat().st_size != evidence['bytes'] or digest(path) != evidence['sha256']:
            raise ValueError('Test kit content changed: ' + name)
    return manifest


def run(root, work, source_commit, harness_commit):
    if sys.version_info[:2] != (3, 12):
        raise ValueError('This fixed kit requires an approved Python 3.12 test interpreter')
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ValueError('Invoke the approved interpreter with -I -B')
    verify(root, source_commit, harness_commit)
    required = {**DEPENDENCIES, **({'colorama': '0.4.6'} if sys.platform == 'win32' else {})}
    versions = {name: importlib.metadata.version(name) for name in required}
    if versions != required:
        raise ValueError('Dependency versions differ; report them without installing or changing production')
    if not work.is_absolute():
        raise ValueError('Use a new absolute directory within the approved task parent')
    _no_link(work.parent)
    root = root.resolve(strict=True)
    work = work.parent.resolve(strict=True) / work.name
    if work.exists() or work.is_symlink():
        raise ValueError('Work directory already exists; retain evidence and do not overwrite')
    if work == root or root in work.parents or work in root.parents:
        raise ValueError('Keep test outputs outside the immutable kit')
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        kernel.GetWindowsDirectoryW.restype = wintypes.UINT
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetWindowsDirectoryW(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise ValueError('Cannot verify Windows system tools')
        system = Path(buffer.value)
        if Path(os.environ.get('SystemRoot', '')).resolve() != system.resolve():
            raise ValueError('SystemRoot differs from the actual Windows directory')
        os.environ['PATH'] = os.pathsep.join(map(str, (
            Path(sys.executable).parent, system / 'System32', system,
            system / 'System32/WindowsPowerShell/v1.0')))
    work.mkdir(mode=0o700)
    temporary, results = work / 'temp', work / 'results'
    temporary.mkdir(); results.mkdir()
    # These changes affect only this test process and its child fixture shells.
    for key in list(os.environ):
        if key.startswith(('ERP_', 'TM_ERP_', 'PYTEST_')) or key in {'PYTHONPATH', 'PYTHONHOME'}:
            del os.environ[key]
    os.environ.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                      PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                      PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', ERP_ENVIRONMENT='test',
                      ERP_HOME_REHEARSAL='1', ERP_DATABASE_PATH=str(temporary / 'synthetic.sqlite3'),
                      ERP_BACKUP_DIR=str(temporary / 'backups'),
                      ERP_SECRET_KEY_FILE=str(temporary / 'synthetic-session.key'))
    tempfile.tempdir = str(temporary)
    sys.path.insert(0, str(root))
    os.chdir(work)
    import pytest
    args = [str(root / name) for name in TESTS] + [
        '-q', '-c', str(root / 'pytest.ini'), '--noconftest',
        '--basetemp', str(temporary / 'pytest'),
        '--junitxml', str(results / 'windows-tests.xml'),
        '-o', 'cache_dir=' + str(temporary / 'cache'), '--import-mode=importlib']
    with (results / 'windows-tests.log').open('x', encoding='utf-8') as output:
        with redirect_stdout(output), redirect_stderr(output):
            exit_code = int(pytest.main(args))
    verify(root, source_commit, harness_commit)
    suites = list(ET.parse(results / 'windows-tests.xml').getroot().iter('testsuite'))
    counts = {name: sum(int(s.attrib[name]) for s in suites)
              for name in ('tests', 'failures', 'errors', 'skipped')}
    counts['passed'] = counts['tests'] - counts['failures'] - counts['errors'] - counts['skipped']
    expected = (51, 2) if sys.platform == 'win32' else (43, 10)
    imports_erp = any(name == 'app' or name.startswith('app.') for name in sys.modules)
    module_sources = {name: str(Path(module.__file__).resolve())
                      for name, module in sys.modules.items()
                      if name == 'desktop_assistant' or name.startswith('desktop_assistant.')}
    unexpected_source = any(not Path(path).is_relative_to(root) for path in module_sources.values())
    if (counts['tests'] != 53 or (counts['passed'], counts['skipped']) != expected
            or counts['failures'] or counts['errors'] or imports_erp or unexpected_source):
        exit_code = exit_code or 1
    receipt = {'source_commit': source_commit, 'harness_commit': harness_commit,
               'finished_at': datetime.now(timezone.utc).isoformat(),
               'python': sys.version, 'platform': sys.platform, 'dependencies': versions,
               'exit_code': exit_code, 'counts': counts, 'erp_application_imported': imports_erp,
               'tested_module_sources': module_sources,
               'synthetic_only': True, 'real_publisher_identity_read': False,
               'proves_original_factory_user_dpapi': False,
               'runtime_package_rebuilt': False, 'work_root': str(work),
               'retention': 'results and work root retained; unittest cleans owned synthetic fixtures; pytest keeps failures',
               'skip_reasons': [case.find('skipped').attrib.get('message', '')
                                for suite in suites for case in suite.iter('testcase')
                                if case.find('skipped') is not None]}
    (results / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'exit_code': exit_code, 'counts': counts, 'results': str(results)}))
    return exit_code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--expected-source-commit', required=True)
    parser.add_argument('--expected-harness-commit', required=True)
    args = parser.parse_args()
    try:
        result = run(Path(__file__).absolute().parent, args.work_root,
                     args.expected_source_commit, args.expected_harness_commit)
    except Exception:
        print('Fixed synthetic test run did not complete. Preserve the task and report the blocker; do not change permissions or production.', file=sys.stderr)
        result = 2
    raise SystemExit(result)
