from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")


def test_editor_has_one_bom_title_and_no_price_explanation():
    assert '<summary>组合 BOM</summary>' in HTML
    assert '<h4>组合产品 / 内部 BOM</h4>' not in HTML
    assert '按当前客户口径原值保存，不自动乘或除 1.13。' not in HTML


def test_save_is_in_fixed_dialog_footer_and_preserves_validation():
    assert 'class="modal-foot product-editor-actions"' in HTML
    footer = HTML.split('class="modal-foot product-editor-actions"', 1)[1].split('</div>', 1)[0]
    for guard in ('masterSavePending', 'productCreaseMismatch', 'productMoldError',
                  'productPrintingPlateError', 'productPrintingColorError',
                  'productProductionLabelError', 'canEditProducts'):
        assert guard in footer
    assert HTML.count('class="btn primary product-inline-save"') == 1


def test_compact_css_loaded_and_grid_does_not_clip_search_popups():
    assert '/static/product-editor-compact.css?v=e210145a0c7d' in HTML
    css = (ROOT / 'static/product-editor-compact.css').read_text(encoding='utf-8')
    assert 'grid-column: 1 / -1' in css
    assert '.product-size-report-row' in css
    assert 'overflow: visible' in css
    assert '.product-editor-actions' in css


def test_bom_edit_actions_share_controls_and_quantity_is_integer():
    panel = HTML.split('<summary>组合 BOM</summary>', 1)[1].split('</fieldset>', 1)[0]
    assert 'class="bom-editor-add"' not in panel
    assert '添加子件' in panel
    assert '组合计价方式' not in panel
    assert 'min="1" step="1" v-model.number="component.quantity_per_set"' in panel
