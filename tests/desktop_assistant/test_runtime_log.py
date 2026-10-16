import os
from pathlib import Path
import subprocess
import sys
import pytest
from desktop_assistant.runtime_log import BoundedLog


def test_existing_large_console_is_bounded_without_leaving_old_inode_open(tmp_path):
    path = tmp_path / 'server.log'
    path.write_bytes(b'old' * 1000 + b'RECENT')
    path.chmod(0o600)
    with path.open('rb') as inherited:
        sink = BoundedLog(tmp_path, max_bytes=128, backups=1)
        try:
            assert path.stat().st_ino == os.fstat(inherited.fileno()).st_ino
            assert path.stat().st_size == 128
            assert path.read_bytes().endswith(b'RECENT')
            sink.write(b'next')
            assert sum(p.stat().st_size for p in tmp_path.glob('server.log*')) <= 256
        finally:
            sink.close()


def test_active_python_and_native_console_rotate_without_parent_pipe(tmp_path):
    program = '''
import os,sys
from desktop_assistant.runtime_log import capture_console
with capture_console(sys.argv[1],max_bytes=1024,backups=2):
 for i in range(50):
  print('row-'+str(i)+'x'*100,flush=True)
  os.write(2,b'native-output\\n')
 print('END-OF-LOG',flush=True)
print('console-restored',flush=True)
'''
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'console-restored\n'
    logs = sorted(tmp_path.glob('server.log*'))
    assert len(logs) == 3 and sum(p.stat().st_size for p in logs) <= 3072
    assert b'END-OF-LOG' in (tmp_path / 'server.log').read_bytes()
    assert b'native-output' in b''.join(p.read_bytes() for p in logs)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'foreign-mode'])
def test_log_rotation_refuses_unsafe_paths(tmp_path, kind):
    target = tmp_path / 'protected'; target.write_bytes(b'keep'); target.chmod(0o600)
    path = tmp_path / 'server.log.1'
    if kind == 'symlink': path.symlink_to(target)
    elif kind == 'hardlink': os.link(target, path)
    else: path.write_bytes(b'old'); path.chmod(0o644)
    sink = BoundedLog(tmp_path, max_bytes=128, backups=1)
    try:
        with pytest.raises(ValueError): sink.write(b'x' * 256)
    finally: sink.close()
    assert target.read_bytes() == b'keep'


def test_storage_failure_drains_output_and_records_attention_without_blocking_server(tmp_path):
    import json
    (tmp_path / 'protected').write_text('keep')
    (tmp_path / 'server.log.1').symlink_to(tmp_path / 'protected')
    program = '''
import os,sys
from desktop_assistant.runtime_log import capture_console
with capture_console(sys.argv[1],max_bytes=128,backups=1):
 os.write(1,b'x'*200000)
print('business-continues',flush=True)
'''
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'business-continues\n'
    assert (tmp_path / 'protected').read_text() == 'keep'
    assert json.loads((tmp_path / 'runtime-log-status.json').read_text())['status'] == 'attention'
