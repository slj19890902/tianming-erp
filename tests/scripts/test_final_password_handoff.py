from pathlib import Path
import importlib.util
import sys


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "admin" / "final_password_handoff.py"
spec = importlib.util.spec_from_file_location("final_password_handoff", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_validate_handoff_password_rejects_weak_and_short() -> None:
    issues = module.validate_handoff_password("admin", "admin")
    assert issues
    assert any("弱密码" in issue or "至少 12 位" in issue for issue in issues)


def test_validate_handoff_password_requires_complexity() -> None:
    issues = module.validate_handoff_password("abcdefghijkl", "sales")
    assert any("大写字母" in issue for issue in issues)
    assert any("数字" in issue for issue in issues)
    assert any("符号" in issue for issue in issues)


def test_validate_handoff_password_accepts_strong_password() -> None:
    assert module.validate_handoff_password("ErpTrial!2026", "finance") == []
