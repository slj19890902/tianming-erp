from pathlib import Path
from unittest.mock import patch
import pytest
from desktop_assistant.installer import update_launcher


def fixture(tmp_path):
    payload=tmp_path/'payload';payload.mkdir();(payload/'TianmingERP-Assistant.exe').write_bytes(b'new helper')
    destination=tmp_path/'TianmingERP';destination.mkdir()
    (destination/'TianmingERP-Assistant.exe').write_bytes(b'old helper')
    (destination/'installer-release.zip').write_bytes(b'original installer package')
    (destination/'shared').mkdir();(destination/'shared/data.sqlite3').write_bytes(b'preserved facts')
    (destination/'preferences.json').write_bytes(b'preserved configuration')
    return payload,destination


def test_launcher_update_preserves_all_business_files(tmp_path):
    payload,destination=fixture(tmp_path)
    before={p.relative_to(destination):p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    update_launcher(payload,destination)
    assert (destination/'TianmingERP-Assistant.exe').read_bytes()==b'new helper'
    for path,contents in before.items():
        if path.name!='TianmingERP-Assistant.exe':assert (destination/path).read_bytes()==contents
    assert not list(destination.glob('*.tmp'))


def test_running_launcher_failure_preserves_old_binary(tmp_path):
    payload,destination=fixture(tmp_path)
    with patch('desktop_assistant.installer.os.replace',side_effect=PermissionError):
        with pytest.raises(ValueError,match='关闭旧版'):
            update_launcher(payload,destination)
    assert (destination/'TianmingERP-Assistant.exe').read_bytes()==b'old helper'
    assert not list(destination.glob('*.tmp'))


def test_unrelated_destination_rejected(tmp_path):
    payload,destination=fixture(tmp_path);(destination/'installer-release.zip').unlink()
    with pytest.raises(ValueError,match='已有'):
        update_launcher(payload,destination)
