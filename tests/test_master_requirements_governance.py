from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MASTER = DOCS / "TIANMING_ERP_MASTER_REQUIREMENTS.md"
CHARTER = DOCS / "CODEX_EXECUTION_CHARTER.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_master_requirements_is_unique_and_indexed() -> None:
    matching = sorted(DOCS.rglob("*TIANMING_ERP_MASTER_REQUIREMENTS*.md"))
    assert matching == [MASTER]

    agents = _read(ROOT / "AGENTS.md")
    start = _read(DOCS / "CODEX_START.md")
    assert "docs/TIANMING_ERP_MASTER_REQUIREMENTS.md" in agents
    assert "docs/CODEX_EXECUTION_CHARTER.md" in agents
    assert "只读取" in start and "总需求章节" in start
    assert "不得把 `docs/CODEX_HANDOFF.md`" in agents


def test_confirmed_factory_rules_are_frozen_in_master() -> None:
    master = _read(MASTER)
    required = (
        "`FIN-001～003` 是同一个合并的一楼暂存区",
        "片料优先复用工厂现有半成品/原料暂存区",
        "客户简称，按顺序用 `/` 分隔",
        "P1-11D 继续暂停",
        "一款固定占半张",
        "唯一标准栈板物理尺寸为 `1200×1000×150mm`",
        "五个异常对象全部纳入只读扫描",
    )
    for rule in required:
        assert rule in master


def test_module_routes_exist_without_duplicating_master() -> None:
    route_dir = DOCS / "context"
    routes = {
        "ORDER_FLOW.md": "订单主链上下文路由",
        "WAREHOUSE.md": "仓库、地图与模具上下文路由",
        "FINANCE.md": "成本、对账与收款上下文路由",
        "PLATFORM.md": "平台、权限与交付上下文路由",
    }
    for filename, title in routes.items():
        text = _read(route_dir / filename)
        assert title in text
        assert "优先读取总需求" in text
        assert len(text) < 4000


def test_legacy_rule_documents_are_explicitly_historical() -> None:
    assert "历史规则汇编" in _read(DOCS / "BUSINESS_RULES.md")
    assert "历史快照" in _read(DOCS / "ERP_PROJECT_STATE.md")

    charter = _read(CHARTER)
    assert "家庭候选完成验证后允许提交并推送" in charter
    assert "这不授权合并正式分支或工厂发布" in charter
    assert "禁止 IAB" in charter
