# MATERIAL_FLOW_FIX_20261014

负责人：限定采购执行者（Sol）；主管独立整合。分支 codex/material-flow-fix-20261014，已核实正式代码源 61e5fafb；origin 旧引用 b9a077bc 不覆盖正式能力。

范围：供应商对外交付规格归一化（预览/PDF/Excel共用）、订单直接及 typed/BOM 采购来源撤回、已报料撤销读取历史订单。目标 SRO208 的620/621/622数量267/800/850对外合计1917，内部来源和用途独立；订单9781/明细10527与采购605/606历史残留只读受控预览。

允许文件：requisition.py 对外采购投影片段；orders.py 采购撤回片段；新采购服务；主管追加授权 admin_order_reversal.py 必要采购解析/撤回片段；index.html 已报料撤销和scope片段；专用测试和本卡。禁止 incoming.py、地图、搜索、库存耗用、成本、送货、desktop_assistant、数据库、服务、发布、push及其他聊天。

不变量：保留供应商真实开料、瓦楞方向、压线和毛净料差异，冻结历史数量/材质/用途/成本；共享采购只撤自身来源。客户权限、状态/收料门禁、预览hash、幂等和审计事务保持。已取消来源的遗留采购必须先核实是否已对外承诺，历史可读不等于允许执行。绝不修改267张、000204已授权更正或正式数据。

证据只读：evidence/material-map-audit-20261014 方案/probe/relations/target-records。专用合成pytest先红后绿；真实演练仅使用已登记run_task受管子副本。兼容da12d243的_void_supplier_order(commit=False)内部入口，不触碰该片段。

完成条件：定向回归、diffcheck、小提交、精确片段报告；候选验证通过不冒称正式发布或人工验收。

## 最新事实与受控更正

主管转达本轮用户明确确认：「80012500本体75张、补强150张两条均未向供应商下单，撤销采购」。此授权仅适用于supplier605/606；正式执行仍由统一发布负责人完成。本候选新增服务级 `procurement_cancelled_correction.preview/execute`，没有扩大HTTP入口、自动修复或恢复已取消订单。

正式负责人步骤：使用已备份且版本核对后的正式Session，以实际有客户权限的管理员，调用preview(order_ids=[9781],supplier_item_ids=[605,606],supplier_not_sent_confirmed=True)。核对before必须是605/版本1/75张、606/版本1/150张，typed link15→内部204、link16→内部205；出现新版本、有效实收、完工、组套、库存占用/耗用、未取消内部来源或采购状态变化均停止。保留本次实时reviewed_hash；使用唯一operation_key及用户确认原文原因调用execute，成功后由调用者commit；失败rollback。禁止复用隔离副本hash到正式库。结果必须是采购605/606及link15/16 voided、采购表头按剩余active行重算、订单9781仍cancelled、内部204/205仍已取消、所有其他共享来源及数量不变；同键重放只返回原审计结果，异载荷拒绝。

## 已有证据与整合片段

- 两条先红测试：3行未归并、typed采购未撤回；初始均失败，修后通过。
- 只读真实probe：`evidence/material-flow-fix-20261014/managed-tasks/task-e41cc873cedc418eaaddc0f6216fba18/stdout.log`，SRO208最终1组1917张、sources620/621/622、issues=[]；三份冻结快照全部长2×宽1、理论375×236、交付750×236。schema及模数差异忽略，方向份数/理论切片/实际交付尺寸/毛净料/压线/供应商工序仍参与外规格契约。缺失或无效合同独立显示具体issues；不回填历史。
- 实际专项演练：`evidence/material-flow-fix-20261014/managed-tasks/task-ec5bf39020b641208d13abd26fb59cc0/stdout.log`，preview executable=true、blockers=[]；仅本任务受管子副本执行605/606成功，同键重放唯一审计41592，订单/内部来源取消状态保持，production_written=false。
- 两次成功run_task因「无法确认ERP/测试进程已退出」保留owned副本；初次venv软链接校验失败现场亦保留，后用现有Python真实绝对路径完成runner校验。原10:49源始终只读；不手工清理。
- Supplier PDF实际提取文字与Excel单元格验证1917/750×236/VSNIV且不含首源一开六/厂内模数；对外Vue模板编译通过，渲染只读长宽/压线/材质/数量/备注。各source_items保存独立snapshot；内部_purchase_line_key未修改。
- requisition.py仅_supplier_order_purchase_lines归一化和逐源snapshot；orders.py仅采购撤回、实收检查、baseline以及真正rollback_workflow保留取消报料事实，_delete_orders_in_transaction维持HEAD原文。
- admin_order_reversal.py共用direct/typed来源与receipt解析、遗留预览与逐行void；index.html仅voidReportedItem/openOrderEditor/ensureOrderGroupDetail的scope参数。对外单据首源cutting_mode不参与任何供应商渲染。
- 与da12d243的_void_supplier_order(commit=False)片段无重叠；不要覆盖其000204更正能力。无migration，head仍ep1010md。

开发验证候选尚未正式发布。统一NAS回执由主管整合此卡及证据写回，本执行者未改NAS资料、正式服务、正式数据库或其他聊天。

最终定向：采购专项/exports/admin399/开料/旧cancel/投影/多供应商8文件60项全过；phase5 workflow_rollback 15项全过（74 deselected），合计75个不同定向用例。另曾一次扩跑phase5全文件及上述集149项，147通过、2失败；共享源物理删除的旧预期已按保留void历史及原安全门禁改为409，并在15项回归通过。剩余主档售价字符串8.125000/8.1250差异在incoming-preflight工作区单节点同样失败，且该工作区orders.py对61e5fafb差异为空；是本任务未修改的既有失败，不顺带更改金额契约。`git diff --check`、compileall、Supplier Vue模板编译、唯一Alembic head ep1010md均通过。
