# STOCK-REPLENISHMENT-SAVE-RECOVERY-20261010

持续优化下一闭环：补库保存完整回执、原请求持久保存与只读原结果核对。上一Goal轮为progress：v611已正式发布并完成真实恢复和数据保持验证；本轮不能把原已知保存缺口继续留作已解决。

2026-10-10本轮实时正式v611/aa70b997095a1fc2604a74a4103a7596efedc5a3，包c523c18d8094e473ff44859340c71dabcc49bf4730991d25dc55c99489227227，en1009hp；origin/factory-current-baseline已经指向同一正式源。根1c753bee仅多上轮回执，工作树clean后创建codex/stock-replenishment-save-recovery-20261010。不从旧候选继续，不覆盖主工作区。

启动路由：CODEX_START→NAS AI_START→本卡→ORDER_FLOW→总需求4/6/7/10/11/12/16/17及平台身份合同→章程3～9。目标是员工真实保存后不误报、不丢请求、不重复采购；不是仅给报错文案。普通提交无需额外确认，权限、客户范围、冻结数量/单位/BOM、当前来源状态、版本、幂等、事务、审计和旧历史事实全部保持。未查询到结果不等于永久取消，也不允许自动换键重建。

本阶段只读复现/方案，不改运行源码或数据库结构。根独占全部仓库文件；本卡允许两名审查者并行，只写各自artifact（D:/.codex/visualizations/2026/10/10/stock-replenishment-save-recovery 下api、ui）。只读根factory-reliability-20261009树，不切换旧子树。实际HTTP使用安全conftest/tmp合成数据库，普通pytest不得继承正式数据库环境；UI执行实际页面方法和持久存储协议的合成运输层。禁止全仓测试、正式写入/自动点击、IAB、新测试服务/Chrome或旧PID终止，此前审批拒绝不重试或绕过。

- /root/mobile_drawings_api：核纸板普通补库、physical quantity_contract及外购专用保存完整来源；成功结果、原key/body/actor证明持久字段、错误后事务；审批分支是否另有真实保存入口。挑4～6项真实HTTP反例/保护，提最小只读resolver和完整回执合同。旧NULL不可回填证明；成功后主档变化/收料/作废时应返回原保存事实与当前状态，不因新建资格误拒；不得让查询接口再次采购。明确是否可无迁移实现，不复用不相容hash语义。
- /root/mobile_drawings_ui：核真实saveStockReplenishmentDraft与所有入口/关闭/重开/换账号/刷新，保存空或部分2xx、500/断网、辅助打印刷新失败及多标签页恢复。明确正常主动备库/预警/外购与员工申请分流，不把approval当已采购。挑6～10项高价值真实方法探针，画出最简原请求恢复入口和剩余合同。不得空stub实际保存逻辑或用虚构字段充原回执证明。
- 根：核当前正式/源码，整合复现，先固定可实现方案与文件owner，再授权最小开发与发布；独立审者待合同方案完成后接入。根不改正式库存、采购、成本或历史业务；当前无Git push。普通验证后发布长期授权延续，仍实时CAS、备份实际恢复、事实/附件/健康及人工验收门禁。

输出：复现与正常门禁、精确接口/字段/旧数据边界、最小修复方案、NAS回执。不得以测试次数充Bug数；保存未知的持久安全终止属于另一个reader/回退合同问题，本卡不虚构永久注销功能。Goal保持active。

架构复核接入：/root/order_recovery_review只写artifact/review，独立判断原完整actor+payload request_hash与稳定来源行能否作为原保存证明、是否需新增专用持久字段。禁止为免迁移把完整proof塞进quantity_contract/sheet_cutting/审计extra_json等相异合同；如果真实需要migration，先提出最小方案，由根按专项门禁执行。physical来源的专用external-purchase接口是相邻边界，不与本卡plain保存共用hash语义。原字段首次写入、后续合法更新/删除和历史NULL必须精确追查；对方案先审，不实施。

