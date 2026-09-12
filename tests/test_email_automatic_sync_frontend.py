from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_email_settings_live_in_system_settings_not_intake():
    source = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    history = (ROOT / 'static' / 'email-intake.html').read_text(encoding='utf-8')
    assert "selectSystemSection('email')" in source
    assert 'v-model="emailSettings.automatic_enabled"' in source
    assert 'v-model.number="emailSettings.sync_interval_minutes"' in source
    assert 'axios.put("/api/email-intake/automation"' in source
    assert 'v-model="emailSenderText"' in source
    assert 'id="code"' not in history and 'id="senders"' not in history
    assert '不会改变邮箱里的已读状态' in history


def test_formal_lifespan_runs_email_scheduler_only_with_formal_jobs():
    source = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
    assert 'from app.services.email_intake import automatic_sync_loop' in source
    assert 'if current.is_production and not os.getenv("ERP_UAT_ROOT")' in source
    assert 'asyncio.create_task(automatic_sync_loop(stop, SessionLocal))' in source
