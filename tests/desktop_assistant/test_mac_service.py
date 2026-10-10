import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from desktop_assistant import mac_service as service
from desktop_assistant.storage import pack_tree, write_json
from tests.desktop_assistant import test_recovery as fixtures

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac launchd service validation')


@pytest.fixture
def signed_case(monkeypatch):
    instance = fixtures.RecoveryTests()
    instance.setUp()
    try:
        source = instance.root/'native-service-source'
        for name in ('main.py','app/main.py','desktop_assistant/server_entry.py',
                     'desktop_assistant/service_supervisor.py'):
            path = source/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('# synthetic signed-file fixture only\n')
        runtime = source/'runtime/bin/python3.12'
        runtime.parent.mkdir(parents=True)
        shutil.copyfile(Path(sys.executable).resolve(), runtime)
        runtime.chmod(0o755)
        package = instance.root/'native-service.zip'
        pack_tree(source,package,{'type':'tianming.release.v1','version':'service-test',
                  'revision':'r1','runtime_platform':'macos-arm64'},instance.key)
        release = instance.manager.stage_release(package)
        write_json(instance.manager.root/'state.json',{'current':release['id']})
        monkeypatch.setattr(service,'local_root',lambda root: Path(root).resolve())
        monkeypatch.setattr('desktop_assistant.runtime_platform.sys.platform','darwin')
        monkeypatch.setattr('desktop_assistant.runtime_platform.platform.machine',lambda:'arm64')
        yield instance
    finally:
        instance.tearDown()


def test_prepare_checks_signed_code_and_never_selects_cached_manifest(signed_case):
    manager = signed_case.manager
    path = service.prepare(manager)
    spec = plistlib.loads(path.read_bytes())
    assert spec['Label'] == service.label_for(manager.root)
    assert spec['ProgramArguments'][0].endswith('/runtime/bin/python3.12')
    script = Path(spec['ProgramArguments'][2])
    script.write_text('tampered fixture')
    with pytest.raises(ValueError,match='校验失败'):
        service.prepare(manager)


def test_windows_release_cannot_be_registered(signed_case):
    manager = signed_case.manager
    original = manager.stage_release(signed_case.package)
    write_json(manager.root/'state.json',{'current':original['id']})
    with pytest.raises(ValueError,match='Windows包'):
        service.prepare(manager)


def test_wrong_publisher_cannot_register_service(signed_case):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    manager = signed_case.manager
    manager.public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(InvalidSignature):
        service.prepare(manager)
    assert not list((manager.root/'control').glob('*.plist'))


def test_install_disable_only_exact_owned_label_and_archive_definition(signed_case, monkeypatch):
    manager = signed_case.manager
    agents = signed_case.root/'user-launch-agents'
    agents.mkdir()
    unrelated = agents/'unrelated.plist'
    unrelated.write_bytes(b'keep')
    monkeypatch.setattr(service,'_launch_agents',lambda:agents)
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=1 if arguments[1]=='print' else 0)
    monkeypatch.setattr(service.subprocess,'run',run)
    result = service.install(manager)
    assert result['registered'] and not result['health_verified']
    assert calls[1][1]=='bootstrap'
    assert service.disable(manager)['disabled']
    assert manager.state['manual_stop'] is True
    assert calls[-1][1]=='bootout'
    assert unrelated.read_bytes()==b'keep'
    assert len(list((manager.root/'control/service-history').glob('*.plist')))==1
    assert not (agents/(result['label']+'.plist')).exists()


