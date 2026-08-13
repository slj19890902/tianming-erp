from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = (
    PROJECT_ROOT / "scripts" / "admin" / "update_erp.ps1"
).read_text(encoding="utf-8")
RELEASE_SCRIPT = (
    PROJECT_ROOT / "scripts" / "admin" / "release_erp.ps1"
).read_text(encoding="utf-8")
RELEASE_HELPER = (
    PROJECT_ROOT / "scripts" / "admin" / "release_erp.py"
).read_text(encoding="utf-8")


def test_legacy_update_entry_is_fail_closed() -> None:
    assert "旧的一键更新入口已停用" in UPDATE_SCRIPT
    assert "release_erp.ps1" in UPDATE_SCRIPT
    assert "alembic" not in UPDATE_SCRIPT.lower()
    assert "git fetch" not in UPDATE_SCRIPT
    assert "git merge" not in UPDATE_SCRIPT


def test_factory_update_reports_current_release_version() -> None:
    from app.version import (
        APP_BUILD_DATE,
        APP_CHANGES,
        APP_CHANGELOG,
        APP_EXTERNAL_ACCEPTANCE_REQUIRED,
        APP_VERIFICATION_STEPS,
        APP_VERSION,
        APP_VERSION_NAME,
        current_release_metadata,
    )

    assert APP_VERSION == "v0.22.105"
    assert APP_VERSION_NAME == "模具标签打印状态提醒"
    assert APP_BUILD_DATE == "2026-08-13"
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    metadata = current_release_metadata(expected_version=APP_VERSION)
    assert metadata["external_acceptance_required"] is True
    assert metadata["changes"] == APP_CHANGES
    assert metadata["verification_steps"] == APP_VERIFICATION_STEPS
    assert 1 <= len(metadata["changes"]) <= 5
    assert 1 <= len(metadata["verification_steps"]) <= 5
    current_release = [
        item for item in APP_CHANGELOG if item.startswith(f"{APP_VERSION}：")
    ]
    assert any("本次更新｜" in item and "已打印/未打印" in item for item in current_release)
    assert any("本次更新｜" in item and "最后打印时间" in item and "累计次数" in item for item in current_release)
    assert any("本次更新｜" in item and "幂等凭证" in item and "事务保护" in item for item in current_release)
    assert any("本次更新｜" in item and "ll20→mm21" in item and "不批量回填" in item for item in current_release)
    assert any("如何验证｜" in item and "历史模具" in item and "未打印" in item for item in current_release)
    assert any("如何验证｜" in item and "打印标签" in item and "已打印" in item for item in current_release)
    assert any("如何验证｜" in item and "批量打印标签" in item and "累计次数" in item for item in current_release)
    assert any("如何验证｜" in item and "v0.22.105" in item and "mm21v8x9z10" in item for item in current_release)
    assert any(item.startswith("v0.22.104：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.104：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.103：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.103：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.102：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.102：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.101：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.101：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.100：本次更新｜") for item in APP_CHANGELOG)
    assert any(
        item.startswith("v0.22.100：如何验证｜")
        and "v0.22.100" in item
        and "ll20" in item
        for item in APP_CHANGELOG
    )
    assert any(item.startswith("v0.22.99：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.96：本次更新｜") for item in APP_CHANGELOG)
    assert any(
        item.startswith("v0.22.98：本次更新｜")
        and "外购包装采购单" in item
        and "客户订单" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.98：如何验证｜")
        and "v0.22.98" in item
        and "kk19" in item
        for item in APP_CHANGELOG
    )
    assert any(item.startswith("v0.22.94：本次更新｜") for item in APP_CHANGELOG)
    assert any(
        item.startswith("v0.22.86：本次更新｜")
        and "常用箱" in item
        and "即时搜索" in item
        and "旧缓存" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.88：本次更新｜")
        and "一张天明实测仓库地图" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.84：本次更新｜")
        and "A1 型纸箱" in item
        and "模切内盒" in item
        and "衬板" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.83：本次更新｜")
        and "模具名称" in item
        and "拼音首字母" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.82：本次更新｜")
        and "顶部显示模式行" in item
        and "仓库" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.82：如何验证｜")
        and "v0.22.82" in item
        and "ea09" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.81：本次更新｜")
        and "全部真实待送订单候选" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.80：本次更新｜")
        and "组合 BOM" in item
        and "冻结快照" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.80：如何验证｜")
        and "v0.22.80" in item
        and "ea09" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.75：本次更新｜") and "预计总成本快照" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.75：如何验证｜") and "v0.22.75" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.74：本次更新｜") and "双拼报料" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.74：如何验证｜") and "v0.22.74" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.73：本次更新｜") and "外购包装" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.73：如何验证｜") and "v0.22.73" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "如何验证｜" in item and "v0.22.105" in item and "mm21" in item
        for item in current_release
    )
    assert any(
        item.startswith("v0.22.79：本次更新｜") and "完整工作区高度" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.79：如何验证｜") and "v0.22.79" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.78：本次更新｜") and "库存台账" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.78：如何验证｜") and "sr2" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.77：本次更新｜") and "低配电脑" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.77：如何验证｜") and "v0.22.77" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.61：")
        and "默认进入二维平面" in item
        and "轻量批量绘制" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.58：") and "待报料" in item and "页签条件隐藏" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.58：如何验证｜")
        and "15 条" in item
        and "昆山鸣朋 10 条" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.51：") and "YKE 100 条" in item and "KEW 31 条" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5ba2\u6237" in item
        and "\u5e38\u7528\u7bb1" in item
        and "\u6750\u8d28" in item
        and "\u7248\u672c\u5386\u53f2" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u4fee\u6539\u539f\u56e0" in item
        and "\u64cd\u4f5c\u8005" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5e76\u53d1\u51b2\u7a81" in item
        and "409" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5f02\u5e38\u53d8\u66f4" in item
        and "\u4e8c\u6b21\u786e\u8ba4" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u7ba1\u7406\u5458\u6062\u590d" in item
        and "\u65b0\u7248\u672c" in item
        and "\u6062\u590d\u539f\u56e0" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u654f\u611f\u4ef7\u683c" in item
        and "\u6743\u9650" in item
        and "\u8131\u654f" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u6587\u672c\u5c42" in item
        and "OCR" in item
        and "\u6570\u91cf" in item
        and "\u91d1\u989d" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u53ea\u9700\u9009\u62e9\u5ba2\u6237" in item
        and "\u4e09\u697c\u5177\u4f53\u8d27\u4f4d" in item
        for item in APP_CHANGELOG
    )
    assert any("396" in item and "\u9884\u8bbe\u8d27\u4f4d" in item for item in APP_CHANGELOG)
    assert any("F2" in item and "F3" in item and "F4" in item for item in APP_CHANGELOG)
    assert any(
        "\u6309\u6574\u5f20\u9001\u8d27\u5355\u9009\u62e9" in item
        and "\u5168\u90e8\u660e\u7ec6" in item
        for item in APP_CHANGELOG
    )
    assert any("\u6708\u7ed3\u7ed3\u8f6c\u65e5" in item and "20" in item for item in APP_CHANGELOG)
    assert any("\u5f85\u5bf9\u8d26" in item and "\u6309\u5ba2\u6237+\u6708\u4efd\u6c47\u603b" in item for item in APP_CHANGELOG)
    assert any("\u4e00\u952e\u542f\u52a8" in item for item in APP_CHANGELOG)
    assert any("\u684c\u9762\u56fe\u6807\u5c31\u80fd\u81ea\u52a8\u5347\u7ea7" in item for item in APP_CHANGELOG)


