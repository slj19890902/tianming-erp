import subprocess
from pathlib import Path


def test_stocktake_review_recovery_component_contract():
    result=subprocess.run(['node',str(Path(__file__).with_name('stocktake_review_recovery.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_desktop_wires_review_recovery_without_weak_success_or_repeated_confirmation():
    source=(Path(__file__).parents[1]/'static'/'warehouse.html').read_text(encoding='utf-8')
    assert '/static/ui/stocktake-review-recovery.js?' in source
    assert 'id="stocktakeReviewRecovery"' in source
    assert 'async function approveStocktake(id){return startStocktakeReview(id,"approve")}' in source
    assert 'async function rejectStocktake(id){return startStocktakeReview(id,"reject")}' in source
    assert 'await showStocktakeReviewOutcome(await promise' in source
    assert 'if(Number(state.stocktakeReviewDetail?.id)===orderId)closeStocktakeReview()' in source
    assert '请再次确认：审核通过后' not in source
