from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _method_source(start: str, end: str) -> str:
    return INDEX.split(start, 1)[1].split(end, 1)[0]


def test_system_default_section_only_requests_visible_core_data() -> None:
    page_loader = _method_source("async loadSystem(", "async selectSystemSection")
    section_loader = _method_source(
        "async loadSystemSection", "async loadSystemVersionHistory"
    )
    basic_loader = section_loader.split('} else if (section === "print")', 1)[0]

    assert 'systemSection:"basic"' in INDEX
    assert 'this.systemSection = "basic"' in page_loader
    assert "await this.loadSystemSection(this.systemSection, { force })" in page_loader
    assert 'axios.get("/api/auth/users")' in basic_loader
    assert 'axios.get("/api/system/version")' in basic_loader
    assert 'axios.get("/api/system/company")' in basic_loader
    assert "/api/system/backups" not in page_loader
    assert "/api/system/version/changelog" not in page_loader
    assert "/api/system/delivery-print-settings" not in page_loader
    assert "loadMaterialMapping" not in page_loader
    assert "loadPdfTemplates" not in page_loader


def test_system_sections_load_on_demand_and_reuse_loaded_data() -> None:
    selector = _method_source("async selectSystemSection", "async loadSystemSection")
    section_loader = _method_source(
        "async loadSystemSection", "async loadSystemVersionHistory"
    )

    assert "await this.loadSystemSection(section)" in selector
    assert "this.systemSectionLoaded[section]" in section_loader
    assert "if (!force" in section_loader
    assert 'section === "print"' in section_loader
    assert 'axios.get("/api/system/delivery-print-settings")' in section_loader
    assert 'section === "backup"' in section_loader
    assert "await this.loadBackups()" in section_loader
    assert 'section === "pdf"' in section_loader
    assert "this.systemSectionErrors" in section_loader
    assert "this.systemSectionLoading" in section_loader


def test_version_history_and_pdf_templates_are_interaction_only() -> None:
    history_loader = _method_source(
        "async loadSystemVersionHistory", "async toggleSystemVersionHistory"
    )
    history_toggle = _method_source(
        "async toggleSystemVersionHistory", "async onPdfAdvancedToggle"
    )
    pdf_advanced = _method_source("async onPdfAdvancedToggle", "openCompanyModal")

    assert 'axios.get("/api/system/version/changelog")' in history_loader
    assert "this.versionHistoryLoaded" in history_loader
    assert "await this.loadSystemVersionHistory()" in history_toggle
    assert "details?.open" in pdf_advanced
    assert "await this.loadPdfTemplates()" in pdf_advanced
    assert "this.pdfTemplatesLoaded" in pdf_advanced


def test_system_template_exposes_four_clear_sections_and_retry_states() -> None:
    template = INDEX.split('<template v-else-if="activePage === \'system\'">', 1)[1].split(
        "</main>", 1
    )[0]

    for label in ("账号与公司", "打印设置", "数据备份", "PDF 识别管理"):
        assert label in template
    for section in ("basic", "print", "backup", "pdf"):
        assert f"selectSystemSection('{section}')" in template
    assert "systemSectionLoading[systemSection]" in template
    assert "systemSectionErrors[systemSection]" in template
    assert "retrySystemSection" in template
    assert "@toggle=\"onPdfAdvancedToggle\"" in template
    assert "toggleSystemVersionHistory" in template
    assert "systemSectionLoaded.basic ||" in template
    assert 'loadSystem({ force })' in INDEX


def test_hidden_material_mapping_is_not_requested_by_any_system_section() -> None:
    loader = _method_source("async loadSystem(", "openCompanyModal")

    assert "loadMaterialMapping" not in loader
    assert "/api/system/material-mapping" not in loader


def test_pdf_template_refresh_failure_preserves_previous_visible_rows() -> None:
    loader = _method_source("async loadPdfTemplates()", "openPdfTemplateEditor")

    assert "this.pdfTemplatesLoaded = true" in loader
    assert "if (!this.pdfTemplatesLoaded) this.pdfTrainingTemplates = []" in loader
