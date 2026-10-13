"""Build declarations are code protocols, never installation or authorization."""
from pathlib import Path
import pytest
from desktop_assistant import build

ROOT = Path(build.__file__).resolve().parent.parent

def sources():
    value={str(p.relative_to(ROOT)):p.read_bytes()
           for p in (ROOT/'desktop_assistant').glob('*.py')}
    for name in ('app/main.py','app/core/home_rehearsal.py'):
        value[name]=(ROOT/name).read_bytes()
    return value

@pytest.mark.parametrize('stream',[False,True])
def test_complete_current_mac_protocols_and_opt_in_broker(stream):
    value=build.current_mac_capabilities(sources(),stream_enabled=stream)
    extra = {'factory_owner_current_mac_v1':1} if 'desktop_assistant/mac_publisher_trust.py' in sources() else {}
    assert value==dict(current_mac_worker_v1=1,current_mac_audit_v3=1,current_mac_mounted_audit_v1=1,
        factory_current_mac_execution_v3=1,**extra,**({'current_mac_worker_broker_v1':1} if stream else {}))

@pytest.mark.parametrize('missing',[
    'current_mac_worker.py','current_mac_worker_contract.py','current_mac_target_audit.py',
    'current_mac_cutover.py','current_mac_cutover_contract.py','current_mac_publication.py',
    'mac_factory_runtime.py','mac_published_runtime.py','current_mac_smb_broker.py','mounted_nas_proof.py','mac_mount.py'])
def test_incomplete_code_never_declares_protocol(missing):
    value=sources();value.pop('desktop_assistant/'+missing)
    with pytest.raises(ValueError):build.current_mac_capabilities(value,stream_enabled=True)
