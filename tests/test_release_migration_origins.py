import os
from pathlib import Path
from types import SimpleNamespace

from scripts.admin import release_erp


def test_migration_subprocess_does_not_inherit_https_proxy_origins(monkeypatch, tmp_path):
    production_origin = 'https://factory.example.com'
    monkeypatch.setenv('ERP_ALLOWED_ORIGINS', production_origin)
    monkeypatch.setenv('ERP_ENVIRONMENT', 'production')
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(release_erp.subprocess, 'run', run)
    target = tmp_path / 'isolated.sqlite3'
    release_erp._run_migration(target, 'rs08v8x9z67')
    env = calls[0]['env']
    assert env['ERP_ALLOWED_ORIGINS'] == 'http://127.0.0.1:18999'
    assert env['ERP_DATABASE_PATH'] == str(target.resolve())
    assert env['ERP_ENVIRONMENT'] == 'test'
    assert os.environ['ERP_ALLOWED_ORIGINS'] == production_origin
    assert os.environ['ERP_ENVIRONMENT'] == 'production'
