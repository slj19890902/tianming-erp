"""Build the allowlisted synthetic Windows test kit from committed Git bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


MODULES = ('__init__', 'attachments', 'cleanup', 'import_existing', 'onboarding',
           'preflight', 'release_request', 'signing', 'source_paths', 'storage',
           'windows', 'windows_private_workspace')
TESTS = ('windows_private_workspace', 'signing', 'release_request', 'onboarding_powershell')
SOURCE_FILES = tuple(f'desktop_assistant/{name}.py' for name in MODULES) + tuple(
    f'tests/desktop_assistant/test_{name}.py' for name in TESTS)
HARNESS_FILES = {
    'run_tests.py': 'scripts/uat/factory_primitives_runner.py',
    'README.md': 'scripts/uat/factory_primitives_README.md',
    'pytest.ini': 'scripts/uat/factory_primitives_pytest.ini',
    'requirements.txt': 'scripts/uat/factory_primitives_requirements.txt',
    'windows.ps1': 'scripts/uat/factory_primitives_windows.ps1',
    'windows-wheel-audit.json': 'scripts/uat/factory_primitives_windows_wheels.json',
}


def git_bytes(repo, commit, path):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Require an exact committed Git SHA')
    entry = subprocess.check_output(['git', 'ls-tree', commit, '--', path], cwd=repo).decode()
    if not entry.startswith(('100644 blob ', '100755 blob ')):
        raise ValueError('Allowlisted source is missing or is not a regular Git blob')
    return subprocess.check_output(['git', 'show', f'{commit}:{path}'], cwd=repo)


def build(repo, source_commit, harness_commit, output):
    contents, files = {}, {}
    for destination, original, commit in (
            [(p, p, source_commit) for p in SOURCE_FILES]
            + [(p, original, harness_commit) for p, original in HARNESS_FILES.items()]):
        raw = git_bytes(repo, commit, original)
        contents[destination] = raw
        files[destination] = {'source_path': original, 'source_commit': commit,
                              'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    manifest = {'type': 'tianming.factory-synthetic-tests.v1',
                'source_commit': source_commit, 'harness_commit': harness_commit,
                'tests': list(SOURCE_FILES[len(MODULES):]), 'files': files,
                'contains_business_data': False, 'contains_real_credentials': False,
                'not_a_runtime_release': True}
    contents['manifest.json'] = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                           indent=2).encode('utf-8')
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(contents.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 10, 11, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, raw)
    with zipfile.ZipFile(output) as archive:
        if set(archive.namelist()) != set(contents) or archive.testzip() is not None:
            raise ValueError('Test kit readback failed')
        for name, raw in contents.items():
            if archive.read(name) != raw:
                raise ValueError('Test kit bytes differ from committed inputs')
    with Path(output).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'source_commit': source_commit, 'harness_commit': harness_commit,
            'sha256': digest, 'bytes': Path(output).stat().st_size, 'files': len(contents)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--harness-commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.source_commit, args.harness_commit, args.output)))