实施授权（2026-10-10）：上述只读阶段已完成，4类页面根因、10项页面观察、5项真实HTTP及独立架构复核支持最小无迁移闭环。采用 docs/reports/STOCK_REPLENISHMENT_SAVE_RECOVERY_PLAN_20261010.md 与同目录 STOCK_REPLENISHMENT_SAVE_RECOVERY_API_CONTRACT_20261010.md v1。当前阶段允许下列并行开发， supersede 上文只读限制；禁止边做边扩为全ERP重构。

- API唯一写入负责人 /root/mobile_drawings_api：app/api/requisition.py、新 app/services/stock_replenishment_save_recovery.py、其专属 tests。若不得不改其他已有服务，先报根确认文件owner。独立候选树须清洁并从本卡实施授权提交新建分支；保存旧分支，不使用旧候选基线。实现只读resolver、原hash兼容、完整成功回执、严格actor/scope/namespace与安全首次拒绝合同，无migration。
- UI唯一写入负责人 /root/mobile_drawings_ui：新 static/ui/stock-replenishment-save-recovery.js、static/index.html中本卡补库保存/恢复/账号边界接入及其专属 tests。保留611 go/action、提速、pending draft、员工审批分流。主保存只在完整回执成立后确认，任何已unknown请求后来拒绝仍保留。用API作者实际回包做最终跨端验证。
- 独立审者 /root/order_recovery_review：仅artifact/review；待候选成形后独立3～5项最关键反例，不改运行源码。根独占版本、文档、整合及发布；当前无Git push。

每名开发者只在自己的隔离候选树编辑白名单，实际HTTP测试使用合成隔离库，不启新服务/Chrome，不触碰旧PID。API_CONTRACT若需改变状态或字段须先通知根及另一负责人，双方一致再落码。422没有服务端绑定证明时不能按状态码清未知；当前记录必须可见且可查询，不伪造永久注销。条件证据不能替代候选验证和正式发布门禁。

正式基线变化（10:50核对）：另一任务已发布 v612 / 13e9376691afb93eb6bc1438d27c45682825e3b4 / 包8753c9781057690692a0add99654458259566acfe5c2536d37cdf741ed57447b，远端factory-current-baseline一致，revision仍en1009hp。根8a3d5e4f合并并保留全部手机上传图纸列表更新；与本卡API、index、新恢复模块无文件交集。子候选继续78bd6f2b授权基线，根串行合入；最终版本以发布前实时状态为准。


最终整合（2026-10-10）：实时正式已为v613/bcd3510f3b21c8d379f24b765d836502acf32886，包eaebeada5d466be5635caf4ddaf6ec1d85d81564ea0fd1005f722d62f54c989f，eo1010pi。根完整合入新报料加工冻结身份、BOM父身份、读取兼容门禁和手机图纸，未新增迁移；三份迁移文档已完整复核，历史退役命令不执行。原远端仍13e93766，发布前及持锁再次CAS。

最终作者API f5ad9f23、UI 661ab6b6，根运行整合30e239ef；独立审查另捕获并修复外购数量回执可自洽却不等原请求的假成功边界。普通BOM两子件真实4/8来源证明通过，fresh证明生成失败在commit前回滚，旧NULL仅提供线索。完整合同已同步实际HTTP包的float、层数hint、FastAPI detail包络等澄清。

根最终API17项、独立helper reader1项及UI115项通过；真实Vue模板编译、JS/Python语法和差异检查通过。最初测试启动误用conftest导致双播种及旧Python缺helper路径能力，已在runner隔离修正；没有改业务代码消除测试错误。独立原2API/3UI及最小数量变异证据按实际源码等价复用，不累加作新增bug。

候选v614只改补库保存/只读原结果恢复及版本，沿正式eo1010pi，无业务数据修正。发布须新冷备真实隔离恢复、原表字段/附件比对、签名资产及权限健康检查；实际结果另记本轮发布报告。管理员正式页面验收仍待反馈。本闭环不宣称全ERP全部修复。
