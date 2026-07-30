from pathlib import Path
import re
import subprocess
import tempfile


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_default_flow_uploads_one_xlsx_and_has_no_apply_button() -> None:
    assert "下载样品 Excel" in INDEX
    assert "预览样品 Excel" in INDEX
    assert '<input type="file" accept=".xlsx"' in INDEX
    assert "旧模板兼容" in INDEX
    assert "Excel + 外部图纸" in INDEX
    assert "确认整批导入" not in INDEX
    assert "@click=\"applyProductWorkbook\"" not in INDEX
    assert "async applyProductWorkbook()" not in INDEX


def test_preview_only_boundary_is_visible_and_uses_no_apply_token() -> None:
    assert "整理预览检查通过" in INDEX
    assert "write_blocked_reason" in INDEX
    assert "存货编码映射未确认，当前禁止正式导入" in INDEX
    assert "不会写入产品、图纸、模具或历史业务数据" in INDEX
    assert '"/api/master/products/import-template.xlsx"' in INDEX
    assert '"/api/master/products/import/preview"' in INDEX
    assert '"/api/master/products/import/apply"' not in INDEX
    product_block = INDEX.split(
        "async previewProductWorkbook(event) {",
        1,
    )[1].split("async selectProductCustomer(row) {", 1)[0]
    assert "{preview_token:preview.preview_token}" not in product_block


def test_preview_keeps_customer_scope_and_external_files_only_for_compatibility() -> None:
    assert (
        'v-if="canAdmin && productTab === \'products\' && '
        'selectedProductCustomer"'
        in INDEX
    )
    assert 'form.append("customer_id",customer.id)' in INDEX
    assert 'form.append("file",workbooks[0])' in INDEX
    assert 'form.append("drawing_files",file)' in INDEX
    assert "this.clearProductWorkbook()" in INDEX


def test_inline_frontend_javascript_remains_syntactically_valid() -> None:
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        INDEX,
        flags=re.DOTALL | re.IGNORECASE,
    )
    scripts = [
        script
        for script in scripts
        if script.strip() and "src=" not in script[:120].lower()
    ]
    assert scripts
    script = max(scripts, key=len)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js") as source:
        source.write(script)
        source.flush()
        result = subprocess.run(
            ["node", "--check", source.name],
            text=True,
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr or result.stdout
