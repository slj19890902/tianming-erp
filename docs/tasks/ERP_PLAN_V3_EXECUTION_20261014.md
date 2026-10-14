# ERP 第三版方案实施 — 2026-10-14

用户最新指令：按已交付第三版方案选合适模型执行。此前维护造成停工，必须保持正式ERP可用；不得重跑旧combined维护批次。范围只含已报告问题及必要发布保障。

权威方案：`/Users/kimiyf/ERP-Migration/evidence/mac-cleanup-20261014/库存全量治理与应急业务完整方案-20261014.txt`。
证据：同目录`full-stock-readonly.json`、`full-stock-summary.json`、`送货确认与C货架诊断-20261014.txt`、`dispatch-rack-plan-probe.py/.json`。正式数据时点09:35，后续更正须最新只读核账；不把旧pending状态当实时。

基线：09:35已核实正式v625源61e5fafba43ec9fe27e0381d0dbab59d76eb96a5，DB ep1010md。2026-10-14本次fetch失败HTTP2 framing layer，本地origin/factory-current-baseline仍b9a077bc且显著落后Mac部署源。不能以旧远端源码覆盖Mac能力，因此新隔离分支从已核实正式源建立；发布前重新核对实际正式状态，不把远端刷新失败说成已同步。旧inventory-closed-loop候选及文档未提交改动保留，旧修复按依赖/patch逐项提取，不整体带入v629失败候选。

本聊天继续执行，不另建用户聊天。模型：GPT-6 Astra负责送货协议高风险修复及后续独立审查，GPT-6.1 Sol负责明确边界的业务修复；最多两个执行子任务。用户本次选择模型执行及NAS模型协作规则为分派依据。实际调用型号与完成结果分别记录，不虚报。

## 第一阶段文件与工作区负责人

- 根：`/Users/kimiyf/ERP-Migration/erp-plan-v3-20261014`，branch codex/erp-plan-v3-20261014。负责任务卡、整合、备份/更新/启动必要修复和正式只读核对。阶段一不改两个代理所有文件。
- Astra dispatch：`/Users/kimiyf/ERP-Migration/dispatch-v3-20261014`。唯一写入范围：app/services/delivery_dispatch_commands.py、static/ui/delivery-dispatch-recovery.js、static/index.html中送货确认/恢复相关方法、专用dispatch测试/夹具，以及专用子任务报告。其他文件先报根协调。
- Sol stock：`/Users/kimiyf/ERP-Migration/stock-v3-20261014`。唯一写入范围：app/services/inventory_cost_snapshot.py、sheet_cutting_settings.py及必要同类成本/组件服务、app/api/warehouse.py中手工片料与相关状态门禁、专用测试和子任务报告。先复现000120独立底残留base字段误判，后提取10be6028手工片料后端并纠正内部已报料未合并边界。不得写static/index.html，UI需求先报根后串行整合。
- C架/地图及历史库存治理在上述文件负责人释放后单独闭环，禁止并发碰app/api/warehouse.py和大前端文件。

## 不变量与验证

正式ERP保持运行；子代理不得正式数据库读写、启动新业务服务、备份、发布、停止/重启、申请管理员认证、操作正式页面或推送Git。不需要真实凭据；沿用现有Python/Node运行时。合成pytest临时库可用，正式副本需现有run_task隔离工具。成功副本按既有回收规则清理，保留小型结果。

账号/客户权限、物理数量与单位、订单冻结依据、幂等、事务、版本、审计、原回执和旧二维码追溯保持。旧请求兼容不能清记录或换key重发；C显示号不能替换物理格ID；000204已耗100张产50只，后续更正不重复生成50只。未合并内部报料与供应商合并/采购/收料分开，采用60剩40、采用100剩0必须验证。

每项先失败复现，再正常/异常/旧状态闭环、相邻回归；不以测试数代替完整实际使用链。候选代码可本地提交，禁止自行push（先前远端导出审批限制仍保留）。最终合并逐文件核对，无DDL不做无关迁移；保持单head。正式数据更正和发布前置验证由根串行统筹，不能因早前授权而继续旧长停服流程。

当前状态：已整合慢启动09f26b3d、独立实物组件成本f56c7762、送货原请求协议e9dbd59d、未合并内部报料手工抵扣9f544831、搜索实物来源及文字位置f8c1d270。开发验证与正式发布分开：尚无新正式版本或正式业务更正。维护前置未提交补丁及边界见MAC_MAINTENANCE_V3_20261014.md；Sol继续独占static/index.html手工抵扣入口和资源hash，Astra已释放送货文件、接手C架服务/warehouse.py相关段及孪生前端格编辑段，根统一最后构建。

搜索补丁从旧ce02fc39按逐文件差异提取，无DDL、不授予消耗资格。新三个用例在当前基线均先失败（加工来源/移库后搜索为空，改号后首页实存为0），后续回归单独记录。不可把搜索到等同适用于任意订单。
