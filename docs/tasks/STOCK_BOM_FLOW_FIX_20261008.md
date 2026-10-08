# STOCK-BOM-FLOW-FIX-20261008

老板2026-10-08明确批准：按已出的修复方案分配合适模型修复、提交并发布。完整批准范围为NAS `04_开发记录/任务回执/20261008-补库BOM收料任务单与模数标签-原因及修复方案.md`。允许本任务并行；正式数据修复和发布只有root负责。

实际正式为v567 / 70647fa0 / eg1008sc / 包97387523。remote factory-current-baseline071cfa27滞后于实际运行，不能拿它覆盖已交付模数；本次基线选实际运行源码加已提交交付文档339df606，相关5个源码文件与正式包换行规范化完全一致。开发前再次核对运行，发布前再查并发变化。主工作区未知修改不动。

## 目标和文件所有权

1. backend执行者：app/api/requisition.py、app/services/stock_replenishment.py、app/services/unified_procurement.py、app/services/requisition_production_print*.py，必要新增数量/工艺合同服务及对应后端测试。负责BOM人工本次套数权威预览/保存与冻结、在途签名、新旧来源统一排序/总数、统一采购补库打印身份与版本；不得修改前端/warehouse.py/地图文件。
2. frontend执行者：static/index.html、static/requisition-production-print.html、static/mold-label.html、static/assets/mold-label-layout.js、其他必要相关static资源；app/api/warehouse.py中模具标签投影专属段，app/services/mold_label_content.py、mold_label_layout.py及对应标签/前端测试。负责输入/保存反馈/定位、标签改几模、分切首工序渲染。不得修改requisition.py、补库或打印后端服务、地图服务。
3. root：receipt_putaway.py、warehouse_ground_map_application.py、warehouse_twin_layout_editor.py等地图/收料货位服务、专项修复脚本与测试、文档、版本、整合、签名构建发布。不得覆盖执行者文件。
4. reviewer：独立只读审查并反馈，未经root明确分配不写文件。

每位执行者独立worktree，从本任务卡提交开始。新增文件先明确所有权；迁移必须先与root协调且唯一负责人。当前优先复用已存在可空JSON能力，不暗增schema。执行者允许提交，不发布、不改正式数据、不运行全仓测试；root整合后推送。

## 必须闭合

- 原建议格可编辑本次套数，正整数；000205每套长3短4、各4模，1800套无抵扣时1350/1800张，1×2时675/900；本次套数不再减成品360，也不修改预警1800目标。存在在途/库存时权威抵扣，重复数量不能累加两次。
- 新增后新到旧，单内冻结顺序保持；保存成功返回单号和明确查看入口，取消/过期读取不误报失败，刷新不能重复提交。
- SRO186/554/555分别关联补库20/21，不能用空销售任务版本拒绝补库打印，也不能创建假任务、移除权限/版本校验。
- 一楼待入库4区域30货位旧策略revision d27ae0261368c38c与正式地图bd9377ef30e5efd0不符。逐区真实几何/用途/身份验证后授权承接，禁止盲改版本/重建位置/取消门禁。
- 模具标签取M而非A×B或M×A×B；已有手工旧开料覆盖不能遮住新几模；旧冻结打印保留。共用模具模数不一致需核对。
- 分切由冻结A×B>1触发，必须第一步；M>1且1×1不触发。小衬板/盖底/BOM分别处理，已分切库存不重复分切。工艺用明确事实，无需强加工人点击环节。
- 在途签名两端统一，新模数JSON与历史NULL兼容必须明确，不能漏抵扣已采购1440套。
- 不自动修改当前SRO采购数量，不自动确认实收，不写历史订单、库存数量、成本或送货。修复后管理员从原单继续操作。

## 验证和发布

现有证据D:/.codex/workspace_artifacts/cutting-mold-followup-20261008，正式数据库+正式运行地图快照均在该目录。仅复制后使用；禁止原件写入，SQL模拟只在私有副本，独立账号仅本地。正式UI不自动点击，禁止IAB。

先目标失败复现，再最短定向正常/拒绝、权限客户、数量单位、状态/CAS、幂等、故障事务回滚、旧冻结事实保护；一项共享邻接回归。隔离数据库必须配对运行地图，不能仅DB测试冒充正式配置。发布前实时基线/唯一head、验证NAS时点备份、必要迁移演练、签名资源、正式完整性/FK及只读检查，交管理员人工验收。每位执行者NAS独立回执；root统一交付回执。真实token用量无法取得时如实说明。
