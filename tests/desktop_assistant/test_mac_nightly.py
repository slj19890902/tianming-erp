import plistlib
import sys
from types import SimpleNamespace

import pytest

from desktop_assistant import mac_nightly
from tests.desktop_assistant.test_mac_service import signed_case

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac scheduler validation')


def test_calendar_does_not_run_during_settings_save(tmp_path):
    spec=mac_nightly.definition(tmp_path,tmp_path/'python',tmp_path/'entry',tmp_path/'public','a'*64)
    assert spec['StartCalendarInterval']=={'Minute':0}
    assert spec['RunAtLoad'] is False and spec['KeepAlive'] is False
    assert spec['ProgramArguments'][1:3]==['-I','-B']
    assert spec['EnvironmentVariables']=={'ERP_HOME_REHEARSAL':'1'}
    assert 'password' not in plistlib.dumps(spec).decode().lower()


def test_registered_identical_job_can_be_reused_for_password_changes(signed_case,monkeypatch):
    manager=signed_case.manager
    agents=signed_case.root/'nightly-agents';agents.mkdir()
    monkeypatch.setattr(mac_nightly,'_launch_agents',lambda:agents)
    loaded=False;calls=[]
    def run(args,**kw):
        nonlocal loaded
        calls.append(args)
        if args[1]=='print':return SimpleNamespace(returncode=0 if loaded else 1)
        loaded=True;return SimpleNamespace(returncode=0)
    monkeypatch.setattr(mac_nightly.subprocess,'run',run)
    with manager.lock():
        assert mac_nightly.register_locked(manager)['reused'] is False
    with manager.lock():
        assert mac_nightly.register_locked(manager)['reused'] is True
    assert sum(args[1]=='bootstrap' for args in calls)==1
    monkeypatch.setattr(mac_nightly,'local_root',lambda root:root)
    with manager.lock():
        assert mac_nightly.unregister_locked(manager)['disabled']
    assert not list(agents.iterdir())
    assert len(list((manager.root/'control/nightly-history').glob('*.plist')))==1


def test_unknown_existing_task_is_not_adopted(signed_case,monkeypatch):
    agents=signed_case.root/'nightly-agents';agents.mkdir()
    monkeypatch.setattr(mac_nightly,'_launch_agents',lambda:agents)
    monkeypatch.setattr(mac_nightly.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    with signed_case.manager.lock(),pytest.raises(ValueError,match='拒绝接管'):
        mac_nightly.register_locked(signed_case.manager)
    assert not list(agents.iterdir())


def test_windows_import_can_save_mac_backup_settings_without_false_schedule(signed_case,monkeypatch):
    from desktop_assistant import backup_settings
    from desktop_assistant.storage import write_json,read_json
    manager=signed_case.manager
    release=manager.stage_release(signed_case.package)
    write_json(manager.root/'state.json',{'current':release['id']})
    monkeypatch.setattr(backup_settings,'protect',lambda value:'synthetic-keychain-ref')
    monkeypatch.setattr(mac_nightly.subprocess,'run',lambda *a,**kw:pytest.fail('Windows package cannot schedule Mac runtime'))
    result=backup_settings.save_backup_settings(manager,str(signed_case.nas),
        'synthetic-backup-password','synthetic-backup-password',manager.root/'unused')
    assert '尚未安排' in result
    settings=read_json(manager.root/'preferences.json')
    assert settings['nas']==str(signed_case.nas)
    assert settings['nightly_schedule']['registered'] is False
    assert settings['protected_password']=='synthetic-keychain-ref'
