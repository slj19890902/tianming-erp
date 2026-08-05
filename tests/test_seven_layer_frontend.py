"""Regression guards for the seven-layer material UI contract."""

from pathlib import Path
import re
import subprocess
import tempfile


HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
    encoding="utf-8"
)
WAREHOUSE_HTML = (
    Path(__file__).resolve().parent.parent / "static" / "warehouse.html"
).read_text(encoding="utf-8")


def _inline_scripts():
    return re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", HTML, re.S | re.I)


def _source_between(start, end):
    return HTML.split(start, 1)[1].split(end, 1)[0]


def _axios_payload(source, endpoint):
    match = re.search(
        rf'axios\.post\("{re.escape(endpoint)}",\s*\{{(?P<payload>.*?)\n\s*\}}\s*(?:,\s*\{{.*?\}})?\s*\);',
        source,
        re.S,
    )
    assert match, f"missing axios.post payload for {endpoint}"
    return match.group("payload")


class TestSevenLayerControls:
    def test_warehouse_manual_in_supports_seven_layers_without_default_flute(self):
        assert '<option value="7">七层</option>' in WAREHOUSE_HTML
        assert 'layer==="5"?["AB","BE"]:["AAA","ABC"]' in WAREHOUSE_HTML
        assert "layer===\"7\"?'<option value=\"\">请选择楞型</option>'" in WAREHOUSE_HTML
        assert '<select id="siFlute" required>' in WAREHOUSE_HTML

    def test_composer_accepts_seven_digit_code(self):
        assert '<option :value="7">七层</option>' in HTML
        assert ':maxlength="materialComposer.layer_count"' in HTML
        assert "if (![3,5,7].includes(layerCount) || code.length !== layerCount)" in HTML
        assert "onMaterialComposerLayerChange" in HTML

    def test_seven_layer_flutes_are_manual_aaa_or_abc(self):
        assert 'if (layerCount === 7) return ["AAA", "ABC"];' in HTML
        assert 'if (lc === 7) return ["AAA", "ABC"];' in HTML
        assert "const seven = [\"AAA\",\"ABC\"]" in HTML
        composer = _source_between(
            '<div v-if="showMaterialComposer"',
            '<div v-if="!displayedMaterials.length"',
        )
        assert 'v-if="materialComposer.layer_count === 7"' in composer
        assert 'v-model="materialComposer.usage_flute_type"' in composer
        assert 'orderItemFluteOptions(materialComposer.layer_count)' in composer
        assert '<option value="">请选择楞型</option>' in composer

    def test_layer_specific_options_and_safe_defaults(self):
        assert 'if (layerCount === 3) return ["A", "B", "E"];' in HTML
        assert 'if (layerCount === 5) return ["AB", "BE"];' in HTML
        assert 'if (layerCount === 7) return ["AAA", "ABC"];' in HTML
        assert 'if (lc === 3) return ["A", "B", "E"];' in HTML
        defaults = _source_between(
            "getDefaultFluteByLayer(layerCount) {",
            "fluteOptionsByLayer(layerCount) {",
        )
        assert 'if (layerCount === 3) return "B";' in defaults
        assert 'if (layerCount === 5) return "AB";' in defaults
        assert "return null;" in defaults
        assert defaults.count('return "B";') == 1
        quotation = _source_between(
            "const blankQuotationLine = () => ({",
            "const app = createApp({",
        )
        assert "layer_count: null" in quotation
        assert 'flute_type: ""' in quotation
        assert "if (this.materialLayerFilter === 7) return [];" in HTML

    def test_layer_changes_clear_invalid_composer_flute(self):
        handler = _source_between(
            "onMaterialComposerLayerChange() {",
            "onMaterialComposerCodeInput() {",
        )
        assert "const allowedFlutes = this.orderItemFluteOptions(layerCount);" in handler
        assert "allowedFlutes.includes(currentFlute) ? currentFlute : \"\"" in handler

    def test_composer_real_requests_submit_usage_flute_type(self):
        preview_method = _source_between(
            "async previewMaterialComposition() {",
            "async saveMaterialComposition() {",
        )
        save_method = _source_between(
            "async saveMaterialComposition() {",
            "recommendMaterialCode() {",
        )
        preview_payload = _axios_payload(
            preview_method, "/api/master/materials/compose/preview"
        )
        assert "usage_flute_type:usageFluteType || null" in preview_payload
        assert (
            'usage_flute_type:String(form.usage_flute_type || "")'
            ".toUpperCase() || null"
        ) in save_method
        assert 'axios.post("/api/master/materials/compose/save", payload)' in save_method
        assert 'layerCount === 7 && !["AAA","ABC"].includes(usageFluteType)' in preview_method

    def test_common_box_order_and_requisition_have_seven_layer_options(self):
        assert HTML.count('<option :value="7">') >= 4
        assert "onOrderItemLayerChange(index)" in HTML
        assert "onOrderItemLayerChangeForForm(orderItemForm)" in HTML
        assert "onRequisitionLayerChange" in HTML

    def test_quotation_uses_layer_specific_flute_options(self):
        assert "quotationFluteOptions(line)" in HTML
        assert "onQuotationFluteChange(line)" in HTML
        assert "line.layer_count = Number(material?.layer_count" in HTML
        assert "const allowedFlutes = this.orderItemFluteOptions(layerCount);" in HTML

    def test_flute_rule_maintenance_supports_seven_layers(self):
        assert "v-model.number=\"fluteRuleForm.layer_count\" @change=\"onFluteRuleLayerChange\"" in HTML
        assert "onFluteRuleLayerChange()" in HTML
        assert "orderItemFluteOptions(fluteRuleForm.layer_count)" in HTML
        assert "七层支持 AAA/ABC" in HTML


class TestMaterialDictionaryKeepsFluteOut:
    def test_material_editor_does_not_render_flute_field(self):
        block = HTML.split("modal.type === 'material'", 1)[1].split(
            "modal.type === 'orderPdfImport'", 1
        )[0]
        assert "flute_type" not in block

    def test_material_save_drops_flute_field(self):
        payload_builder = _source_between(
            "buildMaterialWritePayload() {",
            "async prepareMaterialChangeConfirmation(changes) {",
        )
        block = _source_between(
            'if (this.modal.type === "material") {',
            'if (this.modal.type === "orderPdfImport") {',
        )
        assert "const payload = {...this.materialForm};" in payload_builder
        assert "delete payload.id;" in payload_builder
        assert "delete payload.flute_type;" in payload_builder
        assert "const payload = this.buildMaterialWritePayload();" in block

    def test_business_payloads_keep_selected_flute(self):
        assert "payload.flute_type = this.orderItemForm.flute_type || null;" in HTML
        assert "flute_type:form.flute_type || null," in HTML


def test_inline_frontend_javascript_is_syntactically_valid():
    scripts = _inline_scripts()
    assert scripts, "static/index.html should contain an inline Vue script"
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
