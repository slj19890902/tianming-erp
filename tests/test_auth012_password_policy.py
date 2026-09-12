import pytest
from app.core.password_policy import password_policy_issues


@pytest.mark.parametrize('password', ['abcd1234', 'ABCD1234', 'aBcD1234', 'abcd1234!', 'a' * 64 + '12345678'])
def test_employee_password_accepts_eight_characters_without_case_or_symbol_requirements(password):
    assert password_policy_issues(password, username='worker') == []


@pytest.mark.parametrize(('password', 'reason'), [
    ('abc1234', '至少 8 位'), ('123456789', '英文字母'), ('abcdefgh', '数字'),
    ('password123', '弱密码'), ('worker12', '用户名'), ('a' * 65 + '12345678', '72 字节'),
    ('中文中文1234', '英文字母'),
])
def test_employee_password_retains_required_and_existing_guards(password, reason):
    assert any(reason in issue for issue in password_policy_issues(password, username='worker'))