def test_existing_job_is_not_adopted_or_replaced(signed_case, monkeypatch):
    monkeypatch.setattr(service.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    monkeypatch.setattr(service,'_launch_agents',lambda:pytest.fail('existing job must not be overwritten'))
    with pytest.raises(ValueError,match='同名'):
        service.install(signed_case.manager)


def test_foreign_label_cannot_be_stopped_even_with_matching_copies(signed_case, monkeypatch):
    manager = signed_case.manager
    source = service.prepare(manager)
    agents = signed_case.root/'fake-launch-agents'
    agents.mkdir()
    spec = plistlib.loads(source.read_bytes()); spec['Label']='unrelated.service'
    source.write_bytes(plistlib.dumps(spec))
    (agents/source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(service,'_launch_agents',lambda:agents)
    monkeypatch.setattr(service.subprocess,'run',lambda *a,**kw:pytest.fail('foreign job stop attempted'))
    with pytest.raises(ValueError,match='标签'):
        service.disable(manager)


def test_definition_uses_isolated_foreground_user_runtime_without_secrets(tmp_path):
    spec = service.definition(tmp_path, tmp_path/'python', tmp_path/'supervisor.py', tmp_path/'public.pem', 'a'*64)
    assert spec['ProgramArguments'][1] == '-I'
    assert spec['KeepAlive'] == {'SuccessfulExit':False}
    assert spec['EnvironmentVariables'] == {'ERP_HOME_REHEARSAL':'1'}
    assert spec['ExitTimeOut'] == 90 and spec['ThrottleInterval'] == 30
    assert spec['AbandonProcessGroup'] is True
    assert spec['ProcessType'] == 'Interactive'
    assert 'UserName' not in spec and spec['Umask'] == 0o077
    assert spec['Label'] != service.label_for(tmp_path/'another-installation')


def test_service_file_never_overwrites_or_follows_link(tmp_path):
    path = tmp_path/'owned.plist'
    service._exclusive_or_equal(path, b'original')
    service._exclusive_or_equal(path, b'original')
    with pytest.raises(ValueError, match='覆盖'):
        service._exclusive_or_equal(path, b'changed')
    linked = tmp_path/'link'
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='链接'):
        service._exclusive_or_equal(linked/'other.plist', b'no')
    assert not (tmp_path/'other.plist').exists()


@pytest.mark.skipif(sys.platform != 'darwin' or os.getenv('ERP_TEST_LAUNCHD') != '1',
                    reason='explicit short-lived current-user launchd test')
def test_real_launchd_restart_and_graceful_bootout(tmp_path):
    root = tmp_path.resolve()
    (root/'control').mkdir()
    script = root/'synthetic.py'
    script.write_text('''import os,signal,sys,time
from pathlib import Path
root=Path(sys.argv[sys.argv.index('--root')+1])
path=root/'starts.txt'
with path.open('a') as stream:
 stream.write(str(os.getpid())+'\\n');stream.flush();os.fsync(stream.fileno())
if len(path.read_text().splitlines())==1: raise SystemExit(7)
def stop(*args):
 (root/'graceful-stop.txt').write_text('stopped')
 raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
while True: time.sleep(.1)
''')
    spec = service.definition(root, Path(sys.executable), script, root/'unused-public.pem', 'a'*64)
    spec['ThrottleInterval'] = 1  # Production definition remains 30 seconds.
    path = root/(spec['Label']+'.plist')
    path.write_bytes(plistlib.dumps(spec))
    target = f'gui/{os.getuid()}/{spec["Label"]}'
    assert subprocess.run([service.LAUNCHCTL,'print',target],capture_output=True).returncode != 0
    try:
        result = subprocess.run([service.LAUNCHCTL,'bootstrap',f'gui/{os.getuid()}',str(path)],capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
        deadline = time.monotonic()+40
        while time.monotonic()<deadline:
            starts = (root/'starts.txt').read_text().splitlines() if (root/'starts.txt').exists() else []
            if len(starts)>=2:
                break
            time.sleep(.2)
        assert len(starts)==2 and starts[0]!=starts[1], 'failed synthetic process was not restarted'
        result = subprocess.run([service.LAUNCHCTL,'bootout',target],capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
        deadline = time.monotonic()+5
        while time.monotonic()<deadline and not (root/'graceful-stop.txt').exists():
            time.sleep(.1)
        assert (root/'graceful-stop.txt').read_text() == 'stopped'
    finally:
        if subprocess.run([service.LAUNCHCTL,'print',target],capture_output=True).returncode == 0:
            subprocess.run([service.LAUNCHCTL,'bootout',target],capture_output=True,check=True)
    assert subprocess.run([service.LAUNCHCTL,'print',target],capture_output=True).returncode != 0
