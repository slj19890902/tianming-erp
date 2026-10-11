import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from scripts.uat import build_factory_primitives as builder
from scripts.uat import factory_primitives_runner as runner


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def kit(tmp_path):
    repo = tmp_path / 'synthetic-repository'
    repo.mkdir()
    for name in (*builder.SOURCE_FILES, *builder.HARNESS_FILES.values()):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    # Even committed, these unrelated canaries must never enter a test kit.
    (repo / 'real-looking.db').write_bytes(b'UNRELATED SYNTHETIC CANARY')
    (repo / 'publisher.pem').write_bytes(b'UNRELATED SYNTHETIC KEY CANARY')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=repo, stderr=subprocess.PIPE)
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'synthetic fixture only')
    commit = git('rev-parse', 'HEAD').decode().strip()
    output = tmp_path / 'kit.zip'
    report = builder.build(repo, commit, commit, output)
    extracted = tmp_path / 'extracted'
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == runner.MEMBERS | {'manifest.json'}
        assert all(b'UNRELATED SYNTHETIC' not in archive.read(name) for name in archive.namelist())
        archive.extractall(extracted)  # The local allowlisted test archive only.
    return extracted.resolve(), commit, output, report


def test_committed_allowlist_verifies_and_archive_is_exclusive(kit, tmp_path):
    root, commit, output, report = kit
    manifest = runner.verify(root, commit, commit)
    assert len(manifest['tests']) == 4 and len(manifest['files']) == 22
    assert report['files'] == 23
    with pytest.raises(FileExistsError):
        builder.build(tmp_path / 'synthetic-repository', commit, commit, output)


@pytest.mark.parametrize('fault', ['changed-module', 'extra-file', 'wrong-commit', 'existing-work'])
def test_runner_refuses_bad_inputs_without_touching_existing_or_creating_work(kit, tmp_path, fault):
    root, commit, _, _ = kit
    work = tmp_path.resolve() / 'execution'
    expected = commit
    if fault == 'changed-module':
        with (root / 'desktop_assistant/signing.py').open('ab') as stream:
            stream.write(b'\n# synthetic tamper\n')
    elif fault == 'extra-file':
        (root / 'unreviewed.txt').write_text('synthetic')
    elif fault == 'wrong-commit':
        expected = '0' * 40
    else:
        work.mkdir()
        (work / 'retain').write_text('keep')
    result = subprocess.run([sys.executable, '-I', '-B', str(root / 'run_tests.py'),
                             '--work-root', str(work), '--expected-source-commit', expected,
                             '--expected-harness-commit', commit], capture_output=True)
    assert result.returncode == 2
    if fault == 'existing-work':
        assert (work / 'retain').read_text() == 'keep' and len(list(work.iterdir())) == 1
    else:
        assert not work.exists()


def test_extracted_kit_runs_original_tests_without_erp_conftest_or_business_imports(kit, tmp_path):
    root, commit, _, _ = kit
    work = tmp_path.resolve() / 'execution'
    result = subprocess.run([sys.executable, '-I', '-B', str(root / 'run_tests.py'),
                             '--work-root', str(work), '--expected-source-commit', commit,
                             '--expected-harness-commit', commit], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    receipt = json.loads((work / 'results/receipt.json').read_text())
    assert receipt['counts']['tests'] == 51
    assert receipt['counts']['failures'] == receipt['counts']['errors'] == 0
    assert receipt['erp_application_imported'] is False
    assert receipt['real_publisher_identity_read'] is False
    assert receipt['proves_original_factory_user_dpapi'] is False
    assert all(Path(path).is_relative_to(root) for path in receipt['tested_module_sources'].values())
    runner.verify(root, commit, commit)


def test_runner_refuses_normalized_output_inside_kit_before_creating_anything(kit, tmp_path):
    root, commit, _, _ = kit
    other = tmp_path.resolve() / 'other'
    other.mkdir()
    work = other / '..' / root.name / 'execution'
    original_paths = set(root.rglob('*'))
    result = subprocess.run([sys.executable, '-I', '-B', str(root / 'run_tests.py'),
                             '--work-root', str(work), '--expected-source-commit', commit,
                             '--expected-harness-commit', commit], capture_output=True)
    assert result.returncode == 2
    assert not (root / 'execution').exists()
    assert set(root.rglob('*')) == original_paths
    runner.verify(root, commit, commit)
