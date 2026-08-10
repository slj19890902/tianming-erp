from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str, next_name: str) -> str:
    match = re.search(
        rf"async {re.escape(name)}\(\) \{{(.*?)\n\s{{10}}\}},\n\s{{10}}async {re.escape(next_name)}\(",
        INDEX,
        re.S,
    )
    assert match is not None
    return match.group(1)


def test_cost_check_copy_explains_what_staff_and_boss_should_do() -> None:
    for text in (
        "成本与利润提醒",
        "员工先看“待补资料”",
        "老板再看“利润复核”",
        "这里只列需要处理的订单，不是全部订单成本清单",
        "目前没有需要补资料的新订单",
        "这不是成本功能失效",
        "旧订单不会自动补算",
        "这个分类没有待处理订单",
        "已检查 {{ costReviewState.evaluated_items }} 条，全部预计正常，无需处理",
        "目前还没有可检查的新订单",
        "打开“查看详情”",
    ):
        assert text in INDEX
    assert "当前没有这类成本缺口" not in INDEX
    assert "当前没有需要复核的预计利润" not in INDEX


def test_open_cost_check_starts_both_reads_and_chooses_the_useful_tab() -> None:
    node = shutil.which("node")
    if node is None:
        return
    body = _method_body("openCostGaps", "loadCostGaps")
    script = f"""
const assert = require('node:assert/strict');
const openCostGaps = async function() {{{body}
}};
async function run(gapsReady, gapTotal, reviewReady) {{
  const started = [];
  const ctx = {{
    canViewCosts: true,
    costPanelTab: '',
    costGapState: {{filter:'all', total_items:99}},
    costReviewState: {{}},
    modal: null,
    showToast() {{ throw new Error('unexpected toast'); }},
    async loadCostGaps() {{
      started.push('gaps');
      await Promise.resolve();
      this.costGapState.total_items = gapTotal;
      return gapsReady;
    }},
    async loadCostReview() {{
      started.push('review');
      await Promise.resolve();
      return reviewReady;
    }},
  }};
  const result = await openCostGaps.call(ctx);
  assert.deepEqual(started, ['gaps', 'review']);
  return {{result, tab:ctx.costPanelTab, title:ctx.modal.title}};
}}
(async () => {{
  assert.deepEqual(await run(true, 2, true), {{result:true, tab:'gaps', title:'成本与利润提醒'}});
  assert.deepEqual(await run(true, 0, true), {{result:true, tab:'review', title:'成本与利润提醒'}});
  assert.deepEqual(await run(false, 0, true), {{result:true, tab:'review', title:'成本与利润提醒'}});
  assert.deepEqual(await run(false, 0, false), {{result:false, tab:'gaps', title:'成本与利润提醒'}});
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [node, "-e", script],
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
