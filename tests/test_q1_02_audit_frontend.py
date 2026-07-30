from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")

AUDIT_PAGE = INDEX.split(
    '<template v-else-if="activePage === \'audit\'">', 1
)[1].split(
    '<template v-else-if="activePage === \'permissions\'">', 1
)[0]


def test_audit_menu_is_permission_gated() -> None:
    assert '{ key: "audit", label: "操作记录" }' in INDEX
    assert 'audit: "audit.view"' in INDEX
    assert 'audit:"audit.view"' in INDEX
    assert '["audit", "操作记录查看", "audit.view"]' in INDEX
    assert '"users.manage","audit.view","warehouse.stocktake.review"' in INDEX
    assert 'if (page === "audit") await this.loadAuditLogs()' in INDEX


def test_audit_page_has_two_simple_tabs_and_all_filters() -> None:
    assert "业务操作" in AUDIT_PAGE
    assert "安全事件" in AUDIT_PAGE
    for field in (
        "date_from",
        "date_to",
        "operator",
        "module",
        "action",
        "object_ref",
        "customer",
        "result",
        "source",
    ):
        assert f"auditLogState.filters.{field}" in AUDIT_PAGE
    assert "@click=\"queryAuditLogs\"" in AUDIT_PAGE
    assert "@click=\"resetAuditFilters\"" in AUDIT_PAGE


def test_audit_list_uses_canonical_server_pagination_contract() -> None:
    assert 'axios.get("/api/audit/logs"' in INDEX
    assert "event_category:this.auditLogState.category" in INDEX
    assert "page:this.auditLogState.page" in INDEX
    assert "page_size:this.auditLogState.page_size" in INDEX
    assert ':page="auditLogState.page"' in AUDIT_PAGE
    assert ':total="auditLogState.total"' in AUDIT_PAGE
    assert '@change="changeAuditPage"' in AUDIT_PAGE
    assert "/api/audit-logs" not in INDEX


def test_audit_detail_is_read_only_and_safe_text() -> None:
    assert "modal.type === 'auditDetail'" in INDEX
    assert "auditDetailsText(auditLogState.detail.details)" in INDEX
    assert "JSON.stringify(value, null, 2)" in INDEX
    assert "v-html=\"auditLogState" not in INDEX
    assert 'axios.get(`/api/audit/logs/${encodeURIComponent(logId)}`)' in INDEX
    assert ">导出<" not in AUDIT_PAGE
    assert ">打印<" not in AUDIT_PAGE
    assert ">编辑<" not in AUDIT_PAGE
    assert ">删除<" not in AUDIT_PAGE