def test_release_script_is_two_phase_and_requires_exact_approval() -> None:
    for marker in (
        "-Prepare",
        "-Apply",
        "ExpectedCodeSha",
        "ExpectedRevision",
        "ExpectedAppVersion",
        "ApprovalToken",
        "factory-current-baseline",
        "Stop-ErpService",
        "Assert-ErpStopped",
        "ERP_ENVIRONMENT=production",
        "release_erp.py",
        "mark-started",
        "check-release-metadata",
    ):
        assert marker in RELEASE_SCRIPT
    prepare_block = RELEASE_SCRIPT.index("if ($Prepare)")
    assert RELEASE_SCRIPT.index(
        '"check-release-metadata",',
        prepare_block,
    ) < RELEASE_SCRIPT.index("Stop-ErpService", prepare_block)
    assert RELEASE_SCRIPT.index("Stop-ErpService") < RELEASE_SCRIPT.index(
        '"prepare",'
    )
    assert "git fetch" not in RELEASE_SCRIPT
    assert "git merge" not in RELEASE_SCRIPT


def test_release_helper_contains_backup_rehearsal_and_fail_closed_checks() -> None:
    for marker in (
        "mode=ro",
        "source_connection.backup",
        "PRAGMA integrity_check",
        "PRAGMA foreign_key_check",
        "core_counts",
        "awaiting_human_approval",
        "apply_failed_service_must_remain_stopped",
        "approval_token",
        "release_metadata",
        "expected_app_version",
        "human_acceptance_status",
        "check-release-metadata",
        '"alembic", "upgrade", revision',
    ):
        assert marker in RELEASE_HELPER
    assert '"head"' not in RELEASE_HELPER


def test_release_helper_creates_valid_sqlite_copy(tmp_path: Path) -> None:
    from scripts.admin.release_erp import create_sqlite_copy

    source = tmp_path / "carton_erp.sqlite3"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('test-revision')")
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('工厂更新测试')")
        connection.commit()

    backup = backup_dir / "copy.sqlite3"
    result = create_sqlite_copy(source, backup)

    assert backup.is_file()
    assert result["integrity_check"] == "ok"
    assert result["foreign_key_violations"] == 0
    assert result["revision"] == "test-revision"
    assert len(result["sha256"]) == 64
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "工厂更新测试"
