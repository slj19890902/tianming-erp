from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def test_managed_entry_preserves_read_only_control_path_for_web_status(monkeypatch, tmp_path):
    from desktop_assistant import server_entry

    control = tmp_path / "installation" / "control"
    control.mkdir(parents=True)
    from desktop_assistant import delivery_dispatch_contract as contract
    from desktop_assistant.storage import write_json
    identity = 'a' * 64
    shared = control.parent/'shared'
    (shared/'data').mkdir(parents=True)
    (control.parent/'releases'/identity).mkdir(parents=True)
    marker = {'reader_capability':contract.CAPABILITY, 'version':1,
              'activated_package':identity, 'pre_activation_package':identity,
              'pre_activation_backup':'synthetic-fixture',
              'activated_at':'2026-10-11T00:00:00+08:00'}
    write_json(shared/contract.MARKER, marker)
    write_json(control.parent/'state.json', {'current':identity,
        contract.STATE_KEY:contract.state_index(contract.activation(shared))})
    write_json(control.parent/'releases'/identity/'manifest.json',
               {'reader_capabilities':{contract.CAPABILITY:1}})
    # The sticky audit hook must not escape this unit test into the pytest
    # worker. Real guarded processes are covered by test_home_rehearsal/process.
    guard = Mock(return_value=True)
    monkeypatch.setattr('app.core.home_rehearsal.install_network_guard', guard)
    monkeypatch.setenv("TM_ERP_CONTROL", str(control))
    monkeypatch.setenv("TM_ERP_NONCE", "synthetic-startup")
    monkeypatch.setenv("ERP_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("ERP_PORT", "18929")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(control.parent / 'shared/data/carton_erp.sqlite3'))
    observed = {}

    class Server:
        def __init__(self, config):
            self.config = config

        def run(self):
            import os

            observed["control"] = os.environ.get("TM_ERP_CONTROL")
            observed["nonce"] = os.environ.get("TM_ERP_NONCE")

    monkeypatch.setattr(server_entry.uvicorn, "Server", Server)
    monkeypatch.setattr(
        server_entry.threading, "Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: None),
    )
    server_entry.main()
    guard.assert_called_once_with()

    assert observed["control"] == str(control)
    assert observed["nonce"] is None
    assert not list(control.iterdir())
