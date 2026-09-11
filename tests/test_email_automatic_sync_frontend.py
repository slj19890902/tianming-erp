from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_email_page_exposes_safe_automatic_controls():
    source = (ROOT / 'static' / 'email-intake.html').read_text(encoding='utf-8')
    assert 'id="automatic" type="checkbox"' in source
    assert 'id="interval"' in source
    assert "api('/automation'" in source
    assert '自动读取收件箱' in source
    assert '不会改变邮箱里的已读状态' in source


def test_formal_lifespan_runs_email_scheduler_only_with_formal_jobs():
    source = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
    assert 'from app.services.email_intake import automatic_sync_loop' in source
    assert 'if current.is_production and not os.getenv("ERP_UAT_ROOT")' in source
    assert 'asyncio.create_task(automatic_sync_loop(stop, SessionLocal))' in source
