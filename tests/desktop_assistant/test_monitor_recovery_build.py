"""Exercise real build.main source admission and emitted manifest declarations."""
import ast
from pathlib import Path
import sys

import pytest

from desktop_assistant import build, native_runtime, ocr_models, release_request
from desktop_assistant import physical_kit_reader_contract as kit
from desktop_assistant.mac_release_publish import SERVICE_RESTORE_FILES
from desktop_assistant.mac_factory_runtime import EXECUTION_FILES


@pytest.mark.parametrize('platform,missing', [('macos-arm64', None), ('windows', None)] + [
    ('macos-arm64', name) for name in ('desktop_assistant/mac_system_upgrade_recovery.py',
        'desktop_assistant/mac_system_upgrade_recovery_contract.py',
        'desktop_assistant/physical_kit_reader_contract.py',
        'app/services/emergency_kit_correction_20261014.py',
        'app/services/entry_source_valuation.py', 'app/core/inventory_entry_guard.py')])
def test_exact_caps_require_all_implementation_sources(tmp_path, monkeypatch, platform, missing):
    repo = Path(build.__file__).resolve().parents[1]
    sources = {str(p.relative_to(repo)): p.read_bytes() for p in (repo / 'desktop_assistant').glob('*.py')}
    sources.update({name: b'# synthetic dependency' for name in SERVICE_RESTORE_FILES | EXECUTION_FILES})
    for node in ast.walk(ast.parse(Path(build.__file__).read_text())):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'reader_sources' for t in node.targets):
            sources.update({name: b'# synthetic reader dependency' for name in ast.literal_eval(node.value)})
    if missing: sources.pop(missing)
    output = tmp_path / 'output'
    public = tmp_path / 'public'; public.write_bytes(b'synthetic public')
    monkeypatch.setattr(sys, 'argv', ['build', '--repo', str(repo), '--runtime-base', str(tmp_path / 'runtime'),
        '--site-packages', str(tmp_path / 'site'), '--ocr-models', str(tmp_path / 'ocr'), '--output', str(output),
        '--runtime-platform', platform, '--revision', 'r1', '--version', 'v1',
        '--signing-request-only', '--trusted-public-key', str(public)])
    monkeypatch.setattr(build, 'source_snapshot', lambda *a: ('f' * 40, sources))
    monkeypatch.setattr(build, 'schema_contract_from_sources', lambda *a: {})
    monkeypatch.setattr(release_request, 'public_identity', lambda *a: None)
    monkeypatch.setattr(release_request, 'prepare_request', lambda *a: {})
    monkeypatch.setattr(native_runtime, 'assemble', lambda *a: a[2].mkdir())
    monkeypatch.setattr(native_runtime, 'verify_relocation', lambda *a: {})
    monkeypatch.setattr(native_runtime, 'executable_files', lambda *a: [])
    monkeypatch.setattr(build.shutil, 'copytree', lambda src, dst, **kw: dst.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(ocr_models, 'copy_models', lambda *a: [])
    monkeypatch.setattr(ocr_models, 'prepare_readonly_layout', lambda *a: None)
    manifests = []
    monkeypatch.setattr(build, 'pack_tree', lambda tree, path, value, key: manifests.append(value))
    if missing:
        with pytest.raises(ValueError): build.main()
        assert not output.exists() and not manifests
    else:
        build.main()
        assert len(manifests) == 1
        value = manifests[0]['reader_capabilities'][kit.CAPABILITY]
        assert type(value) is int and value == 1
        caps = manifests[0].get('runtime_capabilities', {})
        if platform == 'macos-arm64':
            value = caps['system_upgrade_split_recovery_v1']
            assert type(value) is int and value == 1
        else: assert 'system_upgrade_split_recovery_v1' not in caps
