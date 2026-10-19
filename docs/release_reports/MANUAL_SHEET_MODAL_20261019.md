# 库存片料弹窗与定向匹配优化（2026-10-19）

状态：开发验证通过，尚未正式发布；正式 ERP 人工验收待发布后由管理员完成。

## 实现结果

- 待报料“使用库存片料”改为独立居中弹窗，通过 Teleport 渲染到页面顶层，不再撑开右侧操作单元格。主要列为尺寸、可用片数、真实库位、一开几和本次使用片数。常驻灰色教学说明收进详情，实际规格差异仍直接提示。
- 楞型在服务端查询与确认时严格一致。默认按当前订单/部件中从常用箱冻结的原长宽逐轴精确匹配，默认不旋转、不按面积、不匹配近尺寸或更大片料。没有可用的原方向匹配时，才显示“进一步匹配（交换长宽）”；点击后只查询同楞型的精确交换尺寸，保留客户、层数、面纸、加工及实物资格门禁。后端同样拒绝用交换模式绕过仍有默认匹配的情形。
- 勾选批次后按需求和原开料出数建议片数，人工可以修改；双拼耗片、模数、部件与数量单位独立计算，理论余量单列。多个批次只建议补足未覆盖缺口，整次一个确认按钮提交。
- 新批量抵扣接口先核对全部显示依据，再按原库存及报料流程在一个事务内预占、抵扣内部需求并审计。任一批次过期或审计失败，整次回滚。全部覆盖后免报料，部分覆盖保留缺口；采购承诺、备库用途和备注沿原流程保护。
- 长宽交换以本次分配的冻结开料方案保存，生产耗用按同一方案及原材质/楞型复核；未将理论产出写成实际成品。单批次旧回执的幂等摘要保持兼容，网络结果不明时保留原载荷及请求键。

## 来源和范围

分支：`codex/manual-sheet-modal-20261019`，独立工作区 `/Users/kimiyf/ERP-Migration/manual-sheet-modal-20261019`。以实际正式 v0.22.684 源码 0ff57156 及其发布记录 aa96430f 为开发基线；正式签名包 b0722c0c、数据库 revision et1019fl。远端 factory-current-baseline 只读查询仍为 b9a077bc，落后正式实现，因此未从落后代码覆盖正式版本。

功能提交为 `4832196af9103c1055cc6c820536b3c24d1ba7e6`。候选已保存到本地干净分支；GitHub 推送无进展后停止，随后定向远端分支核对也报告网络过慢，远端推送结果未确认，不宣称已推送。

生产实现文件：`app/api/warehouse.py`、`app/services/semi_finished_inventory.py`、`app/services/sheet_cut_plan.py`、`static/index.html`、`static/ui/manual-sheet-deduction.js`、新样式 `static/ui/manual-sheet-deduction.css`。无 schema 变更，无正式数据写入，无停服或重启。

## 验证与证据

核心最终测试 **52 项通过**，结果见 `/Users/kimiyf/ERP-Migration/evidence/manual-sheet-modal-20261019/core-final-results.xml`；前端 **27 项通过**，结果见同目录 `frontend-results.txt`。较广相邻测试 90 项通过、3 项已在原基线复现的旧失败被明确排除，结果见 `backend-results.xml`，该轮在最后两个多模数专用库存用例加入前运行。核心覆盖严格尺寸/楞型、人工交换、普通/BOM/双拼/多模数、单次及多批次数量、重复载荷、过期预览、第二次写入故障的原子回滚、冻结方案生产消耗及审计原子性；仅写 pytest 自建合成数据库。原前端回归里过期的产品脚本缓存断言已同步到当前正式资源版本。

Chrome 隔离预览使用相同组件、样式及主界面基础 CSS，使用示例数据，不连接正式业务接口。已核对默认列表、建议数量、人工修改缺口、多批次补足、确认后关闭及成功结果、无匹配时进一步匹配、交换方向提示及弹窗焦点。效果图：`evidence/manual-sheet-modal-20261019/exact-preview.png` 和 `swapped-preview.png`（证据位于工作区外的 ERP-Migration/evidence）。正式页面未做自动点击验收。

已验证唯一 Alembic head 为 et1019fl；Python 编译、Vue 模板编译、内联脚本解析及 git diff --check 通过。JS SHA-256 为 ad4b549c969b25a80fa386bbab78cd1cfdc5343d51534f709b4a237e30d7e081，CSS 为 7d167e88e29e2f04b25b2ea4c83d79fef022fb9cb80ad05e52bd641c778ae11b，HTML 缓存版本与实际资源相符。

相邻旧测试局限已经在未修改的 aa96430f 工作区复现：`test_semi_finished_lot_eligibility.py` 的旧入库 fixture 缺少现行成本凭据，PDF adopt harness 的 axios mock 缺 request interceptor，`test_cut_pair_yield_preallocation_consume_reverse_release` 旧断言未包含已存在的 review_contract。这些不代表本轮功能通过全量验收。新核心及相关回归结果单独保存，未改正式业务逻辑规避旧测试。

## 发布与人工核对

当前没有执行本任务正式发布。发布后管理员最短核对：

1. 待报料点击“使用库存片料”，核对独立弹窗及尺寸、数量、位置和一开几；以真实业务需求填写片数并确认。
2. 核对异楞不出现，默认原长宽；没有匹配时再点“进一步匹配”，核对仅显示同楞型交换长宽的合格库存。
3. 核对抵扣后完整覆盖免报料、部分覆盖仅留缺口；取消或关闭不改变库存。

NAS 当前未挂载，AI_START 不可读取，独立回执先保存在本地该文件，NAS 知识库归档待挂载恢复。本环境未提供真实 token 用量，无法获取，不估算。
