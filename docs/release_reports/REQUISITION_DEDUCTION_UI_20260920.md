# 合并报料 UI 与逐批片料抵扣评估、验证

日期：2026-09-20。范围：生成供应商采购报料单弹窗；不变更采购模型、成品推荐和数据库结构。开发分支 `codex/requisition-deduction-ui-20260920`，实现提交 `9f8f7356`，集成基线 `be6cde96` / v0.22.461 / db0919。发布版本 v0.22.462，最终发布证据另记回执。

## 评估结论及改进

现场截图所示布局和候选处理不完全符合要求。

1. 高影响布局问题：库存详情塞在狭窄“来源概览”列，长批次号和说明撑高整行，采购数量及保存操作远离视野。改为紧凑采购摘要，点击“核对片料库存”展开整行库存表；规格、加工/压线、可用张数、抵扣片数、位置和采用操作独立列，批次和匹配说明按需展开。
2. 高影响范围问题：默认浏览包含更广范围库存，并把候选称为可抵扣或同客户通用备料。默认 SQL 仅查询当前客户绑定片料；“查询其他可用片料”才查询其他权限允许范围，每页 10 条。显示待核对差异，不把候选视为已经可用；查看不预占。
3. 高影响决策问题：候选可能一起提交，未逐项处理会阻止保存。现在每个“采用”按钮只提交该批次和明确片数，不自动采纳其他候选；可不抵扣直接保存。后端客户权限、适用性、版本、数量、反压线管理员确认等校验保留。
4. 高影响事务问题：SQLite 保存点在外层事务未真正开始时，审计故障可留下已提交预占。故障注入复现后，增加真实外层写事务，并以审计回执核对幂等同键同载荷；部分数量不足整笔拒绝，失败全部回滚。
5. 编辑保护：抵扣刷新采购缺口时保留供应商、备注、额外备库量和人工采购数量上限；查询失败/超时/取消解除加载，旧草稿请求不覆盖新草稿。采购保存和库存采用互斥，保留原有提交不确定状态保护。

本轮沿用现有弹窗与设计，不是全面视觉重做。加工和规格尚未确认的资料仍要求人工核对，没有修改正式主档来消除提示；未做完整无障碍合规审计。

## 真实验证

隔离页面：`http://127.0.0.1:18093/?page=requisition`。
隔离库：`D:\tm-uat\requisition-ui-20260920\run\uat.sqlite3`，独立写根、端口和 cookie。由正式库只读备份取得，确认隔离后仅重置副本管理员测试密码。无迁移。正式库始终为 `D:\TianmingERP\shared\data\carton_erp.sqlite3`。

Chrome 实际渲染并触发页面 DOM 事件完成：两组采购行；只采用 `SI-20260914-1B226E0098` 的 100 片；订单采购从 1000 张重算为 900 张，保留额外备库 5 张及备注，总采购 905 张；另一行 270 张不抵扣亦保存成功。仅在副本生成 `SRO-20260920-0001` 和 `SRO-20260920-0002`。验证了其他范围分页。没有点击正式页面、打印或写入正式业务数据。

截图：`D:\tm-uat\requisition-ui-20260920\screenshots\before-user.png`、`after-summary.png`、`after-inventory.png`、`after-saved.png`。业务核对证据：同目录上级的 `browser-verification.json`。Chrome 自动物理鼠标注入未生效，使用实际 DOM click 触发真实页面处理器；不将其写作工厂鼠标或人工验收通过。

在独立工作树执行（Python 为正式源码目录现有 venv，仅复用解释器）：

```powershell
D:\纸箱厂erp软件搭建\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_requisition_deduction_ui.py tests/test_package_a_inventory.py tests/test_p1_09c_105_requisition_inventory_action_guard.py tests/test_semi_finished_order_reservation.py tests/test_semi_finished_inventory_frontend.py tests/test_stock_replenishment_frontend.py tests/test_p1_46a_merged_requisition_cutting.py tests/test_p1_80_purchase_purpose_frontend.py tests/test_p1_80_purchase_purpose_allocation.py tests/test_a0004_order_execution_status_view.py tests/test_p0_37_vue_template_browser_safety.py -q --tb=short -k 'not liner_direct_coverage_retains_source_without_completion and not pending_requisition_explains_semi_deduction_and_purchase_shortage and not pdf_direct_save_carries_the_same_reservation_plan and not replenishment_save_and_print_paths_are_wired'
```

结果：**178 passed, 6 deselected, 204 warnings，267.21 秒**。警告为测试 JWT 短密钥。包括后端范围/权限、数量不足、过期版本、同键重放/异载荷拒绝、并发同键、审计故障回滚；Node 执行实际前端方法验证逐批选择、冻结载荷、失败/超时/取消、旧请求、编辑保留和无抵扣保存；并回归 R01/R02、合并开料、采购用途、刚发布的订单状态。

未隐瞒的既有失败：上述排除的 4 个测试名称（其中一个参数化 3 例）已在干净的原基线 `158a606b` 单独复现，结果 4 failed, 2 passed。分别为衬板覆盖历史行为和 3 个旧前端文字/源码断言，本轮未顺带修复。该结论不是全量测试全部通过。完整日志 `D:\tm-uat\requisition-ui-20260920\targeted-tests.log`。Python 编译、Node 实际方法执行、差异检查通过，Alembic 唯一 head 为 db0919。

修改：`app/api/requisition.py`、`app/services/semi_finished_inventory.py`、`static/index.html`、`app/version.py`；新增 `tests/test_requisition_deduction_ui.py`、`tests/requisition_deduction_ui.cjs`；更新 4 个既有前端/预占测试和本任务文档。

## 管理员正式人工验收（发布后）

入口：`http://192.168.3.80:8000/?page=requisition`。仅技术发布和只读核对不能替代管理员验收。

1. 选择待报料行打开生成供应商采购报料单，检查采购摘要可读；展开库存，默认只显示当前客户绑定候选。
2. 点“查询其他可用片料”检查分页；按真实业务需要只采用一个批次，核对张/片、采购缺口及备注/备库量保留；其他批次不得自动采用。
3. 按真实业务保存一笔有抵扣或无抵扣报料，核对采购总张数与用途；不要为验收伪造正式订单。

真实工厂屏幕、管理员正式操作、实体打印尚待外部验收。实际 token 用量不可获取。
