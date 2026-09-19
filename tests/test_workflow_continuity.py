from pathlib import Path
import subprocess

def test_workflow_continuity():
    result=subprocess.run(['node','tests/workflow_continuity.cjs'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stdout+result.stderr
