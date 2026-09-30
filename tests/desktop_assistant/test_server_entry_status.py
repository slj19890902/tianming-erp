from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_managed_entry_preserves_read_only_control_path_for_web_status(monkeypatch, tmp_path):
    from desktop_assistant import server_entry

    control = tmp_path / "installation" / "control"
    control.mkdir(parents=True)
    monkeypatch.setenv("TM_ERP_CONTROL", str(control))
    monkeypatch.setenv("TM_ERP_NONCE", "synthetic-startup")
    monkeypatch.setenv("ERP_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("ERP_PORT", "18929")
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

    assert observed["control"] == str(control)
    assert observed["nonce"] is None
    assert not list(control.iterdir())
