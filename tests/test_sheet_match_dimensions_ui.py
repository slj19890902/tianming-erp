"""Both warehouse surfaces consume the same mm basis and near-size flag."""
from pathlib import Path
import re
import subprocess
import shutil
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_and_desktop_share_matching_contract():
    desktop=(ROOT/'factory_twin/frontend/src/MaterialCandidates.tsx').read_text(encoding='utf-8')
    mobile=(ROOT/'static/mobile_erp.html').read_text(encoding='utf-8')
    for source in (desktop,mobile):
        assert 'material-candidates' in source
        assert 'dimension_basis' in source
        assert 'box_styles' in source
        assert '不含衬板' in source and '仅衬板' in source
        assert 'match_reason' in source


def test_mobile_inline_scripts_parse(tmp_path):
    node=shutil.which('node') or 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    if not Path(node).exists(): pytest.skip('Node unavailable')
    source=(ROOT/'static/mobile_erp.html').read_text(encoding='utf-8')
    scripts=[s for s in re.findall(r'<script\b[^>]*>(.*?)</script>',source,re.S) if s.strip()]
    assert scripts
    for index,script in enumerate(scripts):
        path=tmp_path/f'mobile-{index}.js'
        path.write_text(script,encoding='utf-8')
        subprocess.run([node,'--check',str(path)],check=True,capture_output=True)
